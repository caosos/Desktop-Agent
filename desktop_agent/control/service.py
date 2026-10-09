"""Service: wires config, store, projects, launcher, verifier, integrator and
scheduler; exposes the operations the API needs; owns the live event bus."""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path

from . import metrics, router, workspace as ws
from .adapters.claude_headless import ClaudeHeadlessAdapter
from .adapters.codex_exec import CodexExecAdapter
from .config import RuntimeConfig
from .contracts import TaskContract, compile_contract
from .events import Actor, Event, EventType as ET, Provenance, derive_stage
from .integrator import Integrator
from .launcher import Launcher
from .llm import LLM
from .planner import Planner
from .project import load_projects
from .receipts import FAILED, VERIFIED, write_receipt
from .scheduler import Scheduler
from .store import Store
from .verifier import Verifier


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


class Service:
    def __init__(self, cfg: RuntimeConfig):
        self.cfg = cfg
        self.store = Store(cfg.data_dir)
        self.projects = load_projects(cfg.project_files)
        self.adapters = {"claude_headless": ClaudeHeadlessAdapter(), "codex_exec": CodexExecAdapter()}
        self.launcher = Launcher(cfg, self.store, self.adapters)
        self.verifier = Verifier(cfg, self.store)
        self.integrator = Integrator(cfg, self.store)
        self.scheduler = Scheduler(cfg, self.store, self.launcher, self.verifier, self.integrator,
                                   self.projects, self._retry)
        self.llm = LLM(cfg)
        self.planner = Planner(self.llm, self.projects)
        from .aria import AriaBrain                  # local import: aria depends on the service surface
        self.aria = AriaBrain(self, self.llm)
        from .intake import Intake, IntakeSource
        from .intake_delivery import Deliverer
        from .watchdog import Watchdog
        sources = [IntakeSource(project=p.name, repo=p.github_repo, issues=list(p.intake.get("issues") or []),
                                owner_logins=list(p.intake.get("owner_logins") or ["caosos"]),
                                coordinator=dict(p.intake.get("coordinator") or {"kind": "control_plane"}))
                   for p in self.projects.values() if p.intake and p.github_repo and (p.intake.get("issues") or (p.intake.get("coordinator") or {}).get("liaison"))]
        self.deliverer = Deliverer(cfg, self.store)
        self.intake = Intake(self.store, sources, self.deliverer,
                             poll_sec=int(cfg.intake_poll_sec), post_comments=bool(cfg.intake_post_comments),
                             ask_owner=lambda **kw: self.ask_owner(question=kw["question"], options=kw["options"], why=kw["why"],
                                                                   source=kw["source"], recommendation=None))
        self.watchdog = Watchdog(self.store, self.intake, self.deliverer, self.scheduler)
        self.intake.watchdog = self.watchdog
        from .intake import gh_api
        from .workers import WorkerMonitor
        self.workers = WorkerMonitor(self.store, self.projects, gh_api=gh_api)   # grounded worker visibility per project
        self.watchdog.workers = self.workers
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.store.subscribe(self._fanout)
        self.started_at = time.time()

    # ---- live bus ---------------------------------------------------------
    def _fanout(self, ev: Event) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    # ---- goals / tasks ----------------------------------------------------
    def submit_goal(self, *, project: str, text: str, source: str, task_type: str = "code",
                    owned_area: list[str] | None = None, model_class: str | None = None,
                    why_now: str = "owner instruction", acceptance_tests: list[str] | None = None,
                    expected_artifacts: list[str] | None = None, budget_usd: float | None = None,
                    max_attempts: int = 3, worker_adapter: str = "claude_headless") -> dict:
        if project not in self.projects:
            raise KeyError(f"unknown project {project!r}")
        pkg = self.projects[project]
        goal_id = f"g-{uuid.uuid4().hex[:8]}"
        self.store.save_goal(goal_id, project, text, source)
        self.store.append_event(Event(type=ET.GOAL_RECEIVED.value, task_id=None, payload={"goal_id": goal_id, "project": project, "text": text},
                                      provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[f"goal={goal_id}"])))
        if worker_adapter not in self.adapters:
            raise ValueError(f"unknown worker_adapter {worker_adapter!r}; known: {sorted(self.adapters)}")
        dupe = self.duplicate_of(project, text)
        if dupe:
            raise ValueError(f"duplicate of open task {dupe}; stop or finish it first")
        mc = router.at_least(model_class or router.default_class(task_type), pkg.min_model_class)
        router.resolve(self.cfg, mc, worker_adapter)
        contract = compile_contract(
            project=pkg, objective=text, why_now=why_now, goal_id=goal_id, task_type=task_type,
            owned_area=pkg.with_state_file(owned_area or []), model_class=mc,
            budget_usd=budget_usd or self.cfg.default_budget_usd, max_turns=self.cfg.default_max_turns,
            wall_clock_sec=self.cfg.scope.runtime_max_sec, acceptance_tests=acceptance_tests,
            expected_artifacts=expected_artifacts, max_attempts=max_attempts)
        contract.worker_adapter = worker_adapter
        self._create_task(contract, source=source)
        write_receipt(self.store, subject_type="goal", subject_id=goal_id, claim=f"goal accepted; task {contract.task_id} created",
                      actor=Actor.CONTROL.value, source="service", result_label=VERIFIED,
                      evidence=[f"contract={contract.hash()}"], correlation_id=goal_id)
        return {"goal_id": goal_id, "task_ids": [contract.task_id]}

    # ---- planned goals ----------------------------------------------------
    async def plan_goal(self, *, text: str, source: str, project_hint: str | None = None,
                        worker_adapter: str = "claude_headless") -> dict:
        """Plain-language request → planner → contracts (or owner questions)."""
        goal_id = f"g-{uuid.uuid4().hex[:8]}"
        self.store.save_goal(goal_id, project_hint or "", text, source)
        self.store.append_event(Event(type=ET.GOAL_RECEIVED.value, task_id=None,
                                      payload={"goal_id": goal_id, "project": project_hint, "text": text, "planned": True},
                                      provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[f"goal={goal_id}"])))
        return await self._run_planner(goal_id, text, project_hint, [], worker_adapter)

    async def _run_planner(self, goal_id: str, text: str, project_hint: str | None, answers: list[dict],
                           worker_adapter: str) -> dict:
        plan = await self.planner.plan(text, project_hint, answers)
        pd = plan.to_dict()
        evidence = [f"goal={goal_id}", f"backend={pd.get('backend')}", f"model={pd.get('model')}",
                    f"cost={pd.get('cost_usd')}", f"tokens={pd.get('tokens')}"]
        if plan.completion:
            self.store.add_cost(None, None, plan.completion.model, plan.completion.cost_usd,
                                plan.completion.input_tokens, plan.completion.output_tokens)
        if plan.questions:
            self.store.save_plan(goal_id, "WAITING_OWNER", pd, answers)
            for q in plan.questions:
                did = f"d-{uuid.uuid4().hex[:8]}"
                self.store.save_decision(did, None, q["question"], list(q.get("options") or []), goal_id=goal_id)
                self.store.append_event(Event(type=ET.OWNER_DECISION_REQUESTED.value, task_id=None,
                                              payload={"decision_id": did, "goal_id": goal_id, "question": q["question"],
                                                       "options": q.get("options") or [], "why": q.get("why")},
                                              provenance=Provenance(actor=Actor.CONTROL.value, source="planner", evidence=evidence)))
            write_receipt(self.store, subject_type="goal", subject_id=goal_id, claim=f"planner needs owner input: {len(plan.questions)} question(s)",
                          actor=Actor.CONTROL.value, source="planner", result_label=VERIFIED, evidence=evidence,
                          correlation_id=goal_id, after_state=pd)
            return {"goal_id": goal_id, "status": "WAITING_OWNER", "questions": plan.questions, "task_ids": []}
        if not plan.tasks:
            reason = plan.blocked_reason or "; ".join(plan.problems) or "planner produced no tasks"
            self.store.save_plan(goal_id, "BLOCKED", pd, answers)
            self.store.append_event(Event(type=ET.BLOCKED.value, task_id=None, payload={"goal_id": goal_id, "reason": reason},
                                          provenance=Provenance(actor=Actor.CONTROL.value, source="planner", evidence=evidence)))
            write_receipt(self.store, subject_type="goal", subject_id=goal_id, claim=f"planner blocked: {reason[:200]}",
                          actor=Actor.CONTROL.value, source="planner", result_label=FAILED, evidence=evidence,
                          correlation_id=goal_id, after_state=pd)
            return {"goal_id": goal_id, "status": "BLOCKED", "reason": reason, "task_ids": []}
        pkg = self.projects[plan.project]
        task_ids: list[str] = []
        stats = metrics.outcomes(self.store)
        for t in plan.tasks:
            dupe = self.duplicate_of(plan.project, t["objective"])
            if dupe:
                plan.problems.append(f"task skipped: duplicate of open task {dupe}: {t['objective'][:80]}")
                self.store.append_event(Event(type=ET.BLOCKED.value, task_id=None,
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
                budget_usd=self.cfg.default_budget_usd, max_turns=self.cfg.default_max_turns,
                wall_clock_sec=self.cfg.scope.runtime_max_sec,
                acceptance_tests=None, expected_artifacts=list(t.get("expected_artifacts") or []),
                dependencies=[task_ids[i] for i in t["dependencies"] if i < len(task_ids)])
            if t.get("acceptance"):
                contract.expected_artifacts.append("acceptance: " + t["acceptance"])
            contract.worker_adapter = worker_adapter
            self._create_task(contract, source="planner")
            task_ids.append(contract.task_id)
        pd["task_ids"] = task_ids
        self.store.save_plan(goal_id, "PLANNED", pd, answers)
        write_receipt(self.store, subject_type="goal", subject_id=goal_id,
                      claim=f"planned {len(task_ids)} task(s) for {plan.project}: {plan.summary[:160]}",
                      actor=Actor.CONTROL.value, source="planner", result_label=VERIFIED, evidence=evidence,
                      correlation_id=goal_id, after_state=pd)
        return {"goal_id": goal_id, "status": "PLANNED", "project": plan.project, "summary": plan.summary,
                "task_ids": task_ids, "problems": plan.problems}

    def duplicate_of(self, project: str, objective: str, threshold: float = 0.6) -> str | None:
        """An open (READY/RUNNING) task in the same project whose objective overlaps this one
        heavily; two workers must not do the same work (directive 2026-10-08 point 2)."""
        words = {w for w in re.findall(r"[a-z0-9_]+", objective.lower()) if len(w) > 2}
        if len(words) < 4:
            return None
        for t in self.store.list_tasks(status="READY") + self.store.list_tasks(status="RUNNING"):
            if t["project"] != project:
                continue
            other = {w for w in re.findall(r"[a-z0-9_]+", (t["contract"].get("objective") or "").lower()) if len(w) > 2}
            if other and len(words & other) / len(words | other) >= threshold:
                return t["task_id"]
        return None

    def _create_task(self, contract: TaskContract, source: str) -> None:
        self.store.save_task(contract.task_id, contract.goal_id, contract.project, "READY", contract.to_dict(), contract.hash())
        self.store.append_event(Event(type=ET.TASK_CREATED.value, task_id=contract.task_id,
                                      payload={"objective": contract.objective, "model_class": contract.model_class,
                                               "attempt": contract.attempt, "owned_area": contract.owned_area},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source=source,
                                                            contract_hash=contract.hash(), evidence=[f"goal={contract.goal_id}"])))

    def _retry(self, old: TaskContract, model_class: str | None) -> TaskContract:
        new = TaskContract.from_dict(old.to_dict())
        new.task_id = f"{old.task_id.rsplit('-', 1)[0]}-{uuid.uuid4().hex[:6]}"
        new.attempt = old.attempt + 1
        new.supersedes = old.task_id
        new.model_class = model_class or old.model_class
        new.result = None
        new.created_at = time.time()
        self.store.save_task(new.task_id, new.goal_id, new.project, "READY", new.to_dict(), new.hash())
        return new

    # ---- control ----------------------------------------------------------
    async def control(self, action: str, task_id: str | None, source: str) -> dict:
        if action == "pause":
            self.scheduler.paused = True
        elif action == "resume":
            self.scheduler.paused = False
            self.scheduler.hold_reason = None
        elif action == "stop":
            if task_id:
                await self.launcher.kill(task_id, "owner stop")
            else:
                for tid in list(self.launcher.running):
                    await self.launcher.kill(tid, "owner stop")
                self.scheduler.paused = True
        else:
            raise ValueError("action must be pause|resume|stop")
        self.store.append_event(Event(type=ET.CONTROL.value, task_id=task_id, payload={"action": action},
                                      provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[action])))
        return {"paused": self.scheduler.paused, "running": list(self.launcher.running)}

    def ask_owner(self, *, question: str, options: list[str], why: str, source: str,
                  recommendation: str | None = None, task_id: str | None = None, goal_id: str | None = None,
                  project: str | None = None, scope: str | None = None, resumes: str | None = None) -> dict:
        """File a genuine owner decision into the inbox (directive 2026-10-08 point 9)."""
        did = f"d-{uuid.uuid4().hex[:8]}"
        q = question if not recommendation else f"{question} (recommended: {recommendation})"
        self.store.save_decision(did, task_id, q, list(options), goal_id=goal_id, project=project or "desktop_agent",
                                 scope=scope, resumes=resumes, recommendation=recommendation)
        self.store.append_event(Event(type=ET.OWNER_DECISION_REQUESTED.value, task_id=task_id,
                                      payload={"decision_id": did, "goal_id": goal_id, "question": q, "options": options, "why": why},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source=source, evidence=[why[:200]])))
        write_receipt(self.store, subject_type="decision", subject_id=did, claim=f"owner decision requested: {question[:160]}",
                      actor=Actor.CONTROL.value, source=source, result_label=VERIFIED, evidence=[why[:200]],
                      correlation_id=goal_id, task_id=task_id, after_state={"options": options, "recommendation": recommendation})
        return {"decision_id": did, "question": q, "options": options}

    # ---- consolidated approval packet ----------------------------------------------------
    def approval_packet(self) -> dict:
        """Every open owner gate at once: this platform's decisions (answerable here, one receipt each),
        plus other projects' gates read truthfully from their own files (answerable only on their channels)."""
        items = []
        for d in self.store.open_decisions():
            items.append({"decision_id": d["decision_id"], "project": d.get("project") or "desktop_agent", "question": d["question"],
                          "options": (d["options"] or ["yes", "no"]), "recommendation": d.get("recommendation"),
                          "scope": d.get("scope") or "exactly what the question says; nothing else is authorised by answering",
                          "why": (self._why_for(d["decision_id"]) or ""), "resumes": d.get("resumes") or "the item that asked",
                          "asked_at": d["asked_at"], "answerable_here": True})
        external = []
        rows = self.store.coordinator_rows()
        caos = ((rows.get("caoscare") or {}).get("check") or {}).get("state") or {}
        for g in caos.get("waiting_owner") or []:
            external.append({"project": "caoscare", "gate": g, "source": "CAOSCare coordinator state script (waiting_owner)",
                             "answer_via": "a direction to CAOSCare from the Shared Inbox (posted on #117)", "answerable_here": False})
        for g in (self.store.get_kv("gates:michael_business_os") or {}).get("gates") or []:
            external.append({"project": "michael_business_os", **g, "answerable_here": False,
                             "answer_via": "a liaison message from the Shared Inbox (Agent 01 reads it at its own sync)"})
        deferred = [d["decision_id"] for d in self.store.open_decisions(include_deferred=True) if d.get("deferred_until") and d["deferred_until"] > time.time()]
        return {"generated_at": time.time(), "decisions": items, "external_gates": external, "deferred": deferred,
                "rules": ["each answer is recorded as its own receipt", "an item left blank is not consent", "DEFER hides the item for a day and changes nothing",
                          "external gates are shown as read from the other project's own files and are answered on that project's channel"],
                "classifier_note": ("Some actions (for example a Business OS self-wake through tmux, 'W-4') are refused by the Claude Code auto-mode safety classifier. "
                                    "That is a tool and security boundary, not an owner yes/no: a chat approval cannot change it, and no shell bypass or "
                                    "self-edited permission policy will be proposed. Such a step is an operator action you take yourself, with a check and a rollback.")}

    def _why_for(self, decision_id: str) -> str | None:
        for e in reversed(self.store.events(limit=5000)):
            if e.type == ET.OWNER_DECISION_REQUESTED.value and e.payload.get("decision_id") == decision_id:
                return e.payload.get("why")
        return None

    async def submit_packet(self, answers: list[dict], source: str) -> dict:
        """Answer several decisions at once; one durable receipt per answered item; DEFER hides without
        consenting; anything not listed is untouched."""
        recorded, deferred, errors = [], [], []
        for a in answers:
            did, ans = str(a.get("decision_id", "")), str(a.get("answer", "")).strip()
            if not did or not ans:
                continue
            if ans.upper() == "DEFER":
                row = self.store.defer_decision(did, time.time() + 86400)
                if row:
                    self.store.append_event(Event(type=ET.CONTROL.value, task_id=row.get("task_id"), payload={"action": "decision_deferred", "decision_id": did, "until_sec": 86400},
                                                  provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[did])))
                    deferred.append(did)
                continue
            try:
                await self.answer_decision(did, ans, source)
                write_receipt(self.store, subject_type="decision", subject_id=did, claim=f"owner answered: {ans[:160]}", actor=Actor.HUMAN.value,
                              source=source, result_label=VERIFIED, evidence=[f"packet answer by {source}", did], after_state={"answer": ans})
                recorded.append(did)
            except KeyError:
                errors.append({"decision_id": did, "error": "no such decision"})
        return {"recorded": recorded, "deferred": deferred, "errors": errors, "remaining": [d["decision_id"] for d in self.store.open_decisions()]}

    async def answer_decision(self, decision_id: str, answer: str, source: str) -> dict:
        row = self.store.answer_decision(decision_id, answer)
        if not row:
            raise KeyError(decision_id)
        self.store.append_event(Event(type=ET.OWNER_DECISION_RECORDED.value, task_id=row.get("task_id"),
                                      payload={"decision_id": decision_id, "answer": answer, "goal_id": row.get("goal_id")},
                                      provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[decision_id])))
        goal_id = row.get("goal_id")
        if goal_id and not any(d["goal_id"] == goal_id for d in self.store.open_decisions()):
            plan = self.store.get_plan(goal_id)
            goal = self.store.get_goal(goal_id)
            if plan and plan["status"] == "WAITING_OWNER" and goal:
                answers = list(plan["answers"] or []) + [{"question": row["question"], "answer": answer}]
                hint = (plan["plan"] or {}).get("project") or goal.get("project") or None
                row["replan"] = await self._run_planner(goal_id, goal["text"], hint, answers, "claude_headless")
        return row

    # ---- read model -------------------------------------------------------
    def task_view(self, row: dict) -> dict:
        evs = self.store.events(row["task_id"])
        last = evs[-1] if evs else None
        return {
            "task_id": row["task_id"], "project": row["project"], "status": row["status"],
            "stage": derive_stage(evs).value, "objective": row["contract"].get("objective"),
            "model_class": row["contract"].get("model_class"), "attempt": row["contract"].get("attempt"),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "last_activity": describe(last) if last else "", "last_activity_at": last.ts if last else row["created_at"],
            "result": row.get("result"),
        }

    def state(self) -> dict:
        tasks = [self.task_view(t) for t in self.store.list_tasks()]
        projects = []
        for name, pkg in self.projects.items():
            ptasks = [t for t in tasks if t["project"] == name]
            active = [t for t in ptasks if t["status"] in ("READY", "RUNNING")]
            newest = max(ptasks, key=lambda t: t["last_activity_at"], default=None)
            running = [t for t in active if t["status"] == "RUNNING"]
            waiting = [d for d in self.store.open_decisions()]
            if running:
                stage = running[0]["stage"]
            elif active:
                stage = "PLANNING"                     # READY tasks waiting for a slot
            elif any(t["status"] == "BLOCKED" for t in ptasks[-3:]) and waiting:
                stage = "WAITING_OWNER"
            else:
                stage = "IDLE"                         # nothing active; history stays in the task list
            coord = self.coordinator_state(pkg)
            projects.append({"name": name, "integration_branch": pkg.integration_branch, "stage": stage,
                             "coordinator": coord,
                             "workers": self.workers.snapshot(pkg, ptasks, self.scheduler, last_confirmed=coord.get("last_ack_at")),
                             "open_tasks": len(active), "blocked_tasks": sum(1 for t in ptasks if t["status"] == "BLOCKED"),
                             "done_tasks": sum(1 for t in ptasks if t["status"] == "DONE"),
                             "last_activity": newest["last_activity"] if newest else "",
                             "last_activity_at": newest["last_activity_at"] if newest else None})
        workers = [{"worker_id": r.worker_id, "task_id": tid, "started_at": r.started_at, "last_event_at": r.last_event_at}
                   for tid, r in self.launcher.running.items()]
        slots = self.scheduler.slots()
        hold = self.scheduler.hold_reason if self.scheduler.paused else None
        inbox = self.store.open_decisions()
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
        if any(t["status"] == "READY" for t in tasks) and slots["free"] == 0 and not self.scheduler.paused:
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
            "ts": time.time(), "uptime_sec": round(time.time() - self.started_at), "paused": self.scheduler.paused,
            "projects": projects, "tasks": tasks[-50:], "workers": workers,
            "inbox": inbox, "costs": self.store.cost_summary(),
            "blocked": blocked_list[-20:], "glance": glance,
            "next": next((t for t in tasks if t["status"] == "READY"), None),
            "slots": slots, "hold": hold, "class_labels": dict(self.cfg.class_labels), "last_seq": self.store.last_seq(),
            "feedback": metrics.feedback(self.store),
            "budgets": self.budgets(),
            "llm": self.llm.status(),
            "intake": self.intake.summary(),
            "quota": self.store.get_kv("quota"),
            "outcomes": [{"task_type": k[0], "model_class": k[1], **v} for k, v in metrics.outcomes(self.store).items()],
        }

    def coordinator_state(self, pkg) -> dict:
        """Who coordinates this project, whether it is reachable now, when it was last checked, woken
        and heard from, and whether wake is VERIFIED, manual-only or unavailable. Never invents a status."""
        from .intake_delivery import Deliverer
        ik = pkg.intake or {}
        coord = ik.get("coordinator") or {}
        kind = coord.get("kind")
        row = self.store.coordinator_rows().get(pkg.name) or {}
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
                return base | {"connected": True, "detail": f"session {s['name']} ({s['status']})", "session": s["name"], "session_status": s["status"],
                               "wake": "VERIFIED (delivery → ACK observed)" if verified else "delivered, ACK pending" if row.get("last_wake") else "untested"}
            return base | {"connected": False, "detail": "Disconnected: no live coordinator session; items queue until one appears",
                           "wake": "unavailable (no session)"}
        if kind == "liaison":
            return base | {"connected": False, "detail": "liaison branch inbox; coordinator runs under another account, reached only by its own sync",
                           "wake": "manual-only (coordinator syncs the liaison branch itself)"}
        return base | {"connected": False, "detail": "Disconnected / Data unavailable: no coordinator integration configured", "wake": "unavailable"}

    def budgets(self) -> dict:
        """Caps, actual spend against them, and usage with no dollar figure (never estimated)."""
        c = self.store.cost_summary()
        s = self.cfg.scheduler
        return {"hourly_cap_usd": s.hourly_cap_usd, "daily_cap_usd": s.daily_cap_usd,
                "per_task_default_usd": self.cfg.default_budget_usd,
                "spent_last_hour_usd": c["last_hour_usd"], "spent_today_usd": c["today_usd"],
                "remaining_hour_usd": round(max(0.0, s.hourly_cap_usd - c["last_hour_usd"]), 4),
                "remaining_today_usd": round(max(0.0, s.daily_cap_usd - c["today_usd"]), 4),
                "unknown_usage": c["unknown_usage"],
                "note": "unknown_usage = subscription executors or unpriced models: tokens recorded, dollars not estimated"}

    async def task_detail(self, task_id: str) -> dict | None:
        row = self.store.get_task(task_id)
        if not row:
            return None
        view = self.task_view(row)
        evs = self.store.events(task_id)
        view["events"] = [{"seq": e.seq, "ts": e.ts, "type": e.type, "text": describe(e), "actor": e.provenance.actor} for e in evs[-200:]]
        view["contract"] = row["contract"]
        view["receipts"] = self.store.receipts("task", task_id)
        wsdir = self.cfg.workspaces_dir / task_id / "repo"
        base = next((e.payload.get("base_sha") for e in evs if e.type == ET.WORKER_STARTED.value), None)
        if wsdir.exists() and base:
            try:
                view["files"] = await ws.changed_files(wsdir, base)
                view["diff"] = await ws.diff_text(wsdir, base, 120_000)
            except ws.GitError as exc:
                view["diff_error"] = str(exc)
        return view

    async def _intake_forever(self) -> None:
        while True:
            try:
                await self.intake.poll_once()
            except Exception as exc:          # recorded, never fatal
                self.intake.last_error = repr(exc)[:300]
            try:
                await self.watchdog.tick()
            except Exception as exc:
                self.intake.last_error = f"watchdog: {exc!r}"[:300]
            await asyncio.sleep(self.intake.poll_sec)

    # ---- loop -------------------------------------------------------------
    async def run_forever(self) -> None:
        if self.intake.sources:
            asyncio.create_task(self._intake_forever())
        while True:
            try:
                await self.scheduler.tick()
            except Exception as exc:  # the loop must survive; record and go on
                self.store.append_event(Event(type=ET.CONTROL.value, task_id=None, payload={"action": "tick_error", "error": repr(exc)[:300]},
                                              provenance=Provenance(actor=Actor.CONTROL.value, source="scheduler", evidence=[repr(exc)[:100]])))
            await asyncio.sleep(self.cfg.scheduler.tick_sec)
