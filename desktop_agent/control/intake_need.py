"""Pending-item supervision for the intake (AGENTS.md escalation protocol): re-hand undelivered items once a session
resolves, flag unacknowledged ones after the threshold with a NEED body (classification, exact blocker, one copyable
action, impact, next check — never a bare "need permission")."""
from __future__ import annotations

import time

from .events import EventType as ET


def need_for(item: dict, sources, poll_sec: int) -> dict:
    src = next((s for s in sources if s.project == item["project"]), None)
    coord = (src.coordinator if src else None) or {}
    cwd = coord.get("session_cwd_exact") or coord.get("session_cwd") or "/home/caoscare-1"
    start = f"cd {cwd} && claude"
    if item["status"] == "RECEIVED" and coord.get("kind") == "claude_peer":
        return {"classification": "NEED_HOST_ACTION", "blocker": item["note"][:200],
                "action": f"start the coordinator session: {start}   (then set intake.coordinator.session_name in the project yaml and restart desktop-agent)",
                "impact": f"owner instruction {item['item_id']} for {item['project']} is not in any coordinator's hands",
                "next_check_sec": poll_sec}
    if item["status"] == "RECEIVED":
        return {"classification": "NEED_HOST_ACTION", "blocker": item["note"][:200],
                "action": "the project's own coordinator must read its inbox (liaison or control-plane path); nothing to run from this account",
                "impact": f"owner instruction {item['item_id']} for {item['project']} not acknowledged", "next_check_sec": poll_sec}
    return {"classification": "NEED_SERVICE", "blocker": f"delivered but no ACK comment from the coordinator: {item['note'][:160]}",
            "action": f"re-send from the panel (Transfer) or wake the session; if it is dead, start it: {start}",
            "impact": f"owner instruction {item['item_id']} for {item['project']} delivered, not acknowledged", "next_check_sec": poll_sec}


async def redeliver_pending(intake) -> int:
    """An item that could not be handed to a Claude peer (no live session, relay failure) is tried again on every
    poll once a session resolves — the same item row, so no duplicate; the free session check gates the relay call."""
    probe = getattr(intake.deliver, "session_for", None)
    by_project = {s.project: s for s in intake.sources}
    n = 0
    for item in intake.store.list_intake():
        src = by_project.get(item["project"])
        if not src or item["status"] != "RECEIVED" or (src.coordinator or {}).get("kind") != "claude_peer":
            continue
        if item["note"].startswith("ingested from"):
            continue                                # brand new: delivered by the ingest path of this same poll
        if probe is not None and probe(src) is None:
            continue                                # still nobody to hand it to; the flag/NEED path says so
        n += await intake._deliver(src, item)
    return n


def flag_unacked(intake) -> None:
    now = time.time()
    for item in intake.store.list_intake():
        if item["status"] in ("RECEIVED", "DELIVERED") and now - item["updated_at"] > intake.unacked_after_sec and not item.get("flagged"):
            intake.store.flag_intake(item["item_id"])
            need = need_for(item, intake.sources, intake.poll_sec)
            intake._emit(item, ET.BLOCKED.value, {"reason": f"instruction unacknowledged for {int((now - item['updated_at']) / 60)} min",
                                                 "project": item["project"], "url": item["url"], "need": need},
                       [item["url"], need["classification"], need["action"]])
