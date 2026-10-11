"""Planned goals: a plain-language request → the planner (one cheap structured call) → bounded contracts in
dependency order, or owner questions filed as decisions; answering re-plans. Functions take the Service."""
from __future__ import annotations

import uuid

from . import metrics, router
from .contracts import compile_contract
from .events import Actor, Event, EventType as ET, Provenance
from .receipts import FAILED, VERIFIED, write_receipt


async def plan_goal(service, *, text: str, source: str, project_hint: str | None = None,
                    worker_adapter: str = "claude_headless") -> dict:
    """Plain-language request → planner → contracts (or owner questions)."""
    goal_id = f"g-{uuid.uuid4().hex[:8]}"
    service.store.save_goal(goal_id, project_hint or "", text, source)
    service.store.append_event(Event(type=ET.GOAL_RECEIVED.value, task_id=None,
                                  payload={"goal_id": goal_id, "project": project_hint, "text": text, "planned": True},
                                  provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[f"goal={goal_id}"])))
    return await run_planner(service, goal_id, text, project_hint, [], worker_adapter)

async def run_planner(service, goal_id: str, text: str, project_hint: str | None, answers: list[dict],
                       worker_adapter: str) -> dict:
    plan = await service.planner.plan(text, project_hint, answers)
    pd = plan.to_dict()
    evidence = [f"goal={goal_id}", f"backend={pd.get('backend')}", f"model={pd.get('model')}",
                f"cost={pd.get('cost_usd')}", f"tokens={pd.get('tokens')}"]
    if plan.completion:
        service.store.add_cost(None, None, plan.completion.model, plan.completion.cost_usd,
                            plan.completion.input_tokens, plan.completion.output_tokens)
    if plan.questions:
        service.store.save_plan(goal_id, "WAITING_OWNER", pd, answers)
        for q in plan.questions:
            did = f"d-{uuid.uuid4().hex[:8]}"
            service.store.save_decision(did, None, q["question"], list(q.get("options") or []), goal_id=goal_id)
            service.store.append_event(Event(type=ET.OWNER_DECISION_REQUESTED.value, task_id=None,
                                          payload={"decision_id": did, "goal_id": goal_id, "question": q["question"],
                                                   "options": q.get("options") or [], "why": q.get("why")},
                                          provenance=Provenance(actor=Actor.CONTROL.value, source="planner", evidence=evidence)))
        write_receipt(service.store, subject_type="goal", subject_id=goal_id, claim=f"planner needs owner input: {len(plan.questions)} question(s)",
                      actor=Actor.CONTROL.value, source="planner", result_label=VERIFIED, evidence=evidence,
                      correlation_id=goal_id, after_state=pd)
        return {"goal_id": goal_id, "status": "WAITING_OWNER", "questions": plan.questions, "task_ids": []}
    if not plan.tasks:
        reason = plan.blocked_reason or "; ".join(plan.problems) or "planner produced no tasks"
        service.store.save_plan(goal_id, "BLOCKED", pd, answers)
        service.store.append_event(Event(type=ET.BLOCKED.value, task_id=None, payload={"goal_id": goal_id, "reason": reason},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source="planner", evidence=evidence)))
        write_receipt(service.store, subject_type="goal", subject_id=goal_id, claim=f"planner blocked: {reason[:200]}",
                      actor=Actor.CONTROL.value, source="planner", result_label=FAILED, evidence=evidence,
                      correlation_id=goal_id, after_state=pd)
        return {"goal_id": goal_id, "status": "BLOCKED", "reason": reason, "task_ids": []}
    pkg = service.projects[plan.project]
    task_ids: list[str] = []
    stats = metrics.outcomes(service.store)
    for t in plan.tasks:
        dupe = service.duplicate_of(plan.project, t["objective"])
        if dupe:
            plan.problems.append(f"task skipped: duplicate of open task {dupe}: {t['objective'][:80]}")
            service.store.append_event(Event(type=ET.BLOCKED.value, task_id=None,
                                          payload={"goal_id": goal_id, "reason": f"duplicate of open task {dupe}", "objective": t["objective"][:200]},
                                          provenance=Provenance(actor=Actor.CONTROL.value, source="planner", evidence=[f"duplicate_of={dupe}"])))
            continue
        chosen, why = metrics.choose_class(t["task_type"], t["model_class"], stats, router.LADDER)
        mc = router.at_least(chosen, pkg.min_model_class)
        why_now = t.get("why_now") or plan.summary
        if t.get("model_reason") or why:
            why_now += f" | model: {t.get('model_reason') or ''}{(' ; evidence: ' + why) if why else ''}"
        contract = compile_contract(
            project=pkg, objective=t["objective"], why_now=why_now, goal_id=goal_id,
            task_type=t["task_type"], owned_area=pkg.with_state_file(list(t["owned_area"])), model_class=mc,
            budget_usd=service.cfg.default_budget_usd, max_turns=service.cfg.default_max_turns,
            wall_clock_sec=service.cfg.scope.runtime_max_sec,
            acceptance_tests=None, expected_artifacts=list(t.get("expected_artifacts") or []),
            dependencies=[task_ids[i] for i in t["dependencies"] if i < len(task_ids)])
        if t.get("acceptance"):
            contract.expected_artifacts.append("acceptance: " + t["acceptance"])
        contract.worker_adapter = worker_adapter
        service._create_task(contract, source="planner")
        task_ids.append(contract.task_id)
    pd["task_ids"] = task_ids
    service.store.save_plan(goal_id, "PLANNED", pd, answers)
    write_receipt(service.store, subject_type="goal", subject_id=goal_id,
                  claim=f"planned {len(task_ids)} task(s) for {plan.project}: {plan.summary[:160]}",
                  actor=Actor.CONTROL.value, source="planner", result_label=VERIFIED, evidence=evidence,
                  correlation_id=goal_id, after_state=pd)
    return {"goal_id": goal_id, "status": "PLANNED", "project": plan.project, "summary": plan.summary,
            "task_ids": task_ids, "problems": plan.problems}
