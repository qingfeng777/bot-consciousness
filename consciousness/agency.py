"""M06/07/09/10/11/12: persistent scene, goals, telemetry and bounded drives."""
from copy import deepcopy
import math

from .domain import DuplicateConflict, InvalidInput
from .state import identifier, number


class Scene:
    def __init__(self, store):
        self.store = store

    def observe(self, eid, *, location, sensor, track, label, at, lateness=5):
        for value in (eid, location, sensor, track, label):
            identifier(value)
        number(at)
        number(lateness)
        record = dict(location=location, sensor=sensor, track=track, label=label, at=at)
        with self.store.edit("scene", eid) as state:
            seen = state.setdefault("observations", {})
            if eid in seen:
                if seen[eid] != record:
                    raise DuplicateConflict("observation changed")
                return
            seen[eid] = record
            watermark = state.get("watermark", at)
            if at < watermark - lateness:
                state.setdefault("historical_corrections", []).append(eid)
                return
            if at < watermark:
                state.setdefault("late_observations", []).append(eid)
                return  # Historical revision does not overwrite current location.
            if state.get("location") != location:
                state.setdefault("episodes", []).append(dict(location=location, start=at, evidence=eid))
                state["entities"] = {}
            state["location"], state["watermark"] = location, at
            # Same names from different tracks are never silently merged.
            key = sensor + ":" + track
            state.setdefault("entities", {})[key] = dict(label=label, last_seen=at, evidence=eid)

    def snapshot(self):
        return self.store.read("scene")[0]


class SelfState:
    METRICS = {"battery": ("%", 0, 100), "temperature": ("C", -50, 150)}

    def __init__(self, store):
        self.store = store

    def sample(self, eid, metric, value, unit, at, ttl=30, quality=1):
        identifier(eid)
        if metric not in self.METRICS or unit != self.METRICS[metric][0]:
            raise InvalidInput("unknown telemetry metric or unit")
        number(value, *self.METRICS[metric][1:])
        number(at)
        number(ttl, 0.001, 3600)
        number(quality, 0, 1)
        record = dict(id=eid, metric=metric, value=value, unit=unit, at=at, ttl=ttl, quality=quality)
        with self.store.edit("self", eid) as state:
            seen = state.setdefault("samples", {})
            if eid in seen:
                if seen[eid] != record:
                    raise DuplicateConflict("telemetry ID reused")
                return
            seen[eid] = record
            current = state.setdefault("current", {})
            if metric not in current or at >= current[metric]["at"]:
                current[metric] = record
                if metric == "battery" and quality >= 0.8:
                    if value < 20:
                        state["low_battery"] = True
                    elif value >= 30:
                        state["low_battery"] = False

    def snapshot(self, at):
        number(at)
        state, revision = self.store.read("self")
        result = {"revision": revision, "low_battery": state.get("low_battery", False)}
        for metric in self.METRICS:
            item = state.get("current", {}).get(metric)
            validity = ("unknown" if item is None or not item["at"] <= at < item["at"] + item["ttl"]
                        else "degraded" if item["quality"] < 0.8 else "known")
            result[metric] = {"validity": validity, "sample": item,
                              "value": item["value"] if validity == "known" else None}
        return result


class Goals:
    def __init__(self, store, knowledge):
        self.store, self.knowledge = store, knowledge

    def add(self, gid, predicate, *, dependencies=(), deadline=None, budget=8):
        identifier(gid)
        if set(predicate) != {"entity", "attribute", "value"}:
            raise InvalidInput("success predicate requires entity/attribute/value")
        for k in ("entity", "attribute"):
            identifier(predicate[k])
        if deadline is not None:
            number(deadline)
        if type(budget) is not int or not 1 <= budget <= 64:
            raise InvalidInput("goal budget must be 1..64")
        definition = dict(predicate=deepcopy(predicate), dependencies=sorted(set(dependencies)),
                          deadline=deadline, budget=budget)
        with self.store.edit("goals", gid) as state:
            goals = state.setdefault("items", {})
            if gid in goals:
                if goals[gid]["definition"] != definition:
                    raise DuplicateConflict("goal definition is immutable")
                return
            if gid in dependencies or any(d not in goals for d in dependencies):
                raise InvalidInput("dependencies must be existing goals; cycles are forbidden")
            goals[gid] = dict(id=gid, definition=definition, status="proposed", attempts=[], evidence=[])

    def evaluate(self, at):
        number(at)
        with self.store.edit("goals", "evaluate") as state:
            goals = state.setdefault("items", {})
            # Dependencies may sort after dependants in persisted JSON.
            done = set()
            while len(done) < len(goals):
                ready = [g for g in goals.values() if g["id"] not in done
                         and all(d in done for d in g["definition"]["dependencies"])]
                if not ready:
                    raise InvalidInput("cyclic persisted goal graph")
                for goal in ready:
                    done.add(goal["id"])
                    if goal["status"] == "cancelled":
                        continue
                    d = goal["definition"]
                    hits = self.knowledge.search(entity=d["predicate"]["entity"],
                                                 attribute=d["predicate"]["attribute"], at=at, limit=100)
                    goal["evidence"] = [h["id"] for h in hits if h["certain"] and h["value"] == d["predicate"]["value"]]
                    if any(goals[p]["status"] != "satisfied" for p in d["dependencies"]):
                        goal["status"] = "blocked"
                    elif goal["evidence"]:
                        goal["status"] = "satisfied"
                    elif d["deadline"] is not None and at >= d["deadline"]:
                        goal["status"] = "expired"
                    elif len(goal["attempts"]) >= d["budget"]:
                        goal["status"] = "blocked"
                    else:
                        goal["status"] = "active"
        return self.store.read("goals")[0]["items"]

    def reserve(self, gid, step_id):
        identifier(step_id)
        with self.store.edit("goals", step_id) as state:
            goal = state["items"][gid]
            if step_id in goal["attempts"]:
                return
            if goal["status"] != "active" or len(goal["attempts"]) >= goal["definition"]["budget"]:
                raise InvalidInput("goal is not active or budget exhausted")
            goal["attempts"].append(step_id)

    def cancel(self, gid):
        with self.store.edit("goals", "cancel:" + gid) as state:
            state["items"][gid]["status"] = "cancelled"


class Affect:
    STIMULI = {"success": (0.3, 0.1, -0.2), "failure": (-0.3, 0.2, 0.3),
               "emergency": (-0.2, 0.7, 0.5), "calm": (0, 0, 0)}

    def __init__(self, store):
        self.store = store

    def apply(self, eid, kind, at):
        identifier(eid)
        number(at)
        if kind not in self.STIMULI:
            raise InvalidInput("unknown affect stimulus")
        with self.store.edit("affect", eid) as state:
            events = state.setdefault("events", {})
            event = dict(kind=kind, at=at, active=True)
            if eid in events:
                if {**events[eid], "active": True} != event:
                    raise DuplicateConflict("affect event changed")
                return
            events[eid] = event

    def revoke(self, eid):
        with self.store.edit("affect", "revoke:" + eid) as state:
            state["events"][eid]["active"] = False

    def snapshot(self, at):
        number(at)
        events = self.store.read("affect")[0].get("events", {})
        values, mood, last, label = [0.0, 0.0, 0.0], 0.0, 0.0, "calm"
        for eid, event in sorted(events.items(), key=lambda pair: (pair[1]["at"], pair[0])):
            if not event["active"] or event["at"] > at:
                continue
            dt = event["at"] - last
            values = [v * math.exp(-dt / 30) for v in values]
            stimulus = self.STIMULI[event["kind"]]
            values = [max(-1 if i == 0 else 0, min(1, v + stimulus[i])) for i, v in enumerate(values)]
            mood = max(-1, min(1, mood * math.exp(-dt / 300) + stimulus[0] * 0.1))
            if values[1] >= 0.6:
                label = "alert"
            elif values[1] <= 0.3:
                label = "calm"
            last = event["at"]
        values = [v * math.exp(-(at - last) / 30) for v in values]
        if values[1] <= 0.3:
            label = "calm"
        return dict(valence=values[0], arousal=values[1], tension=values[2],
                    mood=mood * math.exp(-(at - last) / 300), label=label)


class Motivation:
    def __init__(self, store, goals, telemetry):
        self.store, self.goals, self.telemetry = store, goals, telemetry

    def tick(self, at):
        snapshot = self.telemetry.snapshot(at)
        if snapshot["battery"]["validity"] != "known":
            return {"status": "unknown", "reason": "battery telemetry unavailable"}
        state, _ = self.store.read("motivation")
        current = state.get("charge_goal")
        if snapshot["low_battery"]:
            if current is None:
                current = "charge-" + snapshot["battery"]["sample"]["id"]
                self.goals.add(current, dict(entity=current, attribute="charged", value=True), budget=3)
                with self.store.edit("motivation", current) as state:
                    state["charge_goal"] = current
                    history = state.setdefault("history", {})
                    history[current] = dict(goal=current, reason="low_battery", triggered_at=at,
                                            sample=snapshot["battery"]["sample"]["id"], status="dispatched")
                    if len(history) > 200:
                        for old in sorted(history, key=lambda key: history[key]["triggered_at"])[:-200]:
                            del history[old]
            return {"status": "dispatched", "goal": current}
        if current is not None:
            sample = snapshot["battery"]["sample"]
            evidence = "charge-evidence-" + sample["id"]
            self.goals.knowledge.evidence(evidence, "simulated-telemetry", "battery recovered to " + str(sample["value"]))
            self.goals.knowledge.claim("charge-complete-" + current, current, "charged", True, [evidence],
                                       start=sample["at"])
            self.goals.evaluate(at)
            with self.store.edit("motivation", "battery-recovered") as state:
                state.pop("charge_goal", None)
                if current in state.get("history", {}):
                    state["history"][current].update(status="resolved", resolved_at=at, evidence=[evidence])
        return {"status": "resolved"}


class Identity:
    def __init__(self, store):
        self.store = store

    def configure(self, role, style, *, version, expires=None):
        identifier(role)
        if style not in {"neutral", "concise", "friendly"} or type(version) is not int or version < 1:
            raise InvalidInput("invalid role version or expression style")
        if expires is not None:
            number(expires)
        record = dict(role=role, style=style, version=version, expires=expires)
        with self.store.edit("identity", "role:" + role) as state:
            key = role + ":" + str(version)
            versions = state.setdefault("versions", {})
            if key in versions and versions[key] != record:
                raise DuplicateConflict("role version is immutable")
            versions[key] = record
            state["active"] = key

    def snapshot(self, at):
        number(at)
        state, revision = self.store.read("identity")
        role = state.get("versions", {}).get(state.get("active"))
        if role is None or role["expires"] is not None and at >= role["expires"]:
            role = dict(role="assistant", style="neutral", version=0, expires=None)
        return {**role, "revision": revision, "robot_id": self.store.journal.robot_id}
