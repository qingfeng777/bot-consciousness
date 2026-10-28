"""Recorded inputs replay only into a fresh simulated runtime."""
import asyncio
import hashlib
import json
import math

from .domain import DuplicateConflict, InvalidInput, Signal, canonical
from .driver import SimulatedDriver
from .clock import ManualClock
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


def load_stream(path):
    """Ordered relative arrival times. Occurrence time is independent of arrival."""
    records = []
    previous = -1.0
    with open(path, encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or set(row) - {"id", "text", "source", "at", "deadline_at"}:
                    raise ValueError("allowed fields: id, text, source, at, deadline_at")
                at = row["at"]
                if type(at) not in (int, float) or not math.isfinite(at) or at < 0 or at < previous:
                    raise ValueError("arrival times must be finite, nonnegative and ordered")
                signal = Signal(row["text"], event_id=row["id"], trace_id="replay:" + row["id"],
                                source=row.get("source", "user"), deadline_at=row.get("deadline_at"))
                signal.validate()
                records.append((float(at), signal))
                previous = at
            except (ValueError, TypeError, KeyError, InvalidInput) as exc:
                raise InvalidInput(f"invalid timed scenario line {number}: {exc}") from exc
            if len(records) > 10000:
                raise InvalidInput("scenario exceeds 10000 inputs")
    if not records:
        raise InvalidInput("scenario is empty")
    return records


async def settle(turns=64):
    """Drain bounded local callback chains before advancing virtual time.

    Only the in-process simulator participates; no external I/O is permitted.
    A missing wakeup with a running worker is an error, never a real-time sleep.
    """
    for _ in range(turns):
        await asyncio.sleep(0)


async def replay_stream(records, *, duration=1.0, stop_delay=0.0):
    if not 1 <= len(records) <= 10000:
        raise InvalidInput("stream must contain 1..10000 inputs")
    fingerprints = {}
    previous = -1.0
    for at, signal in records:
        if type(at) not in (float, int) or not math.isfinite(at) or at < 0 or at < previous:
            raise InvalidInput("arrival times must be finite, nonnegative and ordered")
        signal.validate()
        if signal.robot_id != "robot-demo":
            raise InvalidInput("replay uses the isolated robot-demo identity")
        fingerprint = signal.fingerprint()
        if signal.event_id in fingerprints and fingerprints[signal.event_id] != fingerprint:
            raise DuplicateConflict("scenario reuses an event ID with different content")
        fingerprints[signal.event_id] = fingerprint
        previous = at
    clock = ManualClock()
    journal = Journal(clock=clock)
    driver = SimulatedDriver(clock=clock, duration=duration, stop_delay=stop_delay)
    runtime = Runtime(journal, driver, clock=clock, max_pending=max(1, len(records)))
    position = 0
    steps = 0
    try:
        await runtime.start()
        while True:
            while position < len(records) and records[position][0] <= clock.now():
                await runtime.submit(records[position][1])
                position += 1
            await settle()
            steps += 1
            if steps > 100000:
                raise RuntimeError("replay exceeded transition budget")
            next_input = records[position][0] if position < len(records) else None
            wakeup = clock.next_deadline()
            if next_input is None and wakeup is None:
                if runtime._worker and not runtime._worker.done():
                    raise RuntimeError("simulated worker stalled without a wakeup")
                break
            candidates = [value for value in (next_input, wakeup) if value is not None]
            clock.advance(max(0, min(candidates) - clock.now()))
        summary = await runtime.drain()
        task_inputs = {t["id"]: t["event_id"] for t in summary["tasks"]}
        outcomes = []
        cursor = 0
        while True:
            events = journal.history(cursor)
            if not events:
                break
            for event in events:
                if event["kind"] == "action.result":
                    payload = event["payload"]
                    outcomes.append({"input_id": task_inputs[payload["task_id"]],
                                     "status": payload["status"], "at": event["created"]})
            cursor = events[-1]["seq"]
        return {"simulation": True, "mode": "timed", "virtual_seconds": clock.now(),
                "outcomes": outcomes, "counts": summary["counts"], "blocked": summary["blocked"],
                "max_active": driver.max_active,
                "outcome_sha256": hashlib.sha256(canonical(outcomes).encode()).hexdigest()}
    finally:
        await runtime.close()
        driver.close()
        journal.close()
