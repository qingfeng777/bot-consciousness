"""SQLite journal, atomic state transitions and resumable read projections.

One process owns a writable journal. The OS lock is released after a crash;
this is a single-host contract, not a distributed lease or hardware fence.
"""
from contextlib import contextmanager
import fcntl
import json
import math
from pathlib import Path
import sqlite3

from .clock import RealClock
from .domain import (Action, Backpressure, CapacityExceeded, Decision, DuplicateConflict,
                     InvalidInput, RevisionConflict, Signal, Status, StoreBusy,
                     TERMINAL, TRANSITIONS, canonical, new_id, positive_number)


SCHEMA_VERSION = 2


class Journal:
    def __init__(self, path=":memory:", *, clock=None, robot_id=None,
                 max_tasks=None, max_events=None, aging_seconds=None):
        self.clock = clock or RealClock()
        if aging_seconds is not None:
            positive_number(aging_seconds, "aging_seconds")
        for value in (max_tasks, max_events):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("capacity limits must be positive integers")
        if robot_id is not None and (not isinstance(robot_id, str) or not 1 <= len(robot_id) <= 128):
            raise ValueError("robot_id must contain 1..128 characters")
        self._requested_limits = {"max_tasks": max_tasks, "max_events": max_events, "aging_seconds": aging_seconds}
        self.max_tasks = max_tasks if max_tasks is not None else 100000
        self.max_events = max_events if max_events is not None else 2000000
        self.aging_seconds = aging_seconds if aging_seconds is not None else 30.0
        self._wall_origin, self._mono_origin = self.clock.now(), self.clock.monotonic()
        self.path = str(path)
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
            if version not in (0, 1, SCHEMA_VERSION):
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
            """)
            self._migrate(version, robot_id)
        except BaseException:
            self.close()
            raise

    def _migrate(self, version, requested_robot):
        """Transactional, additive v1 -> v2 upgrade. Old rows/IDs remain intact."""
        with self.transaction():
            self._db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            stored = self._db.execute("SELECT value FROM metadata WHERE key='robot_id'").fetchone()
            self.robot_id = stored[0] if stored else (requested_robot or "robot-demo")
            if requested_robot is not None and self.robot_id != requested_robot:
                raise InvalidInput("journal belongs to a different robot")
            if not stored:
                self._db.execute("INSERT INTO metadata VALUES ('robot_id',?)", (self.robot_id,))
            for name, requested in self._requested_limits.items():
                stored_limit = self._db.execute("SELECT value FROM metadata WHERE key=?", (name,)).fetchone()
                value = (requested if requested is not None else
                         json.loads(stored_limit[0]) if stored_limit else getattr(self, name))
                if name == "aging_seconds":
                    positive_number(value, name)
                elif type(value) is not int or value < 1:
                    raise ValueError("invalid persisted capacity configuration")
                setattr(self, name, value)
                self._db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) "
                                 "DO UPDATE SET value=excluded.value", (name, canonical(value)))
            if version < 2:
                for name, declaration in (("robot_id", "TEXT"), ("source", "TEXT"),
                                          ("occurred_at", "REAL"), ("received_at", "REAL")):
                    self._db.execute(f"ALTER TABLE events ADD COLUMN {name} {declaration}")
                self._db.execute("UPDATE events SET robot_id=?,source='legacy',occurred_at=NULL,received_at=created",
                                 (self.robot_id,))
                self._db.execute("ALTER TABLE tasks ADD COLUMN enqueued_at REAL")
                self._db.execute("ALTER TABLE tasks ADD COLUMN deadline_at REAL")
                self._db.execute("UPDATE tasks SET enqueued_at=(SELECT created FROM events WHERE events.seq=tasks.enqueue_seq)")
            self._db.execute("PRAGMA user_version=2")

    def schedule_now(self):
        # Wall-clock changes during a run cannot create negative waiting times.
        return self._wall_origin + self.clock.monotonic() - self._mono_origin

    def usage(self):
        return {"tasks": self._db.execute("SELECT count(*) FROM tasks").fetchone()[0],
                "events": self._db.execute("SELECT count(*) FROM events").fetchone()[0],
                "max_tasks": self.max_tasks, "max_events": self.max_events,
                "robot_id": self.robot_id, "schema_version": SCHEMA_VERSION,
                "database_pages": self._db.execute("PRAGMA page_count").fetchone()[0],
                "page_size": self._db.execute("PRAGMA page_size").fetchone()[0]}

    def backup(self, destination):
        """Consistent SQLite snapshot, never overwrite an existing user file."""
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass
        try:
            target = sqlite3.connect(path)
            try:
                self._db.backup(target)
                result = target.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError("backup integrity check failed")
            finally:
                target.close()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"path": str(path.resolve()), "schema_version": SCHEMA_VERSION,
                "robot_id": self.robot_id, "integrity": "ok"}

    @contextmanager
    def transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _event(self, kind, payload, trace_id, cause_id=None, event_id=None,
               source="runtime", occurred_at=None):
        eid = event_id or new_id()
        received = self.clock.now()
        cursor = self._db.execute(
            "INSERT INTO events(event_id,trace_id,cause_id,kind,payload,created,schema_version,"
            "robot_id,source,occurred_at,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (eid, trace_id, cause_id, kind, canonical(payload), received, "1.0",
             self.robot_id, source, received if occurred_at is None else occurred_at, received))
        return cursor.lastrowid

    def record(self, kind, payload, trace_id="runtime", cause_id=None):
        with self.transaction():
            return self._event(kind, payload, trace_id, cause_id)

    def accept(self, signal: Signal, decision: Decision, max_pending: int):
        signal.validate()
        if signal.robot_id != self.robot_id:
            raise InvalidInput("signal belongs to a different robot")
        with self.transaction():
            duplicate = self._db.execute("SELECT * FROM tasks WHERE event_id=?", (signal.event_id,)).fetchone()
            if duplicate:
                if duplicate["fingerprint"] != signal.fingerprint():
                    raise DuplicateConflict("event ID was already accepted with different content")
                return dict(duplicate), False
            usage = self.usage()
            outstanding = self._db.execute("SELECT count(*) FROM tasks WHERE status NOT IN "
                                           "('completed','cancelled','failed')").fetchone()[0]
            # Admission budget includes headroom for accepted tasks to stop/finish.
            # Critical receipts remain writable even when the admission budget is full.
            if usage["tasks"] >= self.max_tasks or usage["events"] + 16 * (outstanding + 1) > self.max_events:
                raise CapacityExceeded("journal admission budget exhausted; back up and provision capacity")
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
                              signal.trace_id, event_id=signal.event_id, source=signal.source,
                              occurred_at=signal.occurred_at)
            self._db.execute(
                "INSERT INTO tasks(id,event_id,fingerprint,trace_id,priority,salience,capability,"
                "arguments,status,enqueue_seq,reason,enqueued_at,deadline_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, signal.event_id, signal.fingerprint(), signal.trace_id,
                 decision.priority, decision.salience, decision.capability,
                 canonical(decision.arguments), "queued", seq, decision.reason,
                 self.schedule_now(), signal.deadline_at))
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
            now = self.schedule_now()
            expired = self._db.execute("SELECT id FROM tasks WHERE status='queued' "
                                       "AND deadline_at IS NOT NULL AND deadline_at<=?", (self.clock.now(),)).fetchall()
            for task in expired:
                self._transition(task["id"], "cancelled", "dispatch_deadline_expired")
            candidates = self._db.execute("SELECT * FROM tasks WHERE status='queued'").fetchall()
            def rank(task):
                age = max(0, now - task["enqueued_at"]) / self.aging_seconds
                priority = 0 if task["priority"] == 0 else max(1, task["priority"] - int(age))
                return (priority, task["deadline_at"] if task["deadline_at"] is not None else math.inf,
                        -task["salience"], task["enqueue_seq"])
            row = min(candidates, key=rank) if candidates else None
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
            data["causation_id"] = data["cause_id"]
            data["type"] = data["kind"]
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
