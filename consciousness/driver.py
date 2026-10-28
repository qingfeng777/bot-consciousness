"""Driver protocol and an explicitly simulated, queryable executor."""
import asyncio
from dataclasses import asdict
import json
import math
import sqlite3
from typing import Protocol

from .clock import RealClock
from .domain import Action, Result, StopReceipt, canonical


class Driver(Protocol):
    simulation: bool

    async def execute(self, action: Action) -> Result: ...
    async def stop(self, action: Action) -> StopReceipt: ...
    async def query(self, action: Action) -> Result | None: ...


class SimulatedDriver:
    """No real text/audio/hardware effect. Optional DB preserves final receipts.

    An interrupted simulator process leaves an unresolved record, not an
    invented success. A missing record is also unknown after the send window.
    """
    simulation = True
    capabilities = {"say", "alert"}

    def __init__(self, *, clock=None, duration=0.01, stop_delay=0.0, path=":memory:"):
        if any(not math.isfinite(value) or value < 0 for value in (duration, stop_delay)):
            raise ValueError("durations must be finite and nonnegative")
        self.clock = clock or RealClock()
        self.duration, self.stop_delay = duration, stop_delay
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts ("
                        "key TEXT PRIMARY KEY, action TEXT NOT NULL, result TEXT)")
        self.db.commit()
        self._active = {}
        self._stops = {}
        self._done = {}
        self.max_active = 0
        self.execution_order = []

    @property
    def active_count(self):
        return len(self._active)

    def _validate(self, action):
        return (action.capability in self.capabilities and set(action.arguments) == {"text"}
                and isinstance(action.arguments["text"], str)
                and 0 < len(action.arguments["text"].encode("utf-8")) <= 20000)

    def _read(self, action):
        row = self.db.execute("SELECT action,result FROM receipts WHERE key=?",
                              (action.idempotency_key,)).fetchone()
        if row is None:
            return None
        if row[0] != canonical(action.to_dict()):
            raise ValueError("idempotency key reused for a different action")
        return Result(**json.loads(row[1])) if row[1] else None

    def _save(self, action, result):
        with self.db:
            self.db.execute("UPDATE receipts SET result=? WHERE key=?",
                            (canonical(asdict(result)), action.idempotency_key))

    async def execute(self, action):
        cached = self._read(action)
        if cached:
            return cached
        if action.id in self._active:
            await self._done[action.id].wait()
            return self._read(action)
        if self.db.execute("SELECT 1 FROM receipts WHERE key=?", (action.idempotency_key,)).fetchone():
            raise RuntimeError("unresolved prior simulator execution; refusing to replay")
        with self.db:
            self.db.execute("INSERT INTO receipts VALUES (?,?,NULL)",
                            (action.idempotency_key, canonical(action.to_dict())))
        if not self._validate(action):
            result = Result(action.id, action.generation, action.epoch, "failed",
                            "sim:validation_rejected", "unsupported capability or arguments")
            self._save(action, result)
            return result
        if self._active:
            result = Result(action.id, action.generation, action.epoch, "failed",
                            "sim:slot_busy", "simulator rejects concurrent ordinary actions")
            self._save(action, result)
            return result
        stop = asyncio.Event()
        done = asyncio.Event()
        self._active[action.id] = action
        self._stops[action.id] = stop
        self._done[action.id] = done
        self.max_active = max(self.max_active, len(self._active))
        self.execution_order.append(action.id)
        timer = asyncio.create_task(self.clock.sleep(self.duration))
        stopping = asyncio.create_task(stop.wait())
        try:
            ready, _ = await asyncio.wait([timer, stopping], return_when=asyncio.FIRST_COMPLETED)
            if stopping in ready:
                await self.clock.sleep(self.stop_delay)
                result = Result(action.id, action.generation, action.epoch, "cancelled",
                                "sim:stopped:" + action.id)
            else:
                result = Result(action.id, action.generation, action.epoch, "completed",
                                "sim:completed:" + action.id, action.arguments["text"])
            self._save(action, result)
            return result
        finally:
            timer.cancel()
            stopping.cancel()
            await asyncio.gather(timer, stopping, return_exceptions=True)
            self._active.pop(action.id, None)
            self._stops.pop(action.id, None)
            done.set()
            self._done.pop(action.id, None)

    async def stop(self, action):
        if action.id in self._active:
            self._stops[action.id].set()
            await self._done[action.id].wait()
        result = self._read(action)
        return StopReceipt(action.id, action.generation, action.epoch,
                           bool(result), result.evidence if result else "")

    async def query(self, action):
        return self._read(action)

    def close(self):
        if self._active:
            raise RuntimeError("cannot close a driver with active actions")
        self.db.close()
