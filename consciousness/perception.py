"""M02: deterministic evidence adapters for text and labelled local fixtures.

Media fixtures supply annotations, not actual ASR/vision inference. Their
provenance explicitly says simulation and cannot be confused with sensors.
"""
import hashlib

from .domain import DuplicateConflict, InvalidInput
from .state import identifier, number


class Perception:
    def __init__(self, store, knowledge):
        self.store, self.knowledge = store, knowledge

    def ingest(self, eid, modality, content, *, at, quality=1, annotation=None, partial=False):
        identifier(eid)
        number(at)
        number(quality, 0, 1)
        if modality not in {"text", "image", "audio", "video"} or type(partial) is not bool:
            raise InvalidInput("invalid modality or partial flag")
        if not isinstance(content, bytes) or not 0 < len(content) <= 1024 * 1024:
            raise InvalidInput("fixture input must contain 1..1048576 bytes")
        if modality == "text":
            try:
                text = content.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise InvalidInput("text must be UTF-8") from exc
        else:
            text = annotation
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 16384):
            raise InvalidInput("invalid fixture annotation")
        record = dict(id=eid, modality=modality, checksum=hashlib.sha256(content).hexdigest(),
                      at=at, quality=quality, partial=partial, text=text,
                      status="unknown" if quality < 0.8 or text is None else "partial" if partial else "observed",
                      simulation=modality != "text")
        previous = self.store.read("perception")[0].get("observations", {}).get(eid)
        if previous and previous != record:
            raise DuplicateConflict("perception event changed; revisions need a new ID")
        if record["status"] == "observed":
            self.knowledge.evidence(eid, "text-input" if modality == "text" else "simulated-" + modality,
                                    text, group=record["checksum"])
        with self.store.edit("perception", eid) as state:
            state.setdefault("observations", {})[eid] = record
        return record
