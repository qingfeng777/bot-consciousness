"""M13/M16: bounded candidate gateway, plan validation and resumable skills."""
import asyncio
from copy import deepcopy
import hashlib
import json

from .domain import DuplicateConflict, InvalidInput, canonical
from .state import identifier, number


def validate_predicate(predicate):
    if not isinstance(predicate, dict) or set(predicate) != {"entity", "attribute", "value"}:
        raise InvalidInput("condition requires entity, attribute and value")
    identifier(predicate["entity"])
    identifier(predicate["attribute"])
    if type(predicate["value"]) not in (str, bool, int, float):
        raise InvalidInput("condition value must be a scalar")
    canonical(predicate)


def validate_plan(plan):
    if (not isinstance(plan, dict) or not {"goal", "steps", "evidence"} <= set(plan)
            or set(plan) - {"goal", "steps", "evidence", "requires"}):
        raise InvalidInput("plan requires goal, steps and evidence")
    identifier(plan["goal"])
    if not isinstance(plan["evidence"], list) or not plan["evidence"] or len(plan["evidence"]) > 32:
        raise InvalidInput("plan requires 1..32 evidence references")
    for item in plan["evidence"]:
        identifier(item)
    requires = plan.get("requires", [])
    if not isinstance(requires, list) or len(requires) > 16:
        raise InvalidInput("plan condition budget exceeded")
    for condition in requires:
        validate_predicate(condition)
    if not isinstance(plan["steps"], list) or not 1 <= len(plan["steps"]) <= 8:
        raise InvalidInput("plan must have 1..8 steps")
    seen = set()
    for step in plan["steps"]:
        if (not isinstance(step, dict) or not {"id", "capability", "arguments", "after"} <= set(step)
                or set(step) - {"id", "capability", "arguments", "after", "when", "requires"}):
            raise InvalidInput("invalid step schema")
        if "when" in step:
            validate_predicate(step["when"])
        requires = step.get("requires", [])
        if not isinstance(requires, list) or len(requires) > 16:
            raise InvalidInput("step condition budget exceeded")
        for condition in requires:
            validate_predicate(condition)
        sid = identifier(step["id"])
        if sid in seen or not isinstance(step["after"], list) or any(d not in seen for d in step["after"]):
            raise InvalidInput("steps must have unique IDs and topologically ordered dependencies")
        if step["capability"] not in {"say", "alert"}:
            raise InvalidInput("unsupported capability")
        args = step["arguments"]
        if not isinstance(args, dict) or set(args) != {"text"} or not isinstance(args["text"], str):
            raise InvalidInput("unsupported arguments")
        if not args["text"].strip() or len(args["text"].encode()) > 12000:
            raise InvalidInput("command text outside budget")
        seen.add(sid)
    return deepcopy(plan)


class ModelGateway:
    """Provider protocol is an async callable returning JSON; no default network.

    Each request consumes a persisted call budget even if it times out. Only
    the current generation can return a usable candidate.
    """
    def __init__(self, store, provider, *, max_calls=3, timeout=2):
        if type(max_calls) is not int or not 1 <= max_calls <= 10:
            raise InvalidInput("invalid model call budget")
        number(timeout, 0.001, 120)
        self.store, self.provider = store, provider
        self.max_calls, self.timeout = max_calls, timeout

    async def generate(self, goal, context, generation):
        identifier(goal)
        if type(generation) is not int or generation < 0:
            raise InvalidInput("invalid context generation")
        with self.store.edit("gateway", goal) as state:
            calls = state.setdefault("calls", {}).get(goal, 0)
            latest = state.setdefault("generations", {}).get(goal, generation)
            if generation < latest or calls >= self.max_calls:
                raise InvalidInput("stale request or model budget exhausted")
            state["calls"][goal] = calls + 1
            state["generations"][goal] = generation
        try:
            raw = await asyncio.wait_for(self.provider(deepcopy(context)), self.timeout)
            if not isinstance(raw, str) or len(raw.encode()) > 65536:
                raise InvalidInput("model response outside budget")
            plan = validate_plan(json.loads(raw))
            if plan["goal"] != goal:
                raise InvalidInput("model returned a different goal")
            evidence = self.store.read("knowledge")[0].get("evidence", {})
            if any(not evidence.get(e, {}).get("active") for e in plan["evidence"]):
                raise InvalidInput("model cited unavailable evidence")
            if self.store.read("gateway")[0]["generations"][goal] != generation:
                raise InvalidInput("stale model response")
            self.store.journal.record("model.candidate", {"goal": goal, "generation": generation,
                                                           "digest": hashlib.sha256(raw.encode()).hexdigest()})
            return plan
        except (Exception, asyncio.CancelledError) as exc:
            self.store.journal.record("model.rejected", {"goal": goal, "error": type(exc).__name__})
            raise


class Skills:
    def __init__(self, store, goals, knowledge):
        self.store, self.goals, self.knowledge = store, goals, knowledge

    def register(self, sid, version, plan, *, provenance=()):
        identifier(sid)
        if type(version) is not int or version < 1:
            raise InvalidInput("skill version must be positive")
        plan = validate_plan(plan)
        provenance = sorted(set(provenance))
        known = self.store.read("knowledge")[0].get("evidence", {})
        if any(not known.get(e, {}).get("active") for e in provenance):
            raise InvalidInput("skill provenance unavailable")
        key = sid + ":" + str(version)
        with self.store.edit("skills", key) as state:
            definitions = state.setdefault("definitions", {})
            if key in definitions and definitions[key] != plan:
                raise DuplicateConflict("skill versions are immutable")
            sources = state.setdefault("provenance", {})
            if key in sources and sources[key] != provenance:
                raise DuplicateConflict("skill provenance is immutable")
            definitions[key] = plan
            sources[key] = provenance
        return key

    def retire(self, key):
        with self.store.edit("skills", "retire:" + key) as state:
            if key not in state.get("definitions", {}):
                raise InvalidInput("unknown skill")
            state.setdefault("retired", {})[key] = True

    def _evidence_valid(self, plan, key):
        evidence = self.store.read("knowledge")[0].get("evidence", {})
        sources = self.store.read("skills")[0].get("provenance", {}).get(key, [])
        return all(evidence.get(e, {}).get("active") for e in plan["evidence"] + sources)

    def condition(self, predicate, at):
        validate_predicate(predicate)
        hits = self.knowledge.search(entity=predicate["entity"], attribute=predicate["attribute"], at=at, limit=100)
        if not hits or any(not h["certain"] for h in hits):
            return None
        return any(type(h["value"]) is type(predicate["value"]) and h["value"] == predicate["value"] for h in hits)

    async def run(self, key, run_id, runtime, *, at):
        identifier(run_id)
        state, _ = self.store.read("skills")
        if key not in state.get("definitions", {}):
            raise InvalidInput("unknown skill")
        plan = state["definitions"][key]
        if state.get("retired", {}).get(key):
            raise InvalidInput("retired skill cannot execute")
        with self.store.edit("skills", run_id) as state:
            runs = state.setdefault("runs", {})
            if run_id in runs and runs[run_id]["key"] != key:
                raise DuplicateConflict("run ID already pins another version")
            runs.setdefault(run_id, dict(key=key, completed=[], skipped=[], status="ready"))
        for step in plan["steps"]:
            run = self.store.read("skills")[0]["runs"][run_id]
            if step["id"] in run["completed"] or step["id"] in run.get("skipped", []):
                continue
            event = "step-" + hashlib.sha256(canonical([run_id, key, step["id"]]).encode()).hexdigest()
            existing = next((t for t in runtime.journal.tasks() if t["event_id"] == event), None)
            if existing is None:
                if any(self.condition(p, at) is not True for p in plan.get("requires", []) + step.get("requires", [])):
                    return self._checkpoint(run_id, "blocked")
                if "when" in step:
                    matches = self.condition(step["when"], at)
                    if matches is None:
                        return self._checkpoint(run_id, "blocked")
                    if matches is False:
                        with self.store.edit("skills", event) as state:
                            state["runs"][run_id].setdefault("skipped", []).append(step["id"])
                        continue
                goals = self.goals.evaluate(at)
                goal = goals.get(plan["goal"])
                reserved = goal and event in goal["attempts"]
                resume_reserved = (reserved and goal["status"] == "blocked"
                                   and all(goals[d]["status"] == "satisfied" for d in goal["definition"]["dependencies"])
                                   and (goal["definition"]["deadline"] is None or at < goal["definition"]["deadline"]))
                if goal is None or (goal["status"] != "active" and not resume_reserved) or not self._evidence_valid(plan, key):
                    return self._checkpoint(run_id, "blocked")
                self.goals.reserve(plan["goal"], event)
                task = await runtime.submit_action(step["capability"], step["arguments"], event_id=event)
                await runtime.drain()
                outcome = runtime.journal.task(task)
            else:
                # Recover a committed effect even if the process died before
                # the skill checkpoint. Never send another action for this step.
                await runtime.drain()
                outcome = runtime.journal.task(existing["id"])
            if outcome["status"] != "completed":
                return self._checkpoint(run_id, "blocked")
            with self.store.edit("skills", event) as state:
                record = state["runs"][run_id]
                if step["id"] not in record["completed"]:
                    record["completed"].append(step["id"])
                record["status"] = "running"
        # Completion of steps is not proof of the goal predicate.
        return self._checkpoint(run_id, "steps_completed")

    def _checkpoint(self, run_id, status):
        with self.store.edit("skills", run_id) as state:
            state["runs"][run_id]["status"] = status
            result = deepcopy(state["runs"][run_id])
        return result
