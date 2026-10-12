"""The owner-first, model-sized view of the control-plane state that Aria's get_state action reads, and the opening
greeting built from live state without a model (see aria.py)."""
from __future__ import annotations

import time


def greeting(st: dict) -> dict:
    """Opening line from live state, no model call: decisions waiting, what is running, what finished, paid spend only
    if authorised or used, anything unacknowledged. Offers to walk through decisions one at a time."""
    running = [t for t in st["tasks"] if t["status"] == "RUNNING"]
    done_today = [t for t in st["tasks"] if t["status"] == "DONE" and time.time() - t["updated_at"] < 86400]
    blocked, inbox, ik = st["blocked"], st["inbox"], st.get("intake") or {}
    parts = ["Hello Michael."]
    parts.append(f"I see {len(inbox)} decision{'s' if len(inbox) != 1 else ''} waiting. Want to go through them together?" if inbox else "No decisions are waiting for you.")
    if running:
        parts.append(f"{len(running)} worker{'s are' if len(running) != 1 else ' is'} running: " +
                     "; ".join(f"{t['project']} {t['stage'].lower()} ({t['objective'][:60]})" for t in running[:2]) + ".")
    if done_today:
        parts.append(f"{len(done_today)} task{'s' if len(done_today) != 1 else ''} finished and verified in the last day.")
    if blocked:
        parts.append(f"{len(blocked)} blocked item{'s' if len(blocked) != 1 else ''} to look at.")
    if ik.get("unacknowledged"):
        parts.append(f"{len(ik['unacknowledged'])} instruction{'s' if len(ik['unacknowledged']) != 1 else ''} to coordinators still unacknowledged.")
    b = st["budgets"]
    if b["daily_max_paid_usd"] > 0 or b["paid_today_usd"] > 0:       # nothing about money unless paid API is authorised or was used
        parts.append(f"Paid API today ${b['paid_today_usd']:.2f} of your ${b['daily_max_paid_usd']:.2f} daily maximum.")
    first = inbox[0] if inbox else None
    return {"greeting": " ".join(parts),
            "first_decision": ({"decision_id": first["decision_id"], "question": first["question"], "options": first["options"] or ["yes", "no"]} if first else None),
            "counts": {"decisions": len(inbox), "running": len(running), "done_today": len(done_today), "blocked": len(blocked)}}


def compact_state(st: dict) -> dict:
    """The owner view in model-sized form: what needs the owner, what is blocked and what runs, first; raw
    project internals stay behind project_brief. The full state is ~85 KB; this must fit one action result."""
    ik = st.get("intake") or {}
    work = st.get("work") or {}
    open_items = [i for i in (ik.get("items") or []) if i.get("status") not in ("DONE", "CLOSED")]
    return {
        "next": st.get("next"), "glance": st.get("glance"),
        "work": {k: [{"project": r.get("project"), "who": r.get("who"), "text": str(r.get("text", ""))[:120]} for r in (work.get(k) or [])[:10]]
                 for k in ("needs_owner", "waiting", "can_run_now")},
        "blocked": st.get("blocked"), "decisions_waiting": st.get("inbox"), "workers_running": st.get("workers"),
        "paused": st.get("paused"), "hold": st.get("hold"),
        "projects": [{"name": p.get("name"), "stage": p.get("stage"),
                      "coordinator": {k: (str(v)[:140] if k == "detail" else v) for k, v in (p.get("coordinator") or {}).items() if k in ("connected", "detail", "session")},
                      "workers": {k: v for k, v in (p.get("workers") or {}).items() if k in ("status", "active", "detail")}} for p in st.get("projects") or []],
        "tasks": [{"task_id": t.get("task_id"), "status": t.get("status"), "stage": t.get("stage"), "objective": str(t.get("objective", ""))[:80]}
                  for t in (st.get("tasks") or [])[-5:]],
        "shared_inbox": {"counts": ik.get("counts"), "unacknowledged": ik.get("unacknowledged"),
                         "open_items": [{"item_id": i.get("item_id"), "project": i.get("project"), "status": i.get("status"), "title": str(i.get("title", ""))[:60]} for i in open_items[-8:]]},
        "paid_api_budget": st.get("budgets"),      # the one owner budget; subscription usage has no dollar figure to quote
    }
