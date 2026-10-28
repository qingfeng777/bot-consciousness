import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from consciousness.attention import Attention
from consciousness.clock import ManualClock
from consciousness.domain import CapacityExceeded, DuplicateConflict, InvalidInput, Signal
from consciousness.journal import Journal
from consciousness.replay import load_stream, replay_stream


class EnvelopeMigrationTests(unittest.TestCase):
    def test_event_times_source_and_robot_are_distinct(self):
        clock = ManualClock(100)
        journal = Journal(clock=clock, robot_id="robot-a")
        try:
            signal = Signal("hi", robot_id="robot-a", occurred_at=90, source="sensor")
            journal.accept(signal, Attention().select(signal), 10)
            event = journal.history()[0]
            self.assertEqual(event["robot_id"], "robot-a")
            self.assertEqual(event["source"], "sensor")
            self.assertEqual(event["occurred_at"], 90)
            self.assertEqual(event["received_at"], 100)
            self.assertEqual(event["type"], "input.accepted")
            other = Signal("hi", robot_id="robot-b")
            with self.assertRaises(InvalidInput):
                journal.accept(other, Attention().select(other), 10)
        finally:
            journal.close()

    def test_v1_upgrade_preserves_retry_identity_and_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "v1.sqlite3")
            db = sqlite3.connect(path)
            db.executescript('''
              CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
                trace_id TEXT NOT NULL,cause_id TEXT,kind TEXT NOT NULL,payload TEXT NOT NULL,
                created REAL NOT NULL,schema_version TEXT NOT NULL);
              CREATE TABLE tasks(id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL,
                trace_id TEXT NOT NULL,priority INTEGER NOT NULL,salience INTEGER NOT NULL,
                capability TEXT NOT NULL,arguments TEXT NOT NULL,status TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,enqueue_seq INTEGER NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(event_id));
              PRAGMA user_version=1;
            ''')
            signal = Signal("hi", event_id="old-input")
            db.execute("INSERT INTO events VALUES (1,?,?,?,?,?,?,?)", (
                signal.event_id, signal.trace_id, None, "input.accepted", '{"text":"hi","source":"user"}', 10, "1.0"))
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "old-task", signal.event_id, signal.fingerprint(), signal.trace_id, 2, 40,
                "say", '{"text":"hi"}', "queued", 0, 1, 0, "ordinary"))
            db.commit()
            db.close()
            journal = Journal(path)
            try:
                task, accepted = journal.accept(signal, Attention().select(signal), 10)
                self.assertFalse(accepted)
                self.assertEqual(task["id"], "old-task")
                self.assertEqual(task["enqueued_at"], 10)
                self.assertEqual(journal.usage()["schema_version"], 2)
                self.assertEqual(journal.history()[0]["source"], "legacy")
                self.assertIsNone(journal.history()[0]["occurred_at"])
                self.assertEqual(journal.claim("new-epoch").task_id, "old-task")
            finally:
                journal.close()

    def test_robot_binding_survives_restart_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "original.sqlite3")
            backup = str(Path(directory) / "copy.sqlite3")
            journal = Journal(path, robot_id="robot-a")
            signal = Signal("hi", robot_id="robot-a")
            journal.accept(signal, Attention().select(signal), 10)
            journal.backup(backup)
            with self.assertRaises(FileExistsError):
                journal.backup(backup)
            journal.close()
            with self.assertRaises(InvalidInput):
                Journal(path, robot_id="robot-b")
            restored = Journal(backup)
            try:
                self.assertEqual(restored.robot_id, "robot-a")
                self.assertEqual(len(restored.tasks()), 1)
                self.assertFalse(restored.accept(signal, Attention().select(signal), 10)[1])
            finally:
                restored.close()

    def test_capacity_configuration_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "journal.sqlite3")
            journal = Journal(path, max_tasks=12, max_events=400, aging_seconds=7)
            journal.close()
            reopened = Journal(path)
            self.assertEqual((reopened.max_tasks, reopened.max_events, reopened.aging_seconds), (12, 400, 7))
            reopened.close()
            changed = Journal(path, max_tasks=20)
            self.assertEqual((changed.max_tasks, changed.max_events), (20, 400))
            changed.close()

    def test_invalid_timestamps_are_rejected(self):
        for value in (-1, float("nan"), float("inf"), True, "1"):
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                Signal("hi", deadline_at=value).validate()


class SchedulingCapacityTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.journal = Journal(clock=self.clock, aging_seconds=10)

    def tearDown(self):
        self.journal.close()

    def accept(self, text, deadline_at=None):
        signal = Signal(text, deadline_at=deadline_at)
        return self.journal.accept(signal, Attention().select(signal), 64)[0]["id"]

    def test_aging_beats_new_named_inputs_but_never_emergencies(self):
        old = self.accept("old ordinary")
        self.clock.advance(20)
        named = self.accept("小智 new named")
        urgent = self.accept("火警")
        action = self.journal.claim("epoch")
        self.assertEqual(action.task_id, urgent)
        self.journal.finish(action, "completed", "sim:done")
        action = self.journal.claim("epoch")
        self.assertEqual(action.task_id, old)
        self.journal.finish(action, "completed", "sim:done")
        self.assertEqual(self.journal.claim("epoch").task_id, named)

    def test_deadline_orders_and_expiration_never_dispatches(self):
        late = self.accept("late", 10)
        soon = self.accept("soon", 5)
        expired = self.accept("expired", 0)
        action = self.journal.claim("epoch")
        self.assertEqual(action.task_id, soon)
        self.assertEqual(self.journal.task(expired)["status"], "cancelled")
        self.journal.finish(action, "completed", "sim:done")
        self.assertEqual(self.journal.claim("epoch").task_id, late)

    def test_wall_clock_change_does_not_age_new_tasks(self):
        original_now = self.clock.now
        old = self.accept("old", deadline_at=100)
        self.clock.advance(20)
        self.clock.now = lambda: original_now() + 1000
        new = self.accept("小智 new", deadline_at=1100)
        action = self.journal.claim("epoch")
        self.assertEqual(action.task_id, new)
        self.assertEqual(self.journal.task(old)["reason"], "dispatch_deadline_expired")
        self.assertEqual(self.journal.task(new)["enqueued_at"], 20)

    def test_task_capacity_rejects_new_inputs_but_preserves_retry_and_finish(self):
        self.journal.max_tasks = 1
        signal = Signal("hi")
        task, _ = self.journal.accept(signal, Attention().select(signal), 64)
        with self.assertRaises(CapacityExceeded):
            self.accept("new")
        self.assertFalse(self.journal.accept(signal, Attention().select(signal), 64)[1])
        action = self.journal.claim("epoch")
        self.journal.finish(action, "completed", "sim:done")
        self.assertEqual(self.journal.task(task["id"])["status"], "completed")

    def test_event_budget_reserves_finish_headroom(self):
        self.journal.max_events = 20
        first = self.accept("first")
        with self.assertRaises(CapacityExceeded):
            self.accept("second")
        action = self.journal.claim("epoch")
        self.journal.finish(action, "completed", "sim:done")
        self.assertEqual(self.journal.task(first)["status"], "completed")
        self.assertLessEqual(self.journal.usage()["events"], 20)


class TimedReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_duplicate_fails_before_starting_delayed_driver(self):
        records = [(0, Signal("first", event_id="same")),
                   (0.2, Signal("second", event_id="same"))]
        with self.assertRaises(DuplicateConflict):
            await asyncio.wait_for(replay_stream(records, stop_delay=10), 1)

    async def test_timed_replay_preemption_is_reproducible(self):
        records = load_stream(Path(__file__).resolve().parents[1] / "examples/preemption.jsonl")
        first = await replay_stream(records, duration=1, stop_delay=0.1)
        second = await replay_stream(records, duration=1, stop_delay=0.1)
        self.assertEqual(first, second)
        self.assertEqual([row["status"] for row in first["outcomes"]], ["cancelled", "completed", "completed"])
        self.assertEqual(first["max_active"], 1)
        self.assertAlmostEqual(first["virtual_seconds"], 2.3)

    async def test_unknown_stop_does_not_release_queue(self):
        records = [(0, Signal("slow")), (0.2, Signal("火警"))]
        result = await replay_stream(records, duration=100, stop_delay=10)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["counts"]["queued"], 1)
        self.assertEqual(result["counts"]["outcome_unknown"], 1)

    async def test_manual_clock_does_not_accumulate_cancelled_timers(self):
        clock = ManualClock()
        for _ in range(100):
            timer = asyncio.create_task(clock.sleep(100))
            await asyncio.sleep(0)
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        self.assertIsNone(clock.next_deadline())
        self.assertEqual(len(clock._sleepers), 0)

    async def test_invalid_or_unordered_stream_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            for content in ('{"id":"a","text":"x","at":NaN}',
                            '{"id":"a","text":"x","at":2}\n{"id":"b","text":"y","at":1}'):
                path.write_text(content)
                with self.assertRaises(InvalidInput):
                    load_stream(path)
