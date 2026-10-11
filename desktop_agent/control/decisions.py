"""Owner decisions: filing a genuine owner question (inbox), the consolidated approval packet, submitting answers,
and answering one decision — each a receipted state change. Functions take the Service (single state holder);
`Service` keeps thin delegates so the API and Aria call sites are unchanged."""
from __future__ import annotations

import time
import uuid

from .events import Actor, Event, EventType as ET, Provenance
from .receipts import VERIFIED, write_receipt


def ask_owner(service, *, question: str, options: list[str], why: str, source: str,
              recommendation: str | None = None, task_id: str | None = None, goal_id: str | None = None,
              project: str | None = None, scope: str | None = None, resumes: str | None = None) -> dict:
    """File a genuine owner decision into the inbox (directive 2026-10-08 point 9)."""
    did = f"d-{uuid.uuid4().hex[:8]}"
    q = question if not recommendation else f"{question} (recommended: {recommendation})"
    service.store.save_decision(did, task_id, q, list(options), goal_id=goal_id, project=project or "desktop_agent",
                             scope=scope, resumes=resumes, recommendation=recommendation)
    service.store.append_event(Event(type=ET.OWNER_DECISION_REQUESTED.value, task_id=task_id,
                                  payload={"decision_id": did, "goal_id": goal_id, "question": q, "options": options, "why": why},
                                  provenance=Provenance(actor=Actor.CONTROL.value, source=source, evidence=[why[:200]])))
    write_receipt(service.store, subject_type="decision", subject_id=did, claim=f"owner decision requested: {question[:160]}",
                  actor=Actor.CONTROL.value, source=source, result_label=VERIFIED, evidence=[why[:200]],
                  correlation_id=goal_id, task_id=task_id, after_state={"options": options, "recommendation": recommendation})
    return {"decision_id": did, "question": q, "options": options}

def approval_packet(service) -> dict:
    """Every open owner gate at once: this platform's decisions (answerable here, one receipt each),
    plus other projects' gates read truthfully from their own files (answerable only on their channels)."""
    items = []
    for d in service.store.open_decisions():
        items.append({"decision_id": d["decision_id"], "project": d.get("project") or "desktop_agent", "question": d["question"],
                      "options": (d["options"] or ["yes", "no"]), "recommendation": d.get("recommendation"),
                      "scope": d.get("scope") or "exactly what the question says; nothing else is authorised by answering",
                      "why": (d.get("why") or why_for(service, d["decision_id"]) or ""), "resumes": d.get("resumes") or "the item that asked",
                      "asked_at": d["asked_at"], "answerable_here": True})
    external = []
    rows = service.store.coordinator_rows()
    caos = ((rows.get("caoscare") or {}).get("check") or {}).get("state") or {}
    for g in caos.get("waiting_owner") or []:
        external.append({"project": "caoscare", "gate": g, "source": "CAOSCare coordinator state script (waiting_owner)",
                         "answer_via": "a direction to CAOSCare from the Shared Inbox (posted on #117)", "answerable_here": False})
    mb = service.store.get_kv("gates:michael_business_os") or {}
    for g in mb.get("gates") or []:
        external.append({"project": "michael_business_os", **g, "answerable_here": False,
                         "answer_via": "a liaison message from the Shared Inbox (Agent 01 reads it at its own sync)"})
    historical = [{"project": "michael_business_os", **g} for g in (mb.get("history") or [])[-12:]]
    deferred = [d["decision_id"] for d in service.store.open_decisions(include_deferred=True) if d.get("deferred_until") and d["deferred_until"] > time.time()]
    return {"generated_at": time.time(), "decisions": items, "external_gates": external, "historical_gates": historical, "deferred": deferred,
            "rules": ["each answer is recorded as its own receipt", "an item left blank is not consent", "DEFER hides the item for a day and changes nothing",
                      "external gates are shown as read from the other project's own files and are answered on that project's channel"],
            "classifier_note": ("Some actions (for example a Business OS self-wake through tmux, 'W-4') are refused by the Claude Code auto-mode safety classifier. "
                                "That is a tool and security boundary, not an owner yes/no: a chat approval cannot change it, and no shell bypass or "
                                "self-edited permission policy will be proposed. Such a step is an operator action you take yourself, with a check and a rollback.")}

def why_for(service, decision_id: str) -> str | None:
    for e in reversed(service.store.events(limit=5000)):
        if e.type == ET.OWNER_DECISION_REQUESTED.value and e.payload.get("decision_id") == decision_id:
            return e.payload.get("why")
    return None

async def submit_packet(service, answers: list[dict], source: str) -> dict:
    """Answer several decisions at once; one durable receipt per answered item; DEFER hides without
    consenting; anything not listed is untouched."""
    recorded, deferred, errors = [], [], []
    for a in answers:
        did, ans = str(a.get("decision_id", "")), str(a.get("answer", "")).strip()
        if not did or not ans:
            continue
        if ans.upper() == "DEFER":
            row = service.store.defer_decision(did, time.time() + 86400)
            if row:
                service.store.append_event(Event(type=ET.CONTROL.value, task_id=row.get("task_id"), payload={"action": "decision_deferred", "decision_id": did, "until_sec": 86400},
                                              provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[did])))
                deferred.append(did)
            continue
        try:
            await service.answer_decision(did, ans, source)
            write_receipt(service.store, subject_type="decision", subject_id=did, claim=f"owner answered: {ans[:160]}", actor=Actor.HUMAN.value,
                          source=source, result_label=VERIFIED, evidence=[f"packet answer by {source}", did], after_state={"answer": ans})
            recorded.append(did)
        except KeyError:
            errors.append({"decision_id": did, "error": "no such decision"})
    return {"recorded": recorded, "deferred": deferred, "errors": errors, "remaining": [d["decision_id"] for d in service.store.open_decisions()]}

async def answer_decision(service, decision_id: str, answer: str, source: str) -> dict:
    row = service.store.answer_decision(decision_id, answer)
    if not row:
        raise KeyError(decision_id)
    service.store.append_event(Event(type=ET.OWNER_DECISION_RECORDED.value, task_id=row.get("task_id"),
                                  payload={"decision_id": decision_id, "answer": answer, "goal_id": row.get("goal_id")},
                                  provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[decision_id])))
    goal_id = row.get("goal_id")
    if goal_id and not any(d["goal_id"] == goal_id for d in service.store.open_decisions()):
        plan = service.store.get_plan(goal_id)
        goal = service.store.get_goal(goal_id)
        if plan and plan["status"] == "WAITING_OWNER" and goal:
            answers = list(plan["answers"] or []) + [{"question": row["question"], "answer": answer}]
            hint = (plan["plan"] or {}).get("project") or goal.get("project") or None
            row["replan"] = await service._run_planner(goal_id, goal["text"], hint, answers, "claude_headless")
    return row
