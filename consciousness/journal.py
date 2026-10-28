"""SQLite journal, atomic state transitions and resumable read projections.

One process owns a writable journal. The OS lock is released after a crash;
this is a single-host contract, not a distributed lease or hardware fence.
"""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3

from .clock import RealClock
from .domain import (Action, Backpressure, Decision, DuplicateConflict, RevisionConflict,
                     Signal, Status, StoreBusy, TERMINAL, TRANSITIONS, canonical, new_id)


SCHEMA_VERSION = 1


class Journal:
    def __init__(self, path=":memory:", *, clock=None):
        self.clock = clock or RealClock()
        self._lockfile = None
        self._db = None
        if str(path) != ":memory:":
            path = str(Path(path).resolve())
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._lockfile = open(path + ".lock", "a+")
            try:
                fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._lockfile.close()
                raise StoreBusy("another runtime owns this journal") from None
        try:
            self._db = sqlite3.connect(path)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError(f"unsupported journal schema {version}")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    trace_id TEXT NOT NULL, cause_id TEXT, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL, schema_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                    fingerprint TEXT NOT NULL, trace_id TEXT NOT NULL,
                    priority INTEGER NOT NULL, salience INTEGER NOT NULL,
                    capability TEXT NOT NULL, arguments TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    enqueue_seq INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL, FOREIGN KEY(event_id) REFERENCES events(event_id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL,
                    generation INTEGER NOT NULL, epoch TEXT NOT NULL,
                    capability TEXT NOT NULL, arguments TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL, trace_id TEXT NOT NULL,
                    phase TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS consumer_offsets (
                    name TEXT PRIMARY KEY, seq INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projections (
                    consumer TEXT NOT NULL, event_id TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(consumer,event_id)
                );
                CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,priority,salience,enqueue_seq);
                PRAGMA user_version=1;
            """)
        except BaseException:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _event(self, kind, payload, trace_id, cause_id=None, event_id=None):
        eid = event_id or new_id()
        cursor = self._db.execute(
            "INSERT INTO events(event_id,trace_id,cause_id,kind,payload,created,schema_version) "
            "VALUES (?,?,?,?,?,?,?)",
            (eid, trace_id, cause_id, kind, canonical(payload), self.clock.now(), "1.0"))
        return cursor.lastrowid

    def record(self, kind, payload, trace_id="runtime", cause_id=None):
        with self.transaction():
            return self._event(kind, payload, trace_id, cause_id)

    def accept(self, signal: Signal, decision: Decision, max_pending: int):
        signal.validate()
        with self.transaction():
            duplicate = self._db.execute("SELECT * FROM tasks WHERE event_id=?", (signal.event_id,)).fetchone()
            if duplicate:
                if duplicate["fingerprint"] != signal.fingerprint():
                    raise DuplicateConflict("event ID was already accepted with different content")
                return dict(duplicate), False
            count = self._db.execute("SELECT count(*) FROM tasks WHERE status='queued'").fetchone()[0]
            if count >= max_pending:
                worst = self._db.execute(
                    "SELECT * FROM tasks WHERE status='queued' "
                    "ORDER BY priority DESC,salience ASC,enqueue_seq DESC LIMIT 1").fetchone()
                if decision.priority != 0 or worst["priority"] == 0:
                    raise Backpressure("pending queue is full")
                self._transition(worst["id"], Status.CANCELLED.value, "emergency_queue_eviction")
            task_id = new_id()
            seq = self._event("input.accepted", {"text": signal.text, "source": signal.source},
                              signal.trace_id, event_id=signal.event_id)
            self._db.execute(
                "INSERT INTO tasks(id,event_id,fingerprint,trace_id,priority,salience,capability,"
                "arguments,status,enqueue_seq,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, signal.event_id, signal.fingerprint(), signal.trace_id,
                 decision.priority, decision.salience, decision.capability,
                 canonical(decision.arguments), "queued", seq, decision.reason))
            self._event("task.queued", {"task_id": task_id, "priority": decision.priority,
                                       "reason": decision.reason}, signal.trace_id, signal.event_id)
            return self.task(task_id), True

    def task(self, task_id):
        row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    def tasks(self):
        return [dict(r) for r in self._db.execute("SELECT * FROM tasks ORDER BY enqueue_seq")]

    def _transition(self, task_id, status, reason, expected_revision=None):
        old = self.task(task_id)
        if expected_revision is not None and old["revision"] != expected_revision:
            raise RevisionConflict("task revision changed")
        if status not in TRANSITIONS.get(old["status"], set()):
            raise RevisionConflict(f"invalid task transition {old['status']} -> {status}")
        self._db.execute("UPDATE tasks SET status=?,revision=revision+1,reason=? WHERE id=?",
                         (status, reason, task_id))
        self._event("task." + status, {"task_id": task_id, "reason": reason,
                                      "revision": old["revision"] + 1}, old["trace_id"], old["event_id"])

    def transition(self, task_id, status, reason, expected_revision=None):
        with self.transaction():
            self._transition(task_id, status, reason, expected_revision)

    def claim(self, epoch):
        with self.transaction():
            if self._db.execute("SELECT 1 FROM tasks WHERE status IN "
                                "('running','waiting_stop','outcome_unknown') LIMIT 1").fetchone():
                return None
            row = self._db.execute("SELECT * FROM tasks WHERE status='queued' "
                                   "ORDER BY priority ASC,salience DESC,enqueue_seq ASC LIMIT 1").fetchone()
            if row is None:
                return None
            generation = row["generation"] + 1
            action = Action(new_id(), row["id"], generation, epoch, row["capability"],
                            json.loads(row["arguments"]), new_id(), row["trace_id"])
            self._transition(row["id"], "running", "slot_acquired")
            self._db.execute("UPDATE tasks SET generation=? WHERE id=?", (generation, row["id"]))
            self._db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?)",
                             (action.id, action.task_id, action.generation, action.epoch,
                              action.capability, canonical(action.arguments), action.idempotency_key,
                              action.trace_id, "prepared"))
            self._event("action.prepared", action.to_dict(), row["trace_id"], row["event_id"])
            self._event("focus.changed", {"task_id": row["id"], "focus_version": action.id}, row["trace_id"])
            return action

    def phase(self, action_id, phase):
        with self.transaction():
            row = self._db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise KeyError(action_id)
            if row["phase"] != "prepared" or phase != "sending":
                raise RevisionConflict("invalid action phase transition")
            self._db.execute("UPDATE actions SET phase=? WHERE id=?", (phase, action_id))
            self._event("action.sending", {"action_id": action_id}, row["trace_id"])

    def unresolved(self):
        rows = self._db.execute("SELECT actions.* FROM actions JOIN tasks ON tasks.id=actions.task_id "
                                "WHERE tasks.status IN ('running','waiting_stop','outcome_unknown')")
        result = []
        for row in rows:
            data = dict(row)
            phase = data.pop("phase")
            data["arguments"] = json.loads(data["arguments"])
            result.append((Action(**data), phase))
        return result

    def finish(self, action, status, evidence, detail=""):
        if status not in TERMINAL or not evidence:
            raise ValueError("terminal state requires evidence")
        with self.transaction():
            self._transition(action.task_id, status, detail or "driver_result")
            self._db.execute("UPDATE actions SET phase=? WHERE id=?", (status, action.id))
            payload = {"action_id": action.id, "task_id": action.task_id,
                       "generation": action.generation, "epoch": action.epoch,
                       "status": status, "evidence": evidence, "detail": detail}
            self._event("action.result", payload, action.trace_id)
            self._event("feedback.recorded", payload, action.trace_id)

    def history(self, after=0, limit=1000):
        rows = self._db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))
        result = []
        for row in rows:
            data = dict(row)
            data["payload"] = json.loads(data["payload"])
            result.append(data)
        return result

    def project(self, consumer, transform, limit=100):
        """Pure transform -> JSON, committed atomically with its checkpoint.

        transform must not perform external effects. Returning None skips a row.
        Exceptions roll back the entire batch, allowing safe retry.
        """
        with self.transaction():
            checkpoint = self._db.execute("SELECT seq FROM consumer_offsets WHERE name=?", (consumer,)).fetchone()
            rows = self.history(checkpoint[0] if checkpoint else 0, limit)
            for event in rows:
                value = transform(event)
                if value is not None:
                    self._db.execute("INSERT OR IGNORE INTO projections VALUES (?,?,?)",
                                     (consumer, event["event_id"], canonical(value)))
            if rows:
                self._db.execute("INSERT INTO consumer_offsets VALUES (?,?) ON CONFLICT(name) "
                                 "DO UPDATE SET seq=excluded.seq", (consumer, rows[-1]["seq"]))
            return len(rows)

    def projection(self, consumer):
        return [json.loads(row[0]) for row in self._db.execute(
            "SELECT value FROM projections WHERE consumer=? ORDER BY rowid", (consumer,))]

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        if self._lockfile is not None:
            self._lockfile.close()
            self._lockfile = None
