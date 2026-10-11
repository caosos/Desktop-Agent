"""Read model for the panel, widget and Aria: event descriptions, task views, the full `/v0/state` document,
coordinator state per project, budgets and the per-task detail/trace. Read-only over the Service's components."""
from __future__ import annotations

import time

from . import allowance, metrics, workspace as ws
from .events import Event, EventType as ET, derive_stage
from .receipts import VERIFIED


def describe(ev: Event) -> str:
    p = ev.payload
    t = ev.type
    short = {
        "GOAL_RECEIVED": lambda: f"goal received: {p.get('text', '')[:80]}",
        "TASK_CREATED": lambda: f"task created ({p.get('model_class', '')})" + (f", retry of {p['retry_of']}" if p.get("retry_of") else ""),
        "TASK_CLAIMED": lambda: f"claimed, model {p.get('model')}",
        "WORKER_STARTED": lambda: f"worker started on {p.get('branch')}",
        "MODEL_SELECTED": lambda: f"model {p.get('model')}",
        "FILE_READ": lambda: f"read {p.get('path', '')}",
        "FILE_CHANGED": lambda: f"changed {p.get('path', '')}",
        "TOOL_CALLED": lambda: f"ran {p.get('command', p.get('tool', ''))[:70]}",
        "TOOL_DENIED": lambda: f"denied {p.get('tool')}: {p.get('summary', '')[:50]}",
        "TEST_STARTED": lambda: f"tests: {p.get('command', '')[:60]}",
        "TEST_PASSED": lambda: "worker tests passed (claim)",
        "TEST_FAILED": lambda: "worker tests failed (claim)",
        "COMMIT_CREATED": lambda: f"commit {str(p.get('sha') or '')[:10]}",
        "CLAIM_WRITTEN": lambda: f"worker claim: {p.get('status')}",
        "WORKER_FINISHED": lambda: f"worker finished, exit {p.get('exit_code')}, ${p.get('cost_usd') or 0:.2f}",
        "WORKER_KILLED": lambda: "worker killed",
        "WORKER_STALLED": lambda: "worker stalled",
        "VERIFY_STARTED": lambda: "verifier: clean checkout + tests",
        "VERIFY_PASSED": lambda: "verified",
        "VERIFY_FAILED": lambda: "verification failed: " + "; ".join(p.get("reasons", []))[:80],
        "PUSHED": lambda: f"pushed {p.get('branch')}",
        "PR_OPENED": lambda: f"draft PR {p.get('url')}",
        "INTEGRATION_FAILED": lambda: f"integration failed: {p.get('error', '')[:60]}",
        "BLOCKED": lambda: f"blocked: {p.get('reason', '')[:80]}",
        "TASK_DONE": lambda: "done",
        "OWNER_DECISION_REQUESTED": lambda: f"needs Michael: {p.get('question', '')[:60]}",
        "CONTROL": lambda: f"owner: {p.get('action')}",
    }
    return short.get(t, lambda: t.lower())()


class IntakeSourceLite:
    """Just enough of an IntakeSource for session lookup from a project's coordinator config."""
    def __init__(self, coordinator: dict):
        self.coordinator = coordinator


def task_view(service, row: dict) -> dict:
    evs = service.store.events(row["task_id"])
    last = evs[-1] if evs else None
    return {
        "task_id": row["task_id"], "project": row["project"], "status": row["status"],
        "stage": derive_stage(evs).value, "objective": row["contract"].get("objective"),
        "model_class": row["contract"].get("model_class"), "attempt": row["contract"].get("attempt"),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "last_activity": describe(last) if last else "", "last_activity_at": last.ts if last else row["created_at"],
        "result": row.get("result"),
    }

def state(service) -> dict:
    tasks = [task_view(service, t) for t in service.store.list_tasks()]
    projects = []
    for name, pkg in service.projects.items():
        ptasks = [t for t in tasks if t["project"] == name]
        active = [t for t in ptasks if t["status"] in ("READY", "RUNNING")]
        newest = max(ptasks, key=lambda t: t["last_activity_at"], default=None)
        running = [t for t in active if t["status"] == "RUNNING"]
        waiting = [d for d in service.store.open_decisions()]
        if running:
            stage = running[0]["stage"]
        elif active:
            stage = "PLANNING"                     # READY tasks waiting for a slot
        elif any(t["status"] == "BLOCKED" for t in ptasks[-3:]) and waiting:
            stage = "WAITING_OWNER"
        else:
            stage = "IDLE"                         # nothing active; history stays in the task list
        coord = coordinator_state(service, pkg)
        projects.append({"name": name, "integration_branch": pkg.integration_branch, "stage": stage,
                         "coordinator": coord,
                         "workers": service.workers.snapshot(pkg, ptasks, service.scheduler, last_confirmed=coord.get("last_ack_at")),
                         "open_tasks": len(active), "blocked_tasks": sum(1 for t in ptasks if t["status"] == "BLOCKED"),
                         "done_tasks": sum(1 for t in ptasks if t["status"] == "DONE"),
                         "last_activity": newest["last_activity"] if newest else "",
                         "last_activity_at": newest["last_activity_at"] if newest else None})
    workers = [{"worker_id": r.worker_id, "task_id": tid, "started_at": r.started_at, "last_event_at": r.last_event_at}
               for tid, r in service.launcher.running.items()]
    slots = service.scheduler.slots()
    hold = service.scheduler.hold_reason if service.scheduler.paused else None
    inbox = service.store.open_decisions()
    running = [t for t in tasks if t["status"] == "RUNNING"]
    done_day = [t for t in tasks if t["status"] == "DONE" and time.time() - t["updated_at"] < 86400]
    blocked_list = [t for t in tasks if t["status"] == "BLOCKED"]
    if inbox:
        next_action = {"kind": "decide", "text": inbox[0]["question"], "decision_id": inbox[0]["decision_id"], "options": inbox[0]["options"] or ["yes", "no"]}
    elif blocked_list:
        next_action = {"kind": "unblock", "text": f"Look at blocked task {blocked_list[0]['task_id']}: {(blocked_list[0].get('result') or {}).get('reason', '')[:120]}", "task_id": blocked_list[0]["task_id"]}
    else:
        next_action = {"kind": "none", "text": "Nothing needs you right now." + (" Workers are running." if running else " Give Aria a task when you have one.")}
    glance = {"done_today": [{"task_id": t["task_id"], "objective": (t.get("objective") or "")[:90],
                              "evidence": ((t.get("result") or {}).get("integration") or {}).get("pr_url") or "verified by the control plane"} for t in done_day[-8:]],
              "working": [{"task_id": t["task_id"], "stage": t["stage"], "objective": (t.get("objective") or "")[:90], "last_activity": t["last_activity"]} for t in running],
              "blocked": [{"task_id": t["task_id"], "reason": (t.get("result") or {}).get("reason", "")[:140]} for t in blocked_list[:5]],
              "next_action": next_action}
    from .drilldown import work_summary
    intake_summary = service.intake.summary() if service.intake.sources else {}
    work = work_summary(projects, tasks, inbox, intake_summary, service.scheduler, slots, service.store)
    if next_action["kind"] == "none" and (work["needs_owner"] or work["waiting"]):
        next_action = {"kind": "none", "text": (f"Needs you: {work['needs_owner'][0]['text'][:110]}" if work["needs_owner"] else f"Waiting on others: {work['waiting'][0]['text'][:110]}")}
        glance["next_action"] = next_action
    if any(t["status"] == "READY" for t in tasks) and slots["free"] == 0 and not service.scheduler.paused:
        if not slots["daily_cap_ok"]:
            hold = "daily budget cap reached"
        elif slots["budget_slots"] == 0:
            hold = "hourly budget cap: waiting for spend to age out of the window"
        elif slots["mem_slots"] == 0:
            hold = "host memory below reserve"
        elif slots["cpu_slots"] == 0:
            hold = "no CPU slots"
        elif slots["running"] >= slots["ceiling"]:
            hold = "worker ceiling reached"
        else:
            hold = "ready tasks conflict with running work or wait on dependencies"
    return {
        "ts": time.time(), "uptime_sec": round(time.time() - service.started_at), "paused": service.scheduler.paused,
        "projects": projects, "tasks": tasks[-50:], "workers": workers,
        "inbox": inbox, "costs": service.store.cost_summary(),
        "blocked": blocked_list[-20:], "glance": glance, "work": work,
        "next": next((t for t in tasks if t["status"] == "READY"), None),
        "slots": slots, "hold": hold, "class_labels": dict(service.cfg.class_labels), "last_seq": service.store.last_seq(),
        "feedback": metrics.feedback(service.store),
        "budgets": budgets(service),
        "llm": service.llm.status(),
        "intake": service.intake.summary(),
        "quota": allowance.view(service.store.get_kv("quota")),
        "outcomes": [{"task_type": k[0], "model_class": k[1], **v} for k, v in metrics.outcomes(service.store).items()],
    }

def coordinator_state(service, pkg) -> dict:
    """Who coordinates this project, whether it is reachable now, when it was last checked, woken
    and heard from, and whether wake is VERIFIED, manual-only or unavailable. Never invents a status."""
    from .intake_delivery import Deliverer
    ik = pkg.intake or {}
    coord = ik.get("coordinator") or {}
    kind = coord.get("kind")
    row = service.store.coordinator_rows().get(pkg.name) or {}
    base = {"kind": kind, "last_activity_at": row.get("last_seen"), "last_check_at": row.get("last_check"),
            "last_wake_at": row.get("last_wake"), "last_wake_kind": row.get("last_wake_kind"), "last_ack_at": row.get("last_ack"),
            "external": ((row.get("check") or {}).get("state") if row.get("check") else None),
            "probe": ((row.get("check") or {}).get("probe") if row.get("check") else None),
            "check_error": ((row.get("check") or {}).get("error") if row.get("check") else None)}
    if kind == "control_plane":
        return base | {"connected": True, "detail": "this control plane", "wake": "n/a (self)"}
    if kind == "claude_peer":
        s = Deliverer.session_for(IntakeSourceLite(coord))
        verified = bool(row.get("last_wake") and row.get("last_ack") and row["last_ack"] >= row["last_wake"])
        if s:
            how = ("" if s.get("resolved_by", "name") == "name"
                   else f" — pinned name {coord.get('session_name')} is not live; resolved by {s['resolved_by']}" if coord.get("session_name")
                   else f" — resolved by {s['resolved_by']} (no name pinned for this project)")
            return base | {"connected": True, "detail": f"session {s['name']} ({s['status']}){how}", "session": s["name"], "session_status": s["status"],
                           "resolved_by": s.get("resolved_by", "name"),
                           "wake": "VERIFIED (delivery → ACK observed)" if verified else "delivered, ACK pending" if row.get("last_wake") else "untested"}
        return base | {"connected": False, "detail": "Disconnected: no live coordinator session; items queue until one appears",
                       "wake": "unavailable (no session)"}
    if kind == "liaison":
        return base | {"connected": False, "detail": "liaison branch inbox; coordinator runs under another account, reached only by its own sync",
                       "wake": "manual-only (coordinator syncs the liaison branch itself)"}
    return base | {"connected": False, "detail": "Disconnected / Data unavailable: no coordinator integration configured", "wake": "unavailable"}

def budgets(service) -> dict:
    """Caps, actual spend against them, and usage with no dollar figure (never estimated)."""
    c = service.store.cost_summary()
    s = service.cfg.scheduler
    return {"hourly_cap_usd": s.hourly_cap_usd, "daily_cap_usd": s.daily_cap_usd,
            "per_task_default_usd": service.cfg.default_budget_usd,
            "spent_last_hour_usd": c["last_hour_usd"], "spent_today_usd": c["today_usd"],
            "remaining_hour_usd": round(max(0.0, s.hourly_cap_usd - c["last_hour_usd"]), 4),
            "remaining_today_usd": round(max(0.0, s.daily_cap_usd - c["today_usd"]), 4),
            "unknown_usage": c["unknown_usage"],
            "note": "unknown_usage = subscription executors or unpriced models: tokens recorded, dollars not estimated"}

async def task_detail(service, task_id: str) -> dict | None:
    row = service.store.get_task(task_id)
    if not row:
        return None
    view = task_view(service, row)
    evs = service.store.events(task_id)
    view["events"] = [{"seq": e.seq, "ts": e.ts, "type": e.type, "text": describe(e), "actor": e.provenance.actor,
                       "source": e.provenance.source, "evidence": list(e.provenance.evidence or [])[:3]} for e in evs[-200:]]
    view["contract"] = row["contract"]
    view["receipts"] = service.store.receipts("task", task_id)
    # evidence trace: when each stage was first reached, and what the task cost (known dollars only; None = unknown)
    timeline, seen = [], set()
    for i, e in enumerate(evs):
        if i and e.type == evs[i - 1].type:
            continue                                   # the stage can only move when a new kind of event appears
        st = derive_stage(evs[:i + 1]).value
        if st not in seen:
            seen.add(st); timeline.append({"stage": st, "ts": e.ts, "seq": e.seq})
    view["stage_timeline"] = timeline
    view["cost_usd"] = service.store.cost_summary()["per_task"].get(task_id)
    view["tool_calls"] = sum(1 for e in evs if e.type == ET.TOOL_CALLED.value)
    view["files_changed_events"] = sum(1 for e in evs if e.type == ET.FILE_CHANGED.value)
    wsdir = service.cfg.workspaces_dir / task_id / "repo"
    base = next((e.payload.get("base_sha") for e in evs if e.type == ET.WORKER_STARTED.value), None)
    if wsdir.exists() and base:
        try:
            view["files"] = await ws.changed_files(wsdir, base)
            view["diff"] = await ws.diff_text(wsdir, base, 120_000)
        except ws.GitError as exc:
            view["diff_error"] = str(exc)
    return view
