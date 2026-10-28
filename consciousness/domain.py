"""Versioned boundary objects. No driver or storage dependencies."""
from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import math
from typing import Any
from uuid import uuid4


def new_id() -> str:
    return uuid4().hex


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


class DomainError(Exception):
    code = "DOMAIN_ERROR"


class Backpressure(DomainError):
    code = "BACKPRESSURE"


class InvalidInput(DomainError):
    code = "INVALID_INPUT"


class DuplicateConflict(DomainError):
    code = "DUPLICATE_CONFLICT"


class RevisionConflict(DomainError):
    code = "REVISION_CONFLICT"


class ContextOverflow(DomainError):
    code = "CONTEXT_OVERFLOW"


class StoreBusy(DomainError):
    code = "STORE_BUSY"


class CapacityExceeded(Backpressure):
    code = "CAPACITY_EXCEEDED"


class Status(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_STOP = "waiting_stop"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "outcome_unknown"


TERMINAL = {Status.COMPLETED.value, Status.CANCELLED.value, Status.FAILED.value}
TRANSITIONS = {
    Status.QUEUED.value: {Status.RUNNING.value, Status.CANCELLED.value},
    Status.RUNNING.value: {Status.WAITING_STOP.value, *TERMINAL, Status.UNKNOWN.value},
    Status.WAITING_STOP.value: {*TERMINAL, Status.UNKNOWN.value},
    Status.UNKNOWN.value: TERMINAL,
}


@dataclass(frozen=True)
class Signal:
    text: str
    event_id: str = field(default_factory=new_id)
    trace_id: str = field(default_factory=new_id)
    source: str = "user"
    schema_version: str = "1.0"
    robot_id: str = "robot-demo"
    occurred_at: float | None = None
    deadline_at: float | None = None

    def validate(self):
        if self.schema_version != "1.0":
            raise InvalidInput("unsupported signal schema version")
        if not isinstance(self.text, str) or not self.text.strip():
            raise InvalidInput("text must be nonempty")
        if len(self.text.encode("utf-8")) > 16384:
            raise InvalidInput("text exceeds 16 KiB")
        for key in (self.event_id, self.trace_id, self.source, self.robot_id):
            if not isinstance(key, str) or not key or len(key) > 128:
                raise InvalidInput("identifiers must contain 1..128 characters")
        if self.source not in {"user", "sensor"}:
            raise InvalidInput("outputs and internal feedback are not external inputs")
        for value in (self.occurred_at, self.deadline_at):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise InvalidInput("timestamps must be finite nonnegative Unix seconds")

    def fingerprint(self):
        # Retry traces may differ; identity of the accepted content must not.
        fields = {"text": self.text, "source": self.source, "schema_version": self.schema_version}
        # Preserve the v1 fingerprint for retries of previously accepted signals.
        if self.robot_id != "robot-demo":
            fields["robot_id"] = self.robot_id
        for name in ("occurred_at", "deadline_at"):
            if getattr(self, name) is not None:
                fields[name] = getattr(self, name)
        return canonical(fields)


@dataclass(frozen=True)
class Decision:
    priority: int
    salience: int
    capability: str
    arguments: dict
    reason: str


@dataclass(frozen=True)
class Action:
    id: str
    task_id: str
    generation: int
    epoch: str
    capability: str
    arguments: dict
    idempotency_key: str
    trace_id: str

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Result:
    action_id: str
    generation: int
    epoch: str
    status: str
    evidence: str
    detail: str = ""

    def matches(self, action: Action) -> bool:
        return (self.action_id == action.id and self.generation == action.generation
                and type(self.generation) is int and self.epoch == action.epoch
                and isinstance(self.evidence, str) and bool(self.evidence)
                and self.status in TERMINAL)


@dataclass(frozen=True)
class StopReceipt:
    action_id: str
    generation: int
    epoch: str
    stopped: bool
    evidence: str

    def matches(self, action: Action) -> bool:
        return (self.action_id == action.id and self.generation == action.generation
                and type(self.generation) is int and self.epoch == action.epoch
                and self.stopped is True and isinstance(self.evidence, str) and bool(self.evidence))


def positive_number(value: float, name: str):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
