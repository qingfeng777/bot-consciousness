"""One cloud inference path for every model-assisted cognitive module."""
import asyncio
import base64
from copy import deepcopy
import hashlib
from pathlib import Path
import time

from .cloud import CloudClient, CloudConfig, CloudError, strict_json
from .domain import DuplicateConflict, InvalidInput, canonical
from .knowledge import Knowledge
from .prompting import PROMPT_VERSION, SCHEMAS, prompt, validate_output
from .state import identifier, number


def media_parts(attachments, config):
    if not isinstance(attachments, list) or len(attachments) > 8:
        raise InvalidInput("at most eight media inputs/frames per request")
    parts, manifests, total, seen = [], [], 0, set()
    for attachment in attachments:
        if not isinstance(attachment, dict) or not {"id", "path", "type"} <= set(attachment) or set(attachment) - {"id", "path", "type", "at"}:
            raise InvalidInput("media needs id/path/type and optional frame time")
        mid = identifier(attachment["id"])
        if mid in seen:
            raise InvalidInput("media IDs must be unique")
        seen.add(mid)
        kind = attachment["type"]
        if kind not in {"image", "audio"} or kind not in config.modalities:
            raise InvalidInput("media modality not enabled for the selected cloud endpoint")
        path = Path(attachment["path"])
        with path.open("rb") as source:
            raw = source.read(8 * 1024 * 1024 + 1)
        total += len(raw)
        if not raw or total > 8 * 1024 * 1024:
            raise InvalidInput("media budget is 8 MiB per cloud request")
        suffix = path.suffix.lower()
        if kind == "image":
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix)
            valid = ((mime == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n")) or
                     (mime == "image/jpeg" and raw.startswith(b"\xff\xd8\xff")) or
                     (mime == "image/webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"))
            if not valid:
                raise InvalidInput("unsupported image format or signature")
            data = "data:" + mime + ";base64," + base64.b64encode(raw).decode()
            part = {"type": "image_url", "image_url": {"url": data}}
        else:
            if suffix not in {".wav", ".mp3"}:
                raise InvalidInput("audio input supports WAV or MP3")
            encoded = base64.b64encode(raw).decode()
            if config.audio_data_url:
                encoded = "data:" + ("audio/wav" if suffix == ".wav" else "audio/mpeg") + ";base64," + encoded
            part = {"type": "input_audio", "input_audio": {"data": encoded, "format": suffix[1:]}}
        manifest = {"id": mid, "type": kind, "sha256": hashlib.sha256(raw).hexdigest()}
        if "at" in attachment:
            manifest["at"] = number(attachment["at"])
        parts.extend([{"type": "text", "text": "媒体证据 " + canonical(manifest)}, part])
        manifests.append(manifest)
    return parts, manifests


class CloudCognition:
    WATCHED = ("knowledge", "goals", "self", "scene", "identity", "learning")

    def __init__(self, store, client=None):
        self.store = store
        self.client = client or CloudClient(CloudConfig.from_env())

    def _revisions(self):
        return {name: self.store.read(name)[1] for name in self.WATCHED}

    async def infer(self, purpose, request_id, input, *, scope="default", generation=0,
                    evidence_ids=None, attachments=None):
        identifier(request_id)
        identifier(scope)
        if purpose not in SCHEMAS or type(generation) is not int or generation < 0:
            raise InvalidInput("invalid cloud task or generation")
        if not isinstance(input, dict) or len(canonical(input).encode()) > 32768:
            raise InvalidInput("cloud task input must be an object within 32 KiB")
        instruction, prompt_hash = prompt(purpose)
        parts, media = media_parts(attachments or [], self.client.config)
        knowledge = Knowledge(self.store)
        for item in media:
            knowledge.evidence(item["id"], "cloud-input-media", canonical(item), group=item["sha256"])
        state = self.store.read("knowledge")[0]
        registered = state.get("evidence", {})
        if evidence_ids is None:
            evidence_ids = sorted(eid for eid, record in registered.items() if record["active"])
        if (not isinstance(evidence_ids, list) or any(not isinstance(e, str) for e in evidence_ids)
                or len(evidence_ids) > 32 or len(set(evidence_ids)) != len(evidence_ids)):
            raise InvalidInput("select at most 32 unique evidence IDs")
        if any(not registered.get(e, {}).get("active") for e in evidence_ids):
            raise InvalidInput("cloud context references unavailable evidence")
        if not {m["id"] for m in media} <= set(evidence_ids):
            raise InvalidInput("media evidence must be included in evidence_ids")
        goal_id = input.get("goal")
        if goal_id is not None and not isinstance(goal_id, str):
            raise InvalidInput("goal ID must be a string")
        goals = self.store.read("goals")[0].get("items", {})
        if purpose in {"plan", "learning"} and (not isinstance(goal_id, str) or goal_id not in goals):
            raise InvalidInput("planning requires a registered goal")
        training = input.get("training", []) if purpose == "learning" else []
        experiences = self.store.read("learning")[0].get("experiences", {})
        if not isinstance(training, list) or len(training) > 32 or any(not isinstance(e, str) or e not in experiences for e in training):
            raise InvalidInput("invalid training experience references")
        revisions = self._revisions()
        model = self.client.model_for(purpose)
        binding = dict(purpose=purpose, input=input, evidence_ids=sorted(evidence_ids), media=media,
                       scope=scope, generation=generation, model=model, endpoint=self.client.config.base_url,
                       prompt_hash=prompt_hash, settings=dict(max_tokens=self.client.config.max_tokens,
                           json_mode=self.client.config.json_mode, stream=self.client.config.stream,
                           extra_body=self.client.config.extra_body, audio_data_url=self.client.config.audio_data_url))
        fingerprint = hashlib.sha256(canonical(binding).encode()).hexdigest()
        generation_key = canonical([scope, purpose])
        with self.store.edit("cloud_requests", request_id) as ledger:
            previous = ledger.setdefault("requests", {}).get(request_id)
            latest = ledger.setdefault("generations", {}).get(generation_key, generation)
            if generation < latest:
                raise InvalidInput("cloud request generation is stale")
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise DuplicateConflict("cloud request ID was reused with different input")
                if previous["status"] == "accepted" and previous["revisions"] == revisions:
                    return deepcopy(previous["result"])
                raise InvalidInput("cloud request is stale, failed or unresolved; use a new request ID")
            if ledger.get("calls", 0) >= self.client.config.max_calls:
                raise InvalidInput("persistent cloud call budget exhausted")
            ledger["calls"] = ledger.get("calls", 0) + 1
            ledger["generations"][generation_key] = generation
            ledger["requests"][request_id] = dict(fingerprint=fingerprint, purpose=purpose, model=model,
                prompt_version=PROMPT_VERSION, prompt_sha256=prompt_hash, status="pending", revisions=revisions,
                generation=generation, generation_key=generation_key, usage={})
        started = time.monotonic()
        try:
            context = dict(input=deepcopy(input), evidence=[registered[e] for e in evidence_ids],
                           goal=goals.get(goal_id), training=[experiences[e] for e in training],
                           capabilities={"say": {"text": "string"}, "alert": {"text": "string"}},
                           context_revisions=revisions, media=media)
            if len(canonical(context).encode()) > 128 * 1024:
                raise InvalidInput("selected cloud context exceeds 128 KiB")
            user_content = [{"type": "text", "text": canonical(context)}, *parts] if parts else canonical(context)
            response = await self.client.complete(purpose, [{"role": "system", "content": instruction},
                                                           {"role": "user", "content": user_content}])
            result = validate_output(purpose, strict_json(response["content"]), evidence_ids,
                                     goal=goal_id, training=training)
            if purpose == "plan" and result["status"] == "ok":
                goal = goals[goal_id]
                remaining = goal["definition"]["budget"] - len(goal["attempts"])
                if goal["status"] in {"satisfied", "cancelled", "expired"} or len(result["data"]["steps"]) > remaining:
                    raise InvalidInput("model plan exceeds the remaining goal budget or goal is terminal")
            current = self.store.read("cloud_requests")[0]
            if self._revisions() != revisions or current["generations"][generation_key] != generation:
                raise InvalidInput("cloud response context or generation is stale")
            with self.store.edit("cloud_requests", request_id) as ledger:
                ledger["requests"][request_id].update(status="accepted", result=result, usage=response["usage"],
                                                       latency_ms=(time.monotonic() - started) * 1000)
            return deepcopy(result)
        except (Exception, asyncio.CancelledError) as exc:
            with self.store.edit("cloud_requests", request_id) as ledger:
                ledger["requests"][request_id].update(status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    error=type(exc).__name__, latency_ms=(time.monotonic() - started) * 1000)
            raise


class CloudPlanProvider:
    """Compatibility adapter for the existing ModelGateway; production default."""
    def __init__(self, store):
        self.service = CloudCognition(store)

    async def __call__(self, context):
        goal = context.get("goal")
        request_id = context.get("request_id")
        if not request_id:
            from .domain import new_id
            request_id = "plan-" + new_id()
        result = await self.service.infer("plan", request_id, context, scope=goal,
                                          generation=context.get("generation", 0),
                                          evidence_ids=context.get("evidence_ids"))
        if result["status"] != "ok":
            raise InvalidInput("cloud planner needs clarification")
        return canonical(result["data"])
