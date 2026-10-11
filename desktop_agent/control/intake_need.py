"""The escalation receipt body for an instruction nobody has taken (AGENTS.md protocol): classification, the exact
blocker, one copyable action, impact and the next check — never a bare "need permission"."""
from __future__ import annotations


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
