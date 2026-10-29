"""Run the configured cognitive modules together on a local simulated device."""
import asyncio
import hashlib
import json
from pathlib import Path

from .agency import Affect, Goals, Identity, Motivation, Scene, SelfState
from .calendar import Calendar
from .domain import InvalidInput, Signal, canonical
from .driver import SimulatedDriver
from .journal import Journal
from .knowledge import Knowledge
from .learning import Association, Learning, Reflexes
from .perception import Perception
from .planning import Skills, validate_plan
from .runtime import Runtime
from .state import StateStore, number


class CognitiveSystem:
    def __init__(self, journal, runtime):
        self.store, self.runtime = StateStore(journal), runtime
        self.knowledge = Knowledge(self.store)
        self.perception = Perception(self.store, self.knowledge)
        self.scene = Scene(self.store)
        self.telemetry = SelfState(self.store)
        self.goals = Goals(self.store, self.knowledge)
        self.calendar = Calendar(self.store, self.goals)
        self.affect = Affect(self.store)
        self.identity = Identity(self.store)
        self.motivation = Motivation(self.store, self.goals, self.telemetry)
        self.skills = Skills(self.store, self.goals, self.knowledge)
        self.learning = Learning(self.store)
        self.reflexes = Reflexes(self.store)
        self.association = Association(self.store)
        self._cloud = None

    async def infer(self, purpose, request_id, input, **kwargs):
        from .cloud_cognition import CloudCognition
        if self._cloud is None:
            self._cloud = CloudCognition(self.store)
        return await self._cloud.infer(purpose, request_id, input, **kwargs)

    async def handle(self, event):
        data = dict(event)
        operation, at = data.pop("op"), data.pop("at")
        if operation == "input":
            signal = Signal(data["text"], event_id=data["id"], robot_id=self.store.journal.robot_id)
            task = await self.runtime.submit(signal)
            await self.runtime.drain()
            return self.runtime.journal.task(task)["status"]
        if operation == "observe":
            data["content"] = data["content"].encode("utf-8")
            return self.perception.ingest(at=at, **data)
        if operation == "claim":
            return self.knowledge.claim(**data)
        if operation == "rebuild_index":
            return self.knowledge.rebuild_index()
        if operation == "retrieve":
            result = self.knowledge.ranked(at=at, **data)
            with self.store.edit("retrieval_queries", "query") as state:
                state["last"] = dict(query=data, at=at, hits=result)
            return result
        if operation == "resolve":
            return self.knowledge.resolve(**data)
        if operation == "withdraw":
            return self.knowledge.withdraw(**data)
        if operation == "scene":
            return self.scene.observe(at=at, **data)
        if operation == "telemetry":
            self.telemetry.sample(at=at, **data)
            return self.motivation.tick(at)
        if operation == "goal":
            return self.goals.add(**data)
        if operation == "calendar":
            data.setdefault("start", at)
            return self.calendar.configure(**data)
        if operation == "calendar_tick":
            return self.calendar.tick(at)
        if operation == "evaluate":
            return self.goals.evaluate(at)
        if operation == "affect":
            return self.affect.apply(at=at, **data)
        if operation == "role":
            return self.identity.configure(**data)
        if operation == "skill":
            return self.skills.register(**data)
        if operation == "run_skill":
            return await self.skills.run(runtime=self.runtime, at=at, **data)
        if operation == "model":
            return await self.infer("plan", data["request_id"], data["input"],
                                    scope=data.get("scope", "scenario"), generation=data.get("generation", 0),
                                    evidence_ids=data.get("evidence_ids"))
        if operation == "cloud":
            return await self.infer(**data)
        if operation == "fixture_model":
            # Explicit offline regression fixture; never a fallback for cloud failures.
            candidate = validate_plan(data["candidate"])
            self.store.journal.record("model.fixture", {"goal": candidate["goal"]})
            return candidate
        if operation == "run_cloud_plan":
            from .cloud_cognition import CloudCognition
            ledger = self.store.read("cloud_requests")[0]
            record = ledger.get("requests", {}).get(data["request_id"])
            revisions = {name: self.store.read(name)[1] for name in CloudCognition.WATCHED}
            if (not record or record["status"] != "accepted" or record["purpose"] != "plan"
                    or record["result"]["status"] != "ok" or record["revisions"] != revisions
                    or ledger["generations"].get(record["generation_key"]) != record["generation"]):
                raise InvalidInput("cloud plan unavailable or stale")
            key = self.skills.register(data["skill"], data["version"], record["result"]["data"])
            return await self.skills.run(key, data["run_id"], self.runtime, at=at)
        if operation == "experience":
            return self.learning.experience(**data)
        if operation == "learn":
            return self.learning.propose(**data)
        if operation == "validate_learning":
            return self.learning.evaluate(**data)
        if operation == "publish_learning":
            return self.learning.publish(skills=self.skills, **data)
        if operation == "retire_learning":
            return self.learning.retire(skills=self.skills, **data)
        if operation == "reflex":
            return self.reflexes.configure(**data)
        if operation == "react":
            result = await self.reflexes.react(at=at, runtime=self.runtime, **data)
            await self.runtime.drain()
            return result
        if operation == "associate":
            return self.association.propose(**data)
        raise InvalidInput("unsupported simulation operation: " + str(operation))

    def summary(self, at):
        goals = self.goals.evaluate(at)
        modules = {row["name"]: row["revision"] for row in self.store.journal._db.execute(
            "SELECT name,revision FROM module_state ORDER BY name")}
        return dict(simulation=True, modules=modules, goals=goals,
                    scene=self.scene.snapshot(), self_state=self.telemetry.snapshot(at),
                    affect=self.affect.snapshot(at), identity=self.identity.snapshot(at),
                    conflicts=self.knowledge.conflicts(), facts=self.knowledge.search(at=at, limit=100),
                    runtime=self.runtime.summary())


def load_cognitive_scenario(path):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    if not 1 <= len(rows) <= 10000:
        raise InvalidInput("scenario must contain 1..10000 events")
    last = 0
    for row in rows:
        if not isinstance(row, dict) or "op" not in row or "at" not in row:
            raise InvalidInput("scenario event requires op and at")
        number(row["at"])
        if row["at"] < last:
            raise InvalidInput("scenario timestamps must be ordered")
        last = row["at"]
    return rows


async def simulate(path, data_dir=None):
    rows = load_cognitive_scenario(path)
    root = Path(data_dir) if data_dir else None
    if root:
        if (root / "INCOMPLETE").exists():
            raise InvalidInput("incomplete backup cannot be simulated")
        root.mkdir(parents=True, exist_ok=True)
    journal = Journal(str(root / "journal.sqlite3") if root else ":memory:")
    driver = SimulatedDriver(path=str(root / "simulator.sqlite3") if root else ":memory:", duration=0.001)
    runtime = Runtime(journal, driver)
    try:
        system = CognitiveSystem(journal, runtime)
        digest = hashlib.sha256(canonical(rows).encode()).hexdigest()
        progress, _ = system.store.read("simulation")
        if progress and progress["scenario"] != digest:
            raise InvalidInput("data directory belongs to a different scenario; use a fresh directory")
        await runtime.start()
        if not progress:
            with system.store.edit("simulation", "scenario-start") as state:
                state.update(scenario=digest, completed=0)
        for index, row in enumerate(rows):
            if index < progress.get("completed", 0):
                continue
            await system.handle(row)
            with system.store.edit("simulation", "event-" + str(index)) as state:
                state.update(scenario=digest, completed=index + 1)
        result = system.summary(rows[-1]["at"])
        result.update(scenario_sha256=digest, events=len(rows), max_external_concurrency=driver.max_active)
        return result
    finally:
        await runtime.close()
        if not driver.active_count:
            driver.close()
        journal.close()
