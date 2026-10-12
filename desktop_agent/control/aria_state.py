"""The owner-first, model-sized view of the control-plane state that Aria's get_state action reads (see aria.py)."""
from __future__ import annotations


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
