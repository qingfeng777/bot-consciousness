"""M07: persisted daily local-time rules with explicit DST and catch-up policy."""
from datetime import datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .domain import DuplicateConflict, InvalidInput, canonical
from .state import identifier, number


class Calendar:
    def __init__(self, store, goals):
        self.store, self.goals = store, goals

    def configure(self, rid, predicate, *, hour, minute, zone="UTC", start=None,
                  policy="run_once", catchup=3, fold=0, budget=1):
        identifier(rid)
        start = self.store.journal.clock.now() if start is None else start
        number(start)
        if type(hour) is not int or not 0 <= hour <= 23 or type(minute) is not int or not 0 <= minute <= 59:
            raise InvalidInput("invalid local calendar time")
        if policy not in {"skip", "run_once", "bounded_catchup"}:
            raise InvalidInput("unknown catch-up policy")
        if type(catchup) is not int or not 1 <= catchup <= 10 or type(fold) is not int or fold not in (0, 1):
            raise InvalidInput("invalid catch-up budget or DST fold")
        if type(budget) is not int or not 1 <= budget <= 64:
            raise InvalidInput("invalid occurrence action budget")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise InvalidInput("unknown time zone") from exc
        if not isinstance(predicate, dict) or set(predicate) != {"entity", "attribute", "value"}:
            raise InvalidInput("invalid occurrence success predicate")
        identifier(predicate["entity"])
        identifier(predicate["attribute"])
        record = dict(predicate=predicate, hour=hour, minute=minute, zone=zone, start=start,
                      policy=policy, catchup=catchup, fold=fold, budget=budget)
        with self.store.edit("calendar", rid) as state:
            rules = state.setdefault("rules", {})
            if rid in rules:
                if rules[rid]["definition"] != record:
                    raise DuplicateConflict("calendar rule definitions are immutable")
                return
            rules[rid] = dict(definition=record, through=None, enabled=True)

    def disable(self, rid):
        with self.store.edit("calendar", "disable:" + rid) as state:
            state["rules"][rid]["enabled"] = False

    @staticmethod
    def _due(definition, through, now):
        zone = ZoneInfo(definition["zone"])
        start = definition["start"] if through is None else max(definition["start"], through)
        first = datetime.fromtimestamp(start, zone).date()
        last = datetime.fromtimestamp(now, zone).date()
        if (last - first).days > 3660:
            raise InvalidInput("calendar scan exceeds ten-year simulation budget")
        occurrences = []
        while first <= last:
            local = datetime(first.year, first.month, first.day, definition["hour"], definition["minute"],
                             tzinfo=zone, fold=definition["fold"])
            timestamp = local.timestamp()
            roundtrip = datetime.fromtimestamp(timestamp, zone)
            # DST gaps are skipped. During a fold, exactly the configured offset wins.
            exists = (roundtrip.replace(tzinfo=None) == local.replace(tzinfo=None)
                      and (roundtrip.fold == local.fold or local.utcoffset() == local.replace(fold=0).utcoffset()))
            if exists and timestamp >= definition["start"] and timestamp <= now and (through is None or timestamp > through):
                occurrences.append(timestamp)
            first += timedelta(days=1)
        if definition["policy"] == "skip":
            return [t for t in occurrences if now - t <= 60]
        count = 1 if definition["policy"] == "run_once" else definition["catchup"]
        return occurrences[-count:]

    def tick(self, now):
        number(now)
        state, _ = self.store.read("calendar")
        created = []
        for rid, rule in sorted(state.get("rules", {}).items()):
            if not rule["enabled"] or rule["through"] is not None and now <= rule["through"]:
                continue
            definition = rule["definition"]
            for due in self._due(definition, rule["through"], now):
                gid = "scheduled-" + sha256(canonical([rid, due]).encode()).hexdigest()
                # Each occurrence gets a distinct entity scope so an old result
                # cannot satisfy a later recurrence. It is exposed in the goal.
                predicate = {**definition["predicate"], "entity": definition["predicate"]["entity"] + ":" + gid}
                self.goals.add(gid, predicate, budget=definition["budget"])
                with self.store.edit("calendar", gid) as current:
                    current.setdefault("occurrences", {})[gid] = dict(rule=rid, due=due, goal=gid)
                created.append(gid)
            # Crash before this checkpoint may retry additions, which are idempotent.
            with self.store.edit("calendar", "tick:" + rid) as current:
                current["rules"][rid]["through"] = now
        return created
