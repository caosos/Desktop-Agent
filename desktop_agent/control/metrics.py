"""Feedback metrics (IMPLEMENTATION_PLAN §4): verified work per dollar and per
hour, conflict and retry rates, and per task-type × model-class outcomes
that the router consults. Everything is computed from the store; nothing is
estimated."""
from __future__ import annotations

import time

from .events import EventType as ET
from .store import Store

WINDOW_SEC = 24 * 3600
MIN_SAMPLES = 3


def outcomes(store: Store, window_sec: int = WINDOW_SEC) -> dict[tuple[str, str], dict]:
    """(task_type, model_class) → {done, failed, total, success_rate}; retries count
    as separate attempts, which is what the router needs to know."""
    since = time.time() - window_sec
    out: dict[tuple[str, str], dict] = {}
    for t in store.list_tasks():
        if t["updated_at"] < since or t["status"] in ("READY", "RUNNING"):
            continue
        c = t["contract"]
        key = (c.get("task_type", "code"), c.get("model_class", "cloud_strong"))
        row = out.setdefault(key, {"done": 0, "failed": 0, "total": 0})
        row["total"] += 1
        if t["status"] == "DONE":
            row["done"] += 1
        else:
            row["failed"] += 1
    for row in out.values():
        row["success_rate"] = round(row["done"] / row["total"], 2) if row["total"] else None
    return out


def feedback(store: Store, window_sec: int = WINDOW_SEC) -> dict:
    since = time.time() - window_sec
    tasks = [t for t in store.list_tasks() if t["updated_at"] >= since]
    done = [t for t in tasks if t["status"] == "DONE"]
    finished = [t for t in tasks if t["status"] in ("DONE", "FAILED", "BLOCKED")]
    retries = [t for t in tasks if (t["contract"].get("attempt") or 1) > 1]
    events = [e for e in store.events(limit=20000) if e.ts >= since]
    verifications = [e for e in events if e.type in (ET.VERIFY_PASSED.value, ET.VERIFY_FAILED.value)]
    conflicts = [e for e in events if e.type == ET.VERIFY_FAILED.value and e.payload.get("bounds_violation")] + \
                [e for e in events if e.type == ET.INTEGRATION_FAILED.value]
    costs = store.cost_summary()
    known_usd = sum(costs["per_task"].get(t["task_id"], 0.0) for t in done)
    unknown_cost_tasks = [t["task_id"] for t in done if t["task_id"] not in costs["per_task"]]
    first = min((t["created_at"] for t in tasks), default=None)
    hours = max(0.01, (time.time() - first) / 3600) if first else None
    return {
        "window_hours": round(window_sec / 3600, 1),
        "verified_tasks": len(done),
        "finished_tasks": len(finished),
        "verified_per_dollar": round(len(done) / known_usd, 2) if known_usd else None,
        "verified_per_hour": round(len(done) / hours, 2) if hours else None,
        "known_usd": round(known_usd, 4),
        "unknown_cost_tasks": unknown_cost_tasks,     # subscription executors: no dollar figure
        "conflict_rate": round(len(conflicts) / len(verifications), 2) if verifications else None,
        "retry_rate": round(len(retries) / len(tasks), 2) if tasks else None,
        "verifications": len(verifications),
    }


def effective_ceiling(owner_ceiling: int, fb: dict) -> tuple[int, str | None]:
    """Negative feedback only: drop one slot while conflicts are frequent."""
    if (fb.get("verifications") or 0) >= MIN_SAMPLES and (fb.get("conflict_rate") or 0) > 0.3 and owner_ceiling > 1:
        return owner_ceiling - 1, f"conflict_rate {fb['conflict_rate']} over {fb['verifications']} verifications"
    return owner_ceiling, None


def choose_class(task_type: str, proposed: str, stats: dict[tuple[str, str], dict], ladder: list[str],
                 step_down_rate: float = 0.8, step_up_rate: float = 0.5) -> tuple[str, str | None]:
    """Cheapest class with evidence of success for this task type; escalate when
    the proposed class has been failing. Without enough samples, keep the proposal."""
    if proposed not in ladder:
        return proposed, None
    i = ladder.index(proposed)
    if i > 0:
        below = stats.get((task_type, ladder[i - 1]))
        if below and below["total"] >= MIN_SAMPLES and (below["success_rate"] or 0) >= step_down_rate:
            return ladder[i - 1], f"{ladder[i - 1]} succeeded {below['done']}/{below['total']} on {task_type}"
    here = stats.get((task_type, proposed))
    if here and here["total"] >= MIN_SAMPLES and (here["success_rate"] or 0) < step_up_rate and i + 1 < len(ladder):
        return ladder[i + 1], f"{proposed} failed {here['failed']}/{here['total']} on {task_type}"
    return proposed, None
