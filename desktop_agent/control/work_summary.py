"""The owner's three lists — what can run now, what needs the owner, what waits on others — derived from live
sources only (tasks, inbox, intake, scheduler, project feeds), each row naming who it waits on; plus the small
age/relative-time helpers the drilldown shares. Imported by drilldown.py and state_view.py."""
from __future__ import annotations

import time

from .feedtime import feed_freshness


def _open_item_text(item: str, row: dict | None) -> str:
    """A coordinator's open instruction, labelled by the status this control plane actually holds for it. BLOCKED is not WORKING."""
    status = (row or {}).get("status")
    if status == "BLOCKED":
        return f"instruction {item} BLOCKED per its coordinator's last comment; it moves on an owner answer or a new WORKING/DONE comment"
    if status == "WORKING":
        return f"instruction {item} in its coordinator's hands (WORKING until its DONE comment)"
    if status == "ACKNOWLEDGED":
        return f"instruction {item} acknowledged by its coordinator, not yet WORKING"
    if status in ("SENT", "RECEIVED", "DELIVERED"):
        return f"instruction {item} delivered, not yet acknowledged by its coordinator"
    if status:
        return f"instruction {item} is {status} here while its coordinator's own list still shows it open"
    return f"instruction {item} open on its coordinator's own list (no record of it here)"


def work_summary(projects: list[dict], tasks: list[dict], inbox: list[dict], intake: dict, scheduler, slots: dict, store) -> dict:
    """Every row names who it waits on (`who`). Three short lists for the owner, from live sources only: what can run now, what needs the owner,
    what is waiting on someone else. Never 'nothing needs you' when a project's own data says otherwise."""
    can, need, wait = [], [], []
    now = time.time()
    ready = [t for t in tasks if t["status"] == "READY"]
    paused = bool(getattr(scheduler, "paused", False))
    if ready:
        if paused:
            wait.append({"project": "desktop_agent", "who": "scheduler (you can press Resume)", "text": f"{len(ready)} queued control-plane task(s) held: scheduler paused ({getattr(scheduler, 'hold_reason', None) or 'by owner'})"})
        elif slots.get("free", 0) > 0:
            can.append({"project": "desktop_agent", "who": "scheduler", "text": f"{len(ready)} queued control-plane task(s) start on the next scheduler tick ({slots.get('free')} free slot(s))"})
        else:
            wait.append({"project": "desktop_agent", "who": "scheduler", "text": f"{len(ready)} queued control-plane task(s) wait for a free slot ({slots.get('ceiling_reason') or 'budget or capacity'})"})
    for d in inbox:
        need.append({"project": d.get("project") or "desktop_agent", "who": "you", "text": f"optional decision: {d['question'][:90]}…" if len(d["question"]) > 90 else f"optional decision: {d['question']}"})
    for p in projects:
        name, w, c = p["name"], p.get("workers") or {}, p.get("coordinator") or {}
        ex = c.get("external") or {}
        for f in w.get("finished") or []:
            if "open" in f["status"]:
                need.append({"project": name, "who": "you", "text": f"merge the verified PR {f['pr_url']} ({f['id'][:30]})"})
        for item in ex.get("ready_unblocked") or []:
            can.append({"project": name, "who": "its coordinator", "text": f"its coordinator has unblocked work ready: {str(item)[:90]}"})
        known = {i["item_id"]: i for i in store.list_intake(name)} if ex.get("open_items") else {}
        for item in ex.get("open_items") or []:
            wait.append({"project": name, "who": "its coordinator", "text": _open_item_text(str(item), known.get(str(item)))})
        if ex.get("waiting_owner"):
            need.append({"project": name, "who": "you", "text": f"{len(ex['waiting_owner'])} gate(s) on its own list need you (see the approval packet's read-only section): " + "; ".join(map(str, ex["waiting_owner"][:3])) + ("…" if len(ex["waiting_owner"]) > 3 else "")})
        feed = store.get_kv(f"feed:{name}") or {}
        fw = ((feed.get("data") or {}).get("work") or {}) if feed else {}
        fstate = str(((feed.get("data") or {}).get("state") or "")).upper() if feed else ""
        if feed and fstate == "STOPPED" and not (feed.get("data") or {}).get("session"):
            ff = feed_freshness(feed, now, last_ack=(store.coordinator_rows().get(name) or {}).get("last_ack"))
            pend = (feed.get("data") or {}).get("unacknowledged_messages") or []
            acked_here = {i["title"]: i for i in store.list_intake(name) if i.get("status") == "ACKNOWLEDGED"}
            with_receipt = [m for m in pend if any(m in t for t in acked_here)]
            receipts = (f"{len(pend)} message(s) its feed lists as unread, {len(with_receipt)} of them with an ack receipt here" if pend
                        else "no message its feed lists as unread")
            if ff["fresh"] and not ff["ack_recent"]:
                need.insert(0, {"project": name, "who": "you (other account)",
                                "text": f"its coordinator session is STOPPED per its own watchdog ({ff['label']}); start it in your own account (tmux mbos-agent-01) — {receipts}" + (": " + ", ".join(pend[:2]) if pend else "")})
            elif ff["ack_recent"]:
                wait.append({"project": name, "who": "its coordinator (other account)",
                             "text": f"its watchdog says STOPPED ({ff['label']}) but an ack file from its account appeared {_ago(now - float(ff['last_ack']))} ago: the inbox is being processed, state UNKNOWN; no restart implied — {receipts}"})
            else:
                wait.append({"project": name, "who": "its watchdog (other account)",
                             "text": f"its watchdog last said STOPPED, but {ff['label']}; not a current state and not a restart instruction — {receipts}"})
        if fw and not feed_freshness(feed, now)["fresh"]:
            wait.append({"project": name, "who": "its watchdog (other account)",
                         "text": f"its feed's dispatcher/ready-row figures are not current ({feed_freshness(feed, now)['label']}); nothing is claimed from them"})
        elif fw:
            rows = fw.get("approved_ready_rows_for_specialist_lanes") or 0
            if rows and fw.get("dispatcher_running"):
                can.append({"project": name, "who": "its dispatcher", "text": f"{rows} approved row(s) ready; its own dispatcher launches them (quota allows a turn: {(feed.get('data') or {}).get('quota_allows_a_turn', '?')})"})
            elif rows:
                need.append({"project": name, "who": "you (other account)", "text": f"{rows} approved row(s) ready but its dispatcher is stopped: restart it in your own account (tmux mbos-agent-01)"})
            elif fw.get("dispatcher_running"):
                wait.append({"project": name, "who": "its coordinator", "text": "dispatcher running, 0 approved rows: nothing to launch until a gate clears or a new gap is queued"})
        for r in (store.get_kv(f"roster:{name}") or {}).get("rows") or []:
            b = r.get("blocker") or ""
            if b and b.lower() != "none" and any(k in b for k in ("MICHAEL_DECISIONS", "credentials", "Michael", "host software", "host")):
                need.append({"project": name, "who": "you", "text": f"lane {r['lane']} {r['role'][:20]}: {b[:110]}"})
    for item_id in (store.get_kv("checks:index") or {}).get("ids") or []:       # ETA-driven check alerts the notification filter let through
        al = store.get_kv("alert:" + item_id) or {}
        if al and not al.get("cleared"):
            chk = store.get_kv("check:" + item_id) or {}
            need.append({"project": chk.get("project") or "", "who": "you", "text": f"ALERT ({al.get('kind')}) for {item_id}: {str(al.get('text'))[:170]} — {al.get('reason')}"})
    unacked = [i for i in (intake.get("items") or []) if i.get("flagged") and i.get("status") in ("RECEIVED", "DELIVERED")]
    by_proj: dict[str, list] = {}
    for i in unacked:
        by_proj.setdefault(i["project"], []).append(i)
    for name, items in by_proj.items():
        needs = [store.get_kv("need:" + i["item_id"]) or {} for i in items if i.get("item_id")]
        host = [n for n in needs if n.get("classification") == "NEED_HOST_ACTION"]
        if host:                                        # the stored NEED body names the exact action: the owner's row, not a vague wait
            need.append({"project": name, "who": "you", "text": f"NEED_HOST_ACTION — {host[0].get('impact', '')}: {host[0].get('action', '')}"[:260]})
        rest = len(items) - len(host)
        if rest:
            liaison = any(i.get("kind") == "liaison" for i in items)
            svc = next((n for n in needs if n.get("classification") == "NEED_SERVICE"), None)
            tail = (": Agent 01 reads the liaison branch at its own sync; you can nudge it in your own account (tmux mbos-agent-01)" if liaison
                    else f": {svc['action']}" if svc else ": resend from the Shared inbox or check the coordinator session")
            wait.append({"project": name, "who": ("Agent 01" if liaison else "its coordinator"), "text": f"{rest} instruction(s) delivered, not acknowledged" + tail})
    for p in projects:
        if (p.get("workers") or {}).get("status") == "RUNNING":
            can.append({"project": p["name"], "who": "workers", "text": f"{(p['workers'] or {}).get('active', 0)} worker(s) running now: " + ", ".join(x["id"] for x in (p["workers"] or {}).get("workers", [])[:4])})
    return {"can_run_now": can, "needs_owner": need, "waiting": wait}


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec // 3600}h" if sec < 86400 else f"{sec // 86400}d"


def _age(posted_at: str, updated_at: float | None) -> str:
    try:
        ts = time.mktime(time.strptime(posted_at[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if posted_at else float(updated_at or time.time())
    except Exception:
        ts = float(updated_at or time.time())
    return _ago(time.time() - ts) + " ago"
