import asyncio
from dataclasses import replace
import random
import tempfile
from pathlib import Path
import unittest

from consciousness.attention import Attention
from consciousness.clock import ManualClock
from consciousness.domain import Decision, Result, Signal, StopReceipt
from consciousness.driver import SimulatedDriver
from consciousness.journal import Journal
from consciousness.runtime import Runtime


async def eventually(predicate, turns=300):
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached within event-loop turn budget")


class GatedStopDriver(SimulatedDriver):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.stop_requested = asyncio.Event()
        self.release_stop = asyncio.Event()
        self.stop_calls = 0

    async def stop(self, action):
        self.stop_calls += 1
        self.stop_requested.set()
        await self.release_stop.wait()
        return await super().stop(action)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.journal = Journal()
        self.driver = SimulatedDriver(duration=0)
        self.runtime = Runtime(self.journal, self.driver)
        await self.runtime.start()

    async def asyncTearDown(self):
        if hasattr(self.driver, "release_stop"):
            self.driver.release_stop.set()
        await self.runtime.close()
        # Test-only simulator cleanup. Do not infer a hardware stop from cancellation.
        for action in list(self.driver._active.values()):
            await self.driver.stop(action)
        await eventually(lambda: not self.runtime._background)
        self.driver.close()
        self.journal.close()

    async def test_roundtrip_and_durable_deduplication(self):
        signal = Signal("小智，你好")
        task = await self.runtime.submit(signal)
        self.assertEqual(await self.runtime.submit(signal), task)
        await self.runtime.drain()
        self.assertEqual(self.journal.task(task)["status"], "completed")
        self.assertEqual(len(self.driver.execution_order), 1)
        feedback = [e for e in self.journal.history() if e["kind"] == "feedback.recorded"]
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["trace_id"], signal.trace_id)

    async def test_priority_and_stable_ties(self):
        ids = [await self.runtime.submit(Signal(text)) for text in ["first", "second", "小智", "火警"]]
        await self.runtime.drain()
        order = [e["payload"]["task_id"] for e in self.journal.history() if e["kind"] == "action.prepared"]
        self.assertEqual(order, [ids[3], ids[2], ids[0], ids[1]])

    async def test_preemption_waits_for_physical_stop_and_coalesces_requests(self):
        self.driver.close()
        self.driver = GatedStopDriver(duration=100)
        self.runtime.driver = self.driver
        ordinary = await self.runtime.submit(Signal("slow"))
        await eventually(lambda: self.driver.active_count == 1)
        urgent = await self.runtime.submit(Signal("火警"))
        await eventually(self.driver.stop_requested.is_set)
        another = await self.runtime.submit(Signal("救命"))
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(self.journal.task(urgent)["status"], "queued")
        self.assertEqual(self.driver.stop_calls, 1)
        self.driver.duration = 0
        self.driver.release_stop.set()
        await self.runtime.drain()
        self.assertEqual(self.journal.task(ordinary)["status"], "cancelled")
        self.assertEqual(self.journal.task(urgent)["status"], "completed")
        self.assertEqual(self.journal.task(another)["status"], "completed")
        self.assertEqual(self.driver.max_active, 1)

    async def test_unknown_outcome_freezes_queue_until_reconciliation(self):
        clock = ManualClock()
        self.driver.close()
        self.driver = GatedStopDriver(duration=100, clock=clock)
        self.runtime.driver = self.driver
        self.runtime.clock = clock
        self.runtime.stop_timeout = 1
        ordinary = await self.runtime.submit(Signal("slow"))
        await eventually(lambda: self.driver.active_count == 1)
        urgent = await self.runtime.submit(Signal("火警"))
        await eventually(self.driver.stop_requested.is_set)
        for _ in range(10):
            await asyncio.sleep(0)
        clock.advance(2)
        summary = await self.runtime.drain()
        self.assertTrue(summary["blocked"])
        self.assertEqual(self.journal.task(ordinary)["status"], "outcome_unknown")
        self.assertEqual(self.journal.task(urgent)["status"], "queued")
        self.driver.duration = 0
        self.driver.release_stop.set()
        await eventually(lambda: self.driver.active_count == 0)
        await eventually(lambda: not self.runtime._background)
        # A late receipt alone does not silently unfreeze the scheduler.
        self.assertEqual(self.journal.task(urgent)["status"], "queued")
        await self.runtime.reconcile()
        await self.runtime.drain()
        self.assertEqual(self.journal.task(urgent)["status"], "completed")

    async def test_failed_action_does_not_block_independent_work(self):
        original = self.driver.execute
        async def fail_first(action):
            if action.arguments["text"] == "收到：bad":
                return Result(action.id, action.generation, action.epoch, "failed", "sim:known_failure")
            return await original(action)
        self.driver.execute = fail_first
        bad = await self.runtime.submit(Signal("bad"))
        good = await self.runtime.submit(Signal("good"))
        await self.runtime.drain()
        self.assertEqual(self.journal.task(bad)["status"], "failed")
        self.assertEqual(self.journal.task(good)["status"], "completed")

    async def test_wrong_epoch_result_cannot_release_slot(self):
        async def stale(action):
            return Result(action.id, action.generation, "old-epoch", "completed", "sim:receipt")
        self.driver.execute = stale
        first = await self.runtime.submit(Signal("first"))
        second = await self.runtime.submit(Signal("second"))
        await self.runtime.drain()
        self.assertEqual(self.journal.task(first)["status"], "outcome_unknown")
        self.assertEqual(self.journal.task(second)["status"], "queued")

    async def test_invalid_stop_receipt_keeps_slot_frozen(self):
        self.driver.duration = 100
        original = self.driver.stop
        async def bad_receipt(action):
            await original(action)
            return StopReceipt(action.id, action.generation, "wrong-epoch", True, "sim:stopped")
        self.driver.stop = bad_receipt
        first = await self.runtime.submit(Signal("slow"))
        await eventually(lambda: self.driver.active_count == 1)
        second = await self.runtime.submit(Signal("火警"))
        await self.runtime.drain()
        self.assertEqual(self.journal.task(first)["status"], "outcome_unknown")
        self.assertEqual(self.journal.task(second)["status"], "queued")

    async def test_action_deadline_stops_before_next_action(self):
        clock = ManualClock()
        self.driver.clock = clock
        self.driver.duration = 100
        self.runtime.clock = clock
        self.runtime.action_timeout = 1
        first = await self.runtime.submit(Signal("slow"))
        next_task = await self.runtime.submit(Signal("later"))
        await eventually(lambda: self.driver.active_count == 1)
        for _ in range(10):
            await asyncio.sleep(0)
        self.driver.duration = 0
        clock.advance(2)
        await self.runtime.drain()
        self.assertEqual(self.journal.task(first)["status"], "cancelled")
        self.assertEqual(self.journal.task(next_task)["status"], "completed")

    async def test_persistence_failure_requests_stop_and_halts_scheduler(self):
        self.driver.duration = 100
        first = await self.runtime.submit(Signal("slow"))
        await eventually(lambda: self.driver.active_count == 1)
        original = self.journal.transition
        def fail_transition(*args, **kwargs):
            raise OSError("disk full")
        self.journal.transition = fail_transition
        await self.runtime.submit(Signal("火警"))
        with self.assertRaises(OSError):
            await self.runtime.drain()
        self.assertEqual(self.driver.active_count, 0)
        self.assertEqual(len(self.driver.execution_order), 1)
        self.assertEqual(self.journal.task(first)["status"], "running")
        self.journal.transition = original
        # Restart is required after a fatal journal fault. Preserve old task state
        # and prove it is reconciled from the receipt on a new runtime instance.
        self.runtime._closed = True
        self.driver.duration = 0
        self.runtime = Runtime(self.journal, self.driver)
        await self.runtime.start()
        await self.runtime.drain()
        self.assertEqual(self.journal.task(first)["status"], "cancelled")

    async def test_driver_exception_is_unknown_not_safe_failure(self):
        async def uncertain(action):
            raise ConnectionError("lost receipt")
        self.driver.execute = uncertain
        task = await self.runtime.submit(Signal("first"))
        await self.runtime.drain()
        self.assertEqual(self.journal.task(task)["status"], "outcome_unknown")

    async def test_close_preserves_queued_work_and_rejects_submit(self):
        task = await self.runtime.submit(Signal("queued"))
        await self.runtime.close()
        self.assertEqual(self.journal.task(task)["status"], "queued")
        with self.assertRaises(RuntimeError):
            await self.runtime.submit(Signal("late"))

    async def test_1000_accepted_events_serialized(self):
        rng = random.Random(7331)
        self.runtime.max_pending = 1000
        for n in range(1000):
            await self.runtime.submit(Signal(("小智 " if rng.randrange(4) == 0 else "") + str(n)))
        summary = await self.runtime.drain()
        self.assertEqual(summary["counts"]["completed"], 1000)
        self.assertEqual(self.driver.max_active, 1)
        self.assertEqual(len(self.driver.execution_order), 1000)
        self.assertLessEqual(len(self.runtime.memory.snapshot().items), 32)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_action_recovers_without_send(self):
        journal = Journal()
        driver = SimulatedDriver(duration=0)
        signal = Signal("unsent")
        task, _ = journal.accept(signal, Attention().select(signal), 64)
        journal.claim("prior-epoch")
        runtime = Runtime(journal, driver)
        await runtime.start()
        self.assertEqual(journal.task(task["id"])["status"], "cancelled")
        self.assertEqual(driver.execution_order, [])
        await runtime.close()
        driver.close()
        journal.close()

    async def test_send_window_recovery_queries_receipt_without_replaying(self):
        with tempfile.TemporaryDirectory() as directory:
            jp = str(Path(directory) / "journal.sqlite3")
            dp = str(Path(directory) / "driver.sqlite3")
            journal, driver = Journal(jp), SimulatedDriver(path=dp, duration=0)
            signal = Signal("delivered")
            task, _ = journal.accept(signal, Attention().select(signal), 64)
            action = journal.claim("prior-epoch")
            journal.phase(action.id, "sending")
            await driver.execute(action)
            # Process dies before the driver's final receipt reaches the journal.
            driver.close()
            journal.close()
            journal, driver = Journal(jp), SimulatedDriver(path=dp, duration=0)
            runtime = Runtime(journal, driver)
            await runtime.start()
            await runtime.drain()
            self.assertEqual(journal.task(task["id"])["status"], "completed")
            self.assertEqual(driver.execution_order, [])
            await runtime.close()
            driver.close()
            journal.close()

    async def test_unconfirmed_send_after_restart_blocks_next_action(self):
        journal, driver = Journal(), SimulatedDriver(duration=0)
        signal = Signal("maybe sent")
        task, _ = journal.accept(signal, Attention().select(signal), 64)
        action = journal.claim("prior-epoch")
        journal.phase(action.id, "sending")
        runtime = Runtime(journal, driver)
        await runtime.start()
        second = await runtime.submit(Signal("next"))
        await runtime.drain()
        self.assertEqual(journal.task(task["id"])["status"], "outcome_unknown")
        self.assertEqual(journal.task(second)["status"], "queued")
        self.assertEqual(driver.execution_order, [])
        await runtime.close()
        driver.close()
        journal.close()
