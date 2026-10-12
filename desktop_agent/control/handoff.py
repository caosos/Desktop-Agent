"""Automatic DONE → next eligible handoff (owner directive 2026-10-11 20:03 CDT; item da-17eaaa1109).

When a coordinator's DONE receipt is read back for a Desktop-Agent instruction, this selects the next owner-authorized
row of the project's own BUILD_CHECKLIST execution queue and delivers it ONCE to the existing coordinator through the
existing intake delivery path, as an intake item (kind `handoff`) that then follows RECEIVED → DELIVERED → ACK →
WORKING → DONE like any instruction; the 300 s unacknowledged flag and the ETA-driven checks cover it.

Selection FAILS CLOSED: nothing is delivered (a refusal is recorded with its reason) unless the first READY / QUEUED row
by priority and table order is clearly eligible: assigned to the coordinator, not owner-gated, not blocked, not another
project's, dependencies verifiably met, no earlier handoff still open, and the hourly and daily budgets not exhausted.
One completed item produces at most one handoff (deterministic id + persisted record), also across restarts. Only
projects with `intake.handoff: true` take part; the queue read is the project's own file. No model is called.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from .events import Actor, EventType as ET
from .receipts import VERIFIED, write_receipt

OPEN_STATUSES = ("RECEIVED", "DELIVERED", "ACKNOWLEDGED", "WORKING")
_OWNER_GATED = re.compile(r"\b(owner (decision|window|choice|answers?|names?)|michael (names|chooses|answers|at desktop|decides)|awaiting (the )?owner|needs? michael|owner-?gated|supervisor decides)\b", re.I)
_OTHER_PROJECT = re.compile(r"(?<![\w`-])(caoscare(?![-\w])|deal sniffer|michael[_ ]business[_ ]os|business os|agent 01)(?![-\w])", re.I)   # not a session name like caoscare-1-8c


def parse_queue(text: str) -> list[dict]:
    """Rows of the execution queue table (8 columns: outcome, priority, dependencies, responsible, state, acceptance, evidence, next)."""
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8 or set(cells[0]) <= set("-: ") or cells[0].lower().startswith("outcome"):
            continue
        rows.append({"title": cells[0], "priority": cells[1], "deps": cells[2], "responsible": cells[3], "state": cells[4],
                     "acceptance": cells[5], "evidence": cells[6], "next": cells[7]})
    return rows


def _state_word(row: dict) -> str:
    m = re.match(r"[A-Za-z]+", row["state"])
    return m.group(0).upper() if m else ""


def _prio(row: dict) -> int | None:
    m = re.match(r"P(\d)", row["priority"])
    return int(m.group(1)) if m else None


def select_next(rows: list[dict]) -> tuple[dict | None, str | None]:
    """(row, None) when the first READY/QUEUED row is eligible, else (None, reason). Never skips a refused row."""
    cands = [(i, r) for i, r in enumerate(rows) if _state_word(r) in ("READY", "QUEUED")]
    if not cands:
        return None, "no READY or QUEUED row in the queue"
    if any(_prio(r) is None for _, r in cands):
        return None, "ambiguous: a READY/QUEUED row has no parseable priority"
    cands.sort(key=lambda ir: (_prio(ir[1]), ir[0]))
    row = cands[0][1]
    if sum(1 for _, r in cands if r["title"] == row["title"]) > 1:
        return None, "ambiguous: two READY/QUEUED rows share the same title"
    gated_text = " ".join((row["deps"], row["responsible"], row["state"], row["next"]))
    if _OTHER_PROJECT.search(" ".join((row["title"], row["responsible"], row["deps"]))):
        return None, "outside Desktop-Agent: the row names another project"
    if "coordinator" not in row["responsible"].lower() and "control plane" not in row["responsible"].lower():
        return None, f"not assigned to the coordinator (responsible: {row['responsible'][:60]})"
    if _OWNER_GATED.search(gated_text):
        return None, "owner-gated: the row waits on an owner decision or action"
    deps = row["deps"].strip().lower()
    if deps and not deps.startswith("none"):
        met = [r for r in rows if r is not row and r["title"][:40].lower() in deps and _state_word(r) in ("DONE", "IMPLEMENTED")]
        if not met:
            return None, f"dependency not verifiably met: {row['deps'][:80]}"
    return row, None


class Handoff:
    def __init__(self, store, intake, checklist_for, cfg=None, clock=time.time):
        """checklist_for(project) → the project's own BUILD_CHECKLIST path (never another project's)."""
        self.store, self.intake, self.checklist_for, self.cfg, self.clock = store, intake, checklist_for, cfg, clock

    def _budget_refusal(self) -> str | None:
        if self.cfg is None:
            return None
        c = self.store.cost_summary()
        s = self.cfg.scheduler
        if c["last_hour_usd"] >= s.hourly_cap_usd:
            return f"over budget: ${c['last_hour_usd']:.2f} spent in the last hour against the ${s.hourly_cap_usd:.2f} cap"
        if c["today_usd"] >= s.daily_cap_usd:
            return f"over budget: ${c['today_usd']:.2f} spent today against the ${s.daily_cap_usd:.2f} cap"
        return None

    def _refuse(self, done: dict, reason: str) -> dict:
        self.intake._emit(done, ET.CONTROL.value, {"action": "handoff_refused", "reason": reason, "after": done["item_id"]}, [reason[:100]])
        write_receipt(self.store, subject_type="instruction", subject_id=done["item_id"], claim=f"handoff refused (fail closed): {reason}"[:300], actor=Actor.CONTROL.value,
                      source="handoff", result_label=VERIFIED, evidence=[reason[:100]], after_state={"handoff": "refused"})
        self.store.set_kv("handoff:done:" + done["item_id"], {"refused": reason, "at": self.clock()})
        return {"delivered": False, "refused": reason}

    async def on_done(self, src, done: dict) -> dict:
        key = "handoff:done:" + done["item_id"]
        if self.store.get_kv(key):
            return {"delivered": False, "duplicate": True}               # one completion, at most one handoff (also after a restart)
        checklist = Path(self.checklist_for(src.project))
        if not checklist.exists():
            return self._refuse(done, "the project's BUILD_CHECKLIST queue is not readable")
        if any(i.get("kind") == "handoff" and i["status"] in OPEN_STATUSES for i in self.store.list_intake(project=src.project) if i["item_id"] != done["item_id"]):
            return self._refuse(done, "an earlier handoff is still open (one bounded item at a time)")
        row, why = select_next(parse_queue(checklist.read_text()))
        if row is None:
            return self._refuse(done, why or "no eligible row")
        over = self._budget_refusal()
        if over:
            return self._refuse(done, over)
        item_id = "da-" + hashlib.sha1(f"handoff:{done['item_id']}:{row['title']}".encode()).hexdigest()[:10]
        if self.store.get_intake(item_id):
            self.store.set_kv(key, {"item_id": item_id, "at": self.clock()})
            return {"delivered": False, "duplicate": True}
        body = (f"AUTOMATIC HANDOFF (control plane, not an owner instruction) — {src.project}\n"
                f"Completed: {done['item_id']} ({(done.get('title') or '')[:80]}).\n"
                f"NEXT ELIGIBLE QUEUE ROW (first READY/QUEUED by priority, fail-closed checks passed): {row['title']}\n"
                f"Priority {row['priority']} · responsible: {row['responsible']} · dependencies: {row['deps']}\n"
                f"Acceptance: {row['acceptance']}\n"
                f"Reply `ACK {item_id}` then `WORKING {item_id}` with the ETA receipt schema (AGENTS.md); the unacknowledged flag fires after 300 s.")
        item = {"item_id": item_id, "project": src.project, "repo": src.repo, "issue": src.issues[0], "kind": "handoff", "gh_id": 0, "author": "control",
                "title": f"handoff after {done['item_id']}: {row['title'][:80]}", "body": body, "url": f"https://github.com/{src.repo}/issues/{src.issues[0]}",
                "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())), "status": "POSTED", "coordinator": (src.coordinator or {}).get("kind")}
        self.store.save_intake(item)
        self.store.set_kv(key, {"item_id": item_id, "row": row["title"], "at": self.clock()})        # recorded BEFORE delivery: a crash cannot deliver twice
        self.intake._set(item, "RECEIVED", f"derived by the control plane after {done['item_id']} reached DONE: {row['title'][:100]}", [done["item_id"], row["title"][:80]])
        delivered = await self.intake._deliver(src, item)
        return {"delivered": bool(delivered), "item_id": item_id, "row": row["title"]}
