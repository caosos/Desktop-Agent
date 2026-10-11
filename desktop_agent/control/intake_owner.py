"""Owner-facing side of the intake: composing a direction to a project's coordinator and the owner's actions on an
item (transfer/resend, deny, defer, revise). Each is a receipted state change on the same intake store; the polling,
ingestion, delivery and read-back live in intake.py. Functions take the Intake instance (no second state holder)."""
from __future__ import annotations

import hashlib
import time

from .receipts import FAILED
from .events import EventType as ET


def _intake_module():
    from . import intake                     # the GitHub call stays the intake module's symbol (tests patch intake.gh_api)
    return intake


async def send_direction(intake, project: str, text: str, source: str) -> dict:
    """Create a DRAFT item, post it to the project's intake issue as the owner's direction
    (durable, visible), mark SENT; the poller then ingests it (RECEIVED) and delivers it."""
    src = intake.source_for(project)
    if not src:
        raise ValueError(f"project {project!r} has no coordinator configured")
    if (src.coordinator or {}).get("kind") == "liaison":
        if not intake.watchdog:
            raise ValueError("liaison transport unavailable")
        item_id = "da-" + hashlib.sha1(f"direction:{project}:{time.time_ns()}".encode()).hexdigest()[:10]
        item = {"item_id": item_id, "project": project, "repo": src.repo, "issue": 0, "kind": "direction", "gh_id": 0, "author": source,
                "title": text.strip().splitlines()[0][:120], "body": text, "url": "", "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "DRAFT", "coordinator": "liaison"}
        intake.store.save_intake(item)
        intake._set(item, "DRAFT", f"direction composed via {source}", [source])
        res = await intake.watchdog.send_liaison(src, item)
        intake.store.set_intake_github(item_id, 0, res.get("url", ""), item["posted_at"])
        item["url"] = res.get("url", "")
        intake._set(item, res["status"], res["note"], res["evidence"])
        return intake.store.get_intake(item_id)
    if not src.issues:
        raise ValueError(f"project {project!r} has no owner-dispatch issue configured")
    issue = src.issues[0]
    item_id = "da-" + hashlib.sha1(f"direction:{project}:{time.time_ns()}".encode()).hexdigest()[:10]
    item = {"item_id": item_id, "project": project, "repo": src.repo, "issue": issue, "kind": "direction", "gh_id": 0,
            "author": source, "title": text.strip().splitlines()[0][:120], "body": text, "url": f"https://github.com/{src.repo}/issues/{issue}",
            "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": "DRAFT", "coordinator": src.coordinator.get("kind")}
    intake.store.save_intake(item)
    intake._set(item, "DRAFT", f"direction composed via {source}", [source])
    body = f"## Owner direction via Aria Mission Control\n\n{text}\n\n<!-- da:direction {item_id} -->"
    try:
        posted = await _intake_module().gh_api(f"repos/{src.repo}/issues/{issue}/comments", method="POST", fields={"body": body})
    except Exception as exc:
        intake._set(item, "DRAFT", f"posting to GitHub failed: {exc}"[:200], [str(exc)[:200]], label=FAILED)
        return intake.store.get_intake(item_id)
    intake.store.set_intake_github(item_id, int(posted.get("id") or 0), posted.get("html_url", ""), posted.get("created_at", ""))
    item.update(url=posted.get("html_url", ""))
    intake._set(item, "SENT", f"posted to GitHub {posted.get('html_url','')}", [posted.get("html_url", "")])
    return intake.store.get_intake(item_id)

async def owner_action(intake, item_id: str, action: str, note: str, source: str) -> dict:
    """Transfer/Send, Deny, Defer, Revise on an item, each a receipted state change."""
    item = intake.store.get_intake(item_id)
    if not item:
        raise KeyError(item_id)
    if action == "deny":
        intake._set(item, "DENIED", f"owner denied via {source}: {note}", [source, note[:100]])
    elif action == "defer":
        intake._set(item, "DEFERRED", f"owner deferred via {source}: {note}", [source, note[:100]])
    elif action in ("transfer", "send", "resend"):
        src = intake.source_for(item["project"])
        if not src:
            raise ValueError("project has no coordinator configured")
        intake._emit(item, ET.CONTROL.value, {"action": "owner_transfer", "note": note[:200]}, [source])
        if (src.coordinator or {}).get("kind") == "liaison" and intake.watchdog and item["kind"] in ("direction", "liaison"):
            res = await intake.watchdog.send_liaison(src, item)
            intake._set(item, res["status"], res["note"], res["evidence"])
        else:
            await intake._deliver(src, item)
    elif action == "revise":
        if not note.strip():
            raise ValueError("revise needs the revised text")
        intake.store.update_intake_body(item_id, note)
        item["body"] = note
        intake._set(item, "DRAFT", f"revised via {source}", [source])
    else:
        raise ValueError("action must be transfer|send|resend|deny|defer|revise")
    return intake.store.get_intake(item_id)
