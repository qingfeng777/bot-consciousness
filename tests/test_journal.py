import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from consciousness.attention import Attention
from consciousness.domain import (Backpressure, DuplicateConflict, RevisionConflict,
                                   Signal, StoreBusy)
from consciousness.journal import Journal


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.attention = Attention()

    def tearDown(self):
        self.journal.close()

    def accept(self, text="hello", event_id=None, limit=64):
        signal = Signal(text, **({"event_id": event_id} if event_id else {}))
        return self.journal.accept(signal, self.attention.select(signal), limit)

    def test_retry_is_durable_and_content_conflict_is_rejected(self):
        first, fresh = self.accept(event_id="message-1")
        self.assertTrue(fresh)
        again, fresh = self.accept(event_id="message-1")
        self.assertFalse(fresh)
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(DuplicateConflict):
            self.accept("different", event_id="message-1")
        self.assertEqual(len(self.journal.tasks()), 1)

    def test_emergency_admission_and_bounded_queue(self):
        normal, _ = self.accept(limit=1)
        with self.assertRaises(Backpressure):
            self.accept("more", limit=1)
        urgent, _ = self.accept("火警", limit=1)
        self.assertEqual(self.journal.task(normal["id"])["status"], "cancelled")
        with self.assertRaises(Backpressure):
            self.accept("救命", limit=1)
        self.assertEqual(self.journal.claim("epoch").task_id, urgent["id"])

    def test_transaction_rolls_back_events_and_queue_eviction(self):
        normal, _ = self.accept(limit=1)
        original = self.journal._event
        def fail(kind, *args, **kwargs):
            if kind == "input.accepted":
                raise OSError("disk unavailable")
            return original(kind, *args, **kwargs)
        self.journal._event = fail
        with self.assertRaises(OSError):
            self.accept("火警", limit=1)
        self.assertEqual(self.journal.task(normal["id"])["status"], "queued")
        self.assertFalse(any(e["kind"] == "task.cancelled" for e in self.journal.history()))

    def test_single_slot_and_revision_guards(self):
        task, _ = self.accept()
        self.accept("next")
        self.assertIsNotNone(self.journal.claim("epoch"))
        self.assertIsNone(self.journal.claim("epoch"))
        with self.assertRaises(RevisionConflict):
            self.journal.transition(task["id"], "cancelled", "test", expected_revision=0)
        self.journal.transition(task["id"], "cancelled", "test", expected_revision=1)
        with self.assertRaises(RevisionConflict):
            self.journal.transition(task["id"], "running", "late event")

    def test_projection_and_checkpoint_are_atomic(self):
        self.accept()
        def broken(event):
            if event["kind"] == "task.queued":
                raise RuntimeError("consumer crash")
            return event["payload"]
        with self.assertRaises(RuntimeError):
            self.journal.project("audit", broken)
        self.assertEqual(self.journal.projection("audit"), [])
        self.assertEqual(self.journal.project("audit", lambda event: event["payload"]), 2)
        self.assertEqual(self.journal.project("audit", lambda event: event["payload"]), 0)
        self.assertEqual(len(self.journal.projection("audit")), 2)

    def test_persistence_restart_and_exclusive_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "journal.sqlite3")
            journal = Journal(path)
            signal = Signal("hello", event_id="durable")
            task, _ = journal.accept(signal, self.attention.select(signal), 64)
            with self.assertRaises(StoreBusy):
                Journal(path)
            journal.close()
            reopened = Journal(path)
            try:
                self.assertEqual(reopened.task(task["id"])["status"], "queued")
                self.assertFalse(reopened.accept(signal, self.attention.select(signal), 64)[1])
            finally:
                reopened.close()

    def test_unknown_schema_fails_without_rewriting_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "future.sqlite3")
            db = sqlite3.connect(path)
            db.execute("PRAGMA user_version=99")
            db.close()
            with self.assertRaises(ValueError):
                Journal(path)
            db = sqlite3.connect(path)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)
            db.close()
