"""Event types, the event record, and stage derivation.

Stage is a pure function of a task's events (IMPLEMENTATION_PLAN §2,
ARCHITECTURE_REVIEW §8). Nothing else may set it.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Iterable


class EventType(str, Enum):
    GOAL_RECEIVED = "GOAL_RECEIVED"
    TASK_CREATED = "TASK_CREATED"
    TASK_CLAIMED = "TASK_CLAIMED"
    WORKER_STARTED = "WORKER_STARTED"
    MODEL_SELECTED = "MODEL_SELECTED"
    FILE_READ = "FILE_READ"
    FILE_CHANGED = "FILE_CHANGED"
    TOOL_CALLED = "TOOL_CALLED"
    TOOL_DENIED = "TOOL_DENIED"
    TEST_STARTED = "TEST_STARTED"
    TEST_PASSED = "TEST_PASSED"
    TEST_FAILED = "TEST_FAILED"
    COMMIT_CREATED = "COMMIT_CREATED"
    CLAIM_WRITTEN = "CLAIM_WRITTEN"
    BLOCKED = "BLOCKED"
    OWNER_DECISION_REQUESTED = "OWNER_DECISION_REQUESTED"
    OWNER_DECISION_RECORDED = "OWNER_DECISION_RECORDED"
    BUDGET_WARNING = "BUDGET_WARNING"
    WORKER_STALLED = "WORKER_STALLED"
    WORKER_FINISHED = "WORKER_FINISHED"
    WORKER_KILLED = "WORKER_KILLED"
    VERIFY_STARTED = "VERIFY_STARTED"
    VERIFY_PASSED = "VERIFY_PASSED"
    VERIFY_FAILED = "VERIFY_FAILED"
    PUSHED = "PUSHED"
    PR_OPENED = "PR_OPENED"
    INTEGRATION_STARTED = "INTEGRATION_STARTED"
    INTEGRATION_PASSED = "INTEGRATION_PASSED"
    INTEGRATION_FAILED = "INTEGRATION_FAILED"
    TASK_DONE = "TASK_DONE"
    CONTROL = "CONTROL"  # pause / resume / stop issued by the owner


class Stage(str, Enum):
    READING = "READING"
    PLANNING = "PLANNING"
    BUILDING = "BUILDING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    INTEGRATING = "INTEGRATING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    WAITING_OWNER = "WAITING_OWNER"


class Actor(str, Enum):
    HUMAN = "human"
    CONTROL = "control"
    WORKER = "worker"
    PROVIDER = "provider"
    INFERRED = "inferred"


@dataclass
class Provenance:
    actor: str                      # Actor value
    source: str                     # "claude_headless" | "verifier" | "panel" | "widget:aria" | ...
    contract_hash: str | None = None
    model: str | None = None
    evidence: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Event:
    type: str
    task_id: str | None
    payload: dict
    provenance: Provenance
    worker_id: str | None = None
    parent_event_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    seq: int | None = None          # assigned by the store

    def to_dict(self) -> dict:
        d = asdict(self)
        d["provenance"] = self.provenance.to_dict()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        p = d.get("provenance") or {}
        return cls(
            type=d["type"],
            task_id=d.get("task_id"),
            payload=d.get("payload") or {},
            provenance=Provenance(
                actor=p.get("actor", Actor.CONTROL.value),
                source=p.get("source", "unknown"),
                contract_hash=p.get("contract_hash"),
                model=p.get("model"),
                evidence=list(p.get("evidence") or []),
            ),
            worker_id=d.get("worker_id"),
            parent_event_id=d.get("parent_event_id"),
            id=d.get("id") or uuid.uuid4().hex,
            ts=float(d.get("ts") or time.time()),
            seq=d.get("seq"),
        )


# Terminal / state-setting events, checked newest-first. The first match wins.
_TERMINAL: list[tuple[EventType, Stage]] = [
    (EventType.TASK_DONE, Stage.DONE),
    (EventType.BLOCKED, Stage.BLOCKED),
    (EventType.WORKER_KILLED, Stage.BLOCKED),
    (EventType.INTEGRATION_FAILED, Stage.BLOCKED),
    (EventType.VERIFY_FAILED, Stage.BLOCKED),
    (EventType.INTEGRATION_STARTED, Stage.INTEGRATING),
    (EventType.INTEGRATION_PASSED, Stage.INTEGRATING),
    (EventType.PUSHED, Stage.INTEGRATING),
    (EventType.PR_OPENED, Stage.INTEGRATING),
    (EventType.VERIFY_PASSED, Stage.REVIEWING),
    (EventType.VERIFY_STARTED, Stage.TESTING),
    (EventType.WORKER_STALLED, Stage.BLOCKED),
]


def derive_stage(events: Iterable[Event]) -> Stage:
    """Derive the stage of one task from its events, oldest to newest."""
    evs = list(events)
    if not evs:
        return Stage.PLANNING
    types = [e.type for e in evs]
    # An unanswered owner decision overrides everything but DONE.
    requested = types.count(EventType.OWNER_DECISION_REQUESTED.value)
    recorded = types.count(EventType.OWNER_DECISION_RECORDED.value)
    if requested > recorded and EventType.TASK_DONE.value not in types:
        return Stage.WAITING_OWNER
    for e in reversed(evs):
        for et, stage in _TERMINAL:
            if e.type == et.value:
                return stage
        if e.type == EventType.WORKER_FINISHED.value:
            # Worker is done; until the verifier starts the task is under review.
            return Stage.REVIEWING
    # Worker is running (or has not started yet).
    if EventType.WORKER_STARTED.value not in types:
        return Stage.PLANNING
    saw_change = False
    for e in evs:
        if e.type == EventType.TEST_STARTED.value:
            # a worker-run test after changes
            return Stage.TESTING if saw_change else Stage.READING
        if e.type == EventType.FILE_CHANGED.value:
            saw_change = True
    return Stage.BUILDING if saw_change else Stage.READING


def stage_counts(events_by_task: dict[str, list[Event]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for evs in events_by_task.values():
        s = derive_stage(evs).value
        out[s] = out.get(s, 0) + 1
    return out
