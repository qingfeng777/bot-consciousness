"""Recorded inputs replay only into a fresh simulated runtime."""
import asyncio
import hashlib
import json

from .domain import InvalidInput, Signal, canonical
from .driver import SimulatedDriver
from .journal import Journal
from .runtime import Runtime


def load_scenario(path):
    records = []
    with open(path, encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or set(record) - {"id", "text", "source"}:
                    raise ValueError("allowed fields: id, text, source")
                signal = Signal(record["text"], event_id=record["id"],
                                trace_id="replay:" + record["id"], source=record.get("source", "user"))
                signal.validate()
            except (ValueError, TypeError, KeyError, InvalidInput) as exc:
                raise InvalidInput(f"invalid scenario line {number}: {exc}") from exc
            records.append(signal)
    if not records or len(records) > 10000:
        raise InvalidInput("scenario must contain 1..10000 signals")
    return records


async def replay(signals):
    """Batch semantics: all recorded signals arrive before action dispatch.

    Reproducibility hash excludes generated UUIDs and wall-clock timestamps.
    Timed interruptions are validated separately with a manual clock in tests.
    """
    journal = Journal()
    driver = SimulatedDriver(duration=0)
    runtime = Runtime(journal, driver, max_pending=max(1, len(signals)))
    try:
        await runtime.start()
        for signal in signals:
            await runtime.submit(signal)
        summary = await runtime.drain()
        mapping = {task["id"]: task["event_id"] for task in summary["tasks"]}
        rows = []
        cursor = 0
        while True:
            events = journal.history(cursor)
            if not events:
                break
            for event in events:
                if event["kind"] == "action.result":
                    payload = event["payload"]
                    rows.append({"input_id": mapping[payload["task_id"]],
                                 "status": payload["status"], "detail": payload["detail"]})
            cursor = events[-1]["seq"]
        digest = hashlib.sha256(canonical(rows).encode()).hexdigest()
        return {"simulation": True, "mode": "batch", "outcomes": rows,
                "counts": summary["counts"], "outcome_sha256": digest,
                "max_active": driver.max_active}
    finally:
        await runtime.close()
        driver.close()
        journal.close()
