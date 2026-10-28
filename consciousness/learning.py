"""M15/M17/M19: evaluated candidates, auditable fast rules and hypotheses."""
from copy import deepcopy
import hashlib

from .domain import DuplicateConflict, InvalidInput, canonical
from .planning import validate_plan
from .state import identifier, number


class Learning:
    def __init__(self, store):
        self.store = store

    def experience(self, eid, session, evidence, *, success):
        identifier(eid)
        identifier(session)
        if type(success) is not bool or not evidence:
            raise InvalidInput("experience needs an observed outcome and evidence")
        known = self.store.read("knowledge")[0].get("evidence", {})
        if any(not known.get(e, {}).get("active") for e in evidence):
            raise InvalidInput("experience evidence unavailable")
        record = dict(id=eid, session=session, evidence=sorted(set(evidence)), success=success)
        with self.store.edit("learning", eid) as state:
            rows = state.setdefault("experiences", {})
            if eid in rows and rows[eid] != record:
                raise DuplicateConflict("experience changed")
            rows[eid] = record

    def propose(self, cid, plan, training):
        identifier(cid)
        plan = validate_plan(plan)
        with self.store.edit("learning", cid) as state:
            experiences = state.get("experiences", {})
            if not training or any(e not in experiences for e in training):
                raise InvalidInput("candidate requires training experiences")
            definition = dict(plan=plan, training=sorted(set(training)))
            candidates = state.setdefault("candidates", {})
            if cid in candidates:
                if candidates[cid]["definition"] != definition:
                    raise DuplicateConflict("candidate ID reused")
                return
            candidates[cid] = dict(definition=definition, status="proposed", evaluations=[])

    def evaluate(self, cid, validation, outcomes, baseline, *, minimum_sessions=2):
        if type(minimum_sessions) is not int or minimum_sessions < 2:
            raise InvalidInput("at least two independent validation sessions required")
        if (not validation or len(set(validation)) != len(validation) or len(outcomes) != len(validation)
                or len(baseline) != len(validation) or any(type(x) is not bool for x in outcomes + baseline)):
            raise InvalidInput("invalid paired evaluation")
        with self.store.edit("learning", "evaluate:" + cid) as state:
            candidate = state["candidates"][cid]
            if candidate["status"] in {"published", "retired"}:
                raise InvalidInput("published candidates are immutable; propose another version")
            rows = state["experiences"]
            if any(e not in rows for e in validation):
                raise InvalidInput("unknown validation experience")
            training = candidate["definition"]["training"]
            train_sessions = {rows[e]["session"] for e in training}
            test_sessions = {rows[e]["session"] for e in validation}
            groups = self.store.read("knowledge")[0].get("evidence", {})
            train_groups = {groups[e]["group"] for x in training for e in rows[x]["evidence"]}
            test_groups = {groups[e]["group"] for x in validation for e in rows[x]["evidence"]}
            if train_sessions & test_sessions or train_groups & test_groups:
                raise InvalidInput("training/validation evidence leakage")
            if any(not groups[e]["active"] for x in training + validation for e in rows[x]["evidence"]):
                raise InvalidInput("evaluation source withdrawn")
            passed = len(test_sessions) >= minimum_sessions and all(outcomes) and sum(outcomes) >= sum(baseline)
            report = dict(validation=list(validation), outcomes=list(outcomes), baseline=list(baseline),
                          sessions=len(test_sessions), passed=passed)
            candidate["evaluations"].append(report)
            candidate["status"] = "validated" if passed else "rejected"
        return report

    def publish(self, cid, skills, *, configured=False):
        if configured is not True:
            raise InvalidInput("publication requires explicit configuration")
        state = self.store.read("learning")[0]
        candidate = state["candidates"][cid]
        if candidate["status"] not in {"validated", "published"}:
            raise InvalidInput("candidate has not passed validation")
        evidence = self.store.read("knowledge")[0].get("evidence", {})
        ids = candidate["definition"]["training"] + candidate["evaluations"][-1]["validation"]
        if any(not evidence[e]["active"] for x in ids for e in state["experiences"][x]["evidence"]):
            raise InvalidInput("publication evidence withdrawn")
        provenance = {e for x in ids for e in state["experiences"][x]["evidence"]}
        key = skills.register("learned-" + cid, 1, candidate["definition"]["plan"], provenance=provenance)
        with self.store.edit("learning", "publish:" + cid) as state:
            state["candidates"][cid]["status"] = "published"
            state["candidates"][cid]["skill"] = key
        return key

    def retire(self, cid, skills):
        candidate = self.store.read("learning")[0]["candidates"][cid]
        if candidate.get("skill"):
            skills.retire(candidate["skill"])
        with self.store.edit("learning", "retire:" + cid) as state:
            state["candidates"][cid]["status"] = "retired"


class Reflexes:
    def __init__(self, store):
        self.store = store

    def configure(self, rid, trigger, response, *, cooldown=5):
        for item in (rid, trigger, response):
            identifier(item)
        number(cooldown, 0, 3600)
        record = dict(trigger=trigger.casefold().strip(), response=response, cooldown=cooldown)
        with self.store.edit("reflexes", rid) as state:
            state.setdefault("rules", {})[rid] = record

    async def react(self, eid, text, entity, at, runtime):
        for value in (eid, entity):
            identifier(value)
        number(at)
        fingerprint = canonical([text, entity, at])
        state = self.store.read("reflexes")[0]
        prior = state.get("activations", {}).get(eid)
        if prior:
            if prior["fingerprint"] != fingerprint:
                raise DuplicateConflict("reflex event changed")
            activation = prior
        else:
            rules = [(rid, r) for rid, r in state.get("rules", {}).items() if r["trigger"] == text.strip().casefold()]
            responses = {r["response"] for _, r in rules}
            activation = dict(fingerprint=fingerprint, status="unmatched")
            if len(responses) > 1:
                activation["status"] = "conflict"
            elif rules:
                rid, rule = sorted(rules)[0]
                cooldown_key = canonical([rid, entity])
                last = state.get("last", {}).get(cooldown_key)
                if last is not None and at < last + rule["cooldown"]:
                    activation["status"] = "suppressed"
                else:
                    activation.update(status="pending", response=rule["response"], key=cooldown_key, at=at)
            with self.store.edit("reflexes", eid) as state:
                state.setdefault("activations", {})[eid] = activation
                if activation["status"] == "pending":
                    state.setdefault("last", {})[activation["key"]] = at
        if activation["status"] == "pending":
            event = "reflex-" + hashlib.sha256(eid.encode()).hexdigest()
            task = await runtime.submit_action("alert", {"text": activation["response"]}, event_id=event)
            with self.store.edit("reflexes", eid) as state:
                state["activations"][eid].update(status="dispatched", task=task)
            return {**activation, "status": "dispatched", "task": task}
        return deepcopy(activation)


class Association:
    def __init__(self, store):
        self.store = store

    def propose(self, seed, questions, *, generation, depth=1):
        identifier(seed)
        if type(generation) is not int or generation < 0 or depth not in (1, 2):
            raise InvalidInput("association generation/depth outside budget")
        if not isinstance(questions, list) or len(questions) > 5:
            raise InvalidInput("at most five hypotheses per expansion")
        known = self.store.read("knowledge")[0].get("evidence", {})
        if not known.get(seed, {}).get("active"):
            raise InvalidInput("seed must be real active evidence, not generated text")
        with self.store.edit("association", seed) as state:
            latest = state.setdefault("generations", {}).get(seed, generation)
            if generation < latest:
                raise InvalidInput("stale association context")
            state["generations"][seed] = generation
            items = state.setdefault("hypotheses", {})
            result = []
            for question in questions:
                identifier(question)
                key = hashlib.sha256(canonical([seed, question]).encode()).hexdigest()
                items.setdefault(key, dict(id=key, seed=seed, question=question, generation=generation,
                                           depth=depth, status="unverified"))
                result.append(deepcopy(items[key]))
        return result
