"""State transitions that retire obsolete ETA-driven checks without erasing evidence (DA-STALE-CHECK-CLOSURE-001, da-213f9988f5).

Run at the top of every watchdog check pass, so a restart or replay reaches the same state:
  1. a check armed from a receipt that names another bounded task (its `task` starts with that task's da-id, and that task has its own
     check) is SUPERSEDED by the task's own check: one task, one timer, however many items were acknowledged in the same comment;
  2. a check whose latest receipt says the blocker is NEED_OWNER is PARKED (WAITING_OWNER): nobody on our side owes a receipt while the
     owner decides, and the NEED itself is already shown to the owner. A newer receipt re-arms it.
A DONE item needs no rule here: the existing pass publishes its single DONE outcome and closes it. A genuinely open check (its own task, no owner
wait) is untouched, so real missed-ACK / missed-ETA detection stays."""
from __future__ import annotations

import re

_DA = re.compile(r"\bda-[0-9a-f]{10}\b")


def _retire(checks, rec: dict, state: str, reason: str) -> None:
    now = checks.clock()
    rec.update(state=state, closed_at=now, closed_reason=reason)
    checks._save(rec)
    item = checks.store.get_intake(rec["item_id"]) or {"item_id": rec["item_id"], "project": rec.get("project"), "status": "UNKNOWN"}
    checks.store.set_kv("alert:" + rec["item_id"], {"cleared": True, "at": now})
    checks._event(item, {"action": "check_retired", "state": state, "reason": reason}, [rec.get("receipt_url", ""), reason[:100]])
    checks._receipt(item, f"check {state.lower().replace('_', ' ')}: {reason}", [rec.get("receipt_url", "") or "check record"])


def reconcile(checks) -> list[str]:
    done = []
    for rec in checks.list():
        if rec.get("state") != "ARMED":
            continue
        task_id = next(iter(_DA.findall((rec.get("fields") or {}).get("task", ""))), None)
        if task_id and task_id != rec["item_id"] and (checks.get(task_id) or {}).get("state") in ("ARMED", "WAITING_OWNER", "CLOSED"):
            _retire(checks, rec, "SUPERSEDED", f"its task {task_id} has its own check; one task, one timer")
        elif str((rec.get("fields") or {}).get("blocker", "")).upper().startswith("NEED_OWNER"):
            _retire(checks, rec, "WAITING_OWNER", "the latest receipt says the blocker is the owner's decision; a newer receipt re-arms it")
        else:
            continue
        done.append(rec["item_id"])
    return done
