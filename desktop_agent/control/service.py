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
from . import allowance
from .project import load_projects
from .receipts import FAILED, VERIFIED, write_receipt
from .scheduler import Scheduler
from .store import Store
from .verifier import Verifier
from . import decisions as _decisions, goals_planned as _goals_planned, state_view as _state_view
from .state_view import IntakeSourceLite, describe  # noqa: F401  (re-exported: api.py imports describe from here)


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
        from .paid_budget import PaidBudget          # the one hard daily budget for paid API spend (owner directive da-8708eb5ec7)
        self.paid = PaidBudget(self.store)
        self.llm = LLM(cfg)
        self.llm.gate = self.paid
        self.planner = Planner(self.llm, self.projects)
        from ..memory import MemoryService           # owner's private memory (da-c6d1e57152): private dir only, never in the store, events or workers
        self.memory = MemoryService(cfg.private_dir)
        from .aria import AriaBrain                  # local import: aria depends on the service surface
        self.aria = AriaBrain(self, self.llm)
        from .calling import CallService             # SIMULATED calling loop only (docs/CALLING_BRIEF.md)
        self.calling = CallService(self.store, self.aria)
        from .research import ResearchService        # research jobs: one bounded worker, local sources (docs/RESEARCH_BRIEF.md)
        self.research = ResearchService(self.store, self.aria, self)
        from .intake import Intake, IntakeSource
        from . import intake_delivery
        from .intake_delivery import Deliverer
        from .watchdog import Watchdog
        sources = [IntakeSource(project=p.name, repo=p.github_repo, issues=list(p.intake.get("issues") or []),
                                owner_logins=list(p.intake.get("owner_logins") or ["caosos"]),
                                coordinator=dict(p.intake.get("coordinator") or {"kind": "control_plane"}), handoff=bool(p.intake.get("handoff")), bridge_pr=p.intake.get("bridge_pr"))
                   for p in self.projects.values() if p.intake and p.github_repo and (p.intake.get("issues") or (p.intake.get("coordinator") or {}).get("liaison"))]
        self.deliverer = Deliverer(cfg, self.store)
        intake_delivery.register_pins(self.projects)      # another project's pinned coordinator is never this project's stand-in
        self.intake = Intake(self.store, sources, self.deliverer,
                             poll_sec=int(cfg.intake_poll_sec), post_comments=bool(cfg.intake_post_comments), unacked_after_sec=int(cfg.intake_unacked_sec),
                             ask_owner=lambda **kw: self.ask_owner(question=kw["question"], options=kw["options"], why=kw["why"],
                                                                   source=kw["source"], recommendation=None))
        self.watchdog = Watchdog(self.store, self.intake, self.deliverer, self.scheduler)
        self.watchdog.allowance_path = cfg.data_dir / allowance.ALLOWANCE_FILE
        self.intake.watchdog = self.watchdog
        from .handoff import Handoff                      # DONE → next eligible queue row, for projects that opt in (intake.handoff)
        self.intake.handoff = Handoff(self.store, self.intake, lambda project: Path(self.projects[project].repo_path) / "docs" / "BUILD_CHECKLIST.md", cfg)
        from . import intake as _intake_mod
        from .bridge import Bridge                         # transport-only receipt mirror to one draft PR (intake.bridge_pr); idle without it
        async def _bridge_post(path, fields): return await _intake_mod.gh_api(path, method="POST", fields=fields)
        self.intake.bridge = Bridge(self.store, _bridge_post, self.watchdog.checks.loaded_head, self.watchdog.checks._host_evidence)
        self.slack = None
        if cfg.slack_channel and cfg.slack_owner:               # immediate Slack ingress: idle (NEED_ACCESS event) until slack.env exists
            from .slack_ingress import SlackIngress
            from .slack_socket import SlackSocket
            env = Path("~/.config/desktop-agent/slack.env").expanduser()
            self.slack = SlackSocket(self.store, None, env)
            async def _mirror(project, text):                       # one comment on the routed project's existing transport-only bridge PR
                src = self.intake.source_for(project)
                if src and getattr(src, "bridge_pr", None):
                    from . import intake as _im
                    await _im.gh_api(f"repos/{src.repo}/issues/{src.bridge_pr}/comments", method="POST", fields={"body": text})
            self.slack.ingress = SlackIngress(self.store, self.intake, self.slack.post, cfg.slack_channel, cfg.slack_owner, mirror=_mirror)
        from .intake import gh_api
        from .workers import WorkerMonitor
        self.workers = WorkerMonitor(self.store, self.projects, gh_api=gh_api)   # grounded worker visibility per project
        self.watchdog.workers = self.workers
        from .drilldown import Drilldown
        self.drill = Drilldown(self)                                            # per-project drilldown from existing sources
        self.watchdog.drill = self.drill
        self._subscribers: list[asyncio.Queue] = []
        self.stopping = False
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

    def stop(self) -> None:
        """Shutdown: release every open event stream (a `None` wakes its wait) so connections can close."""
        self.stopping = True
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    # ---- goals / tasks ----------------------------------------------------
    def submit_goal(self, *, project: str, text: str, source: str, task_type: str = "code",
                    owned_area: list[str] | None = None, model_class: str | None = None,
                    why_now: str = "owner instruction", acceptance_tests: list[str] | None = None,
                    expected_artifacts: list[str] | None = None, budget_usd: float | None = None,
                    max_attempts: int = 3, worker_adapter: str = "claude_headless", check_duplicate: bool = True,
                    allowed_tools: list[str] | None = None) -> dict:
        if project not in self.projects:
            raise KeyError(f"unknown project {project!r}")
        pkg = self.projects[project]
        goal_id = f"g-{uuid.uuid4().hex[:8]}"
        self.store.save_goal(goal_id, project, text, source)
        self.store.append_event(Event(type=ET.GOAL_RECEIVED.value, task_id=None, payload={"goal_id": goal_id, "project": project, "text": text},
                                      provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[f"goal={goal_id}"])))
        if worker_adapter not in self.adapters:
            raise ValueError(f"unknown worker_adapter {worker_adapter!r}; known: {sorted(self.adapters)}")
        dupe = self.duplicate_of(project, text) if check_duplicate else None   # research jobs share a long common brief; they have their own turn dedup
        if dupe:
            raise ValueError(f"duplicate of open task {dupe}; stop or finish it first")
        mc = router.at_least(model_class or router.default_class(task_type), pkg.min_model_class)
        router.resolve(self.cfg, mc, worker_adapter)
        contract = compile_contract(
            project=pkg, objective=text, why_now=why_now, goal_id=goal_id, task_type=task_type,
            owned_area=pkg.with_state_file(owned_area or []), model_class=mc,
            budget_usd=budget_usd or self.cfg.default_budget_usd, max_turns=self.cfg.default_max_turns,
            wall_clock_sec=self.cfg.scope.runtime_max_sec, acceptance_tests=acceptance_tests,
            expected_artifacts=expected_artifacts, max_attempts=max_attempts, allowed_tools=allowed_tools)
        contract.worker_adapter = worker_adapter
        self._create_task(contract, source=source)
        write_receipt(self.store, subject_type="goal", subject_id=goal_id, claim=f"goal accepted; task {contract.task_id} created",
                      actor=Actor.CONTROL.value, source="service", result_label=VERIFIED,
                      evidence=[f"contract={contract.hash()}"], correlation_id=goal_id)
        return {"goal_id": goal_id, "task_ids": [contract.task_id]}

    # ---- planned goals (goals_planned.py) ---------------------------------
    async def plan_goal(self, *, text: str, source: str, project_hint: str | None = None,
                        worker_adapter: str = "claude_headless") -> dict:
        return await _goals_planned.plan_goal(self, text=text, source=source, project_hint=project_hint, worker_adapter=worker_adapter)

    async def _run_planner(self, goal_id: str, text: str, project_hint: str | None, answers: list[dict], worker_adapter: str) -> dict:
        return await _goals_planned.run_planner(self, goal_id, text, project_hint, answers, worker_adapter)


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

    def ask_owner(self, **kw) -> dict:
        return _decisions.ask_owner(self, **kw)

    def approval_packet(self) -> dict:
        return _decisions.approval_packet(self)

    async def submit_packet(self, answers: list[dict], source: str) -> dict:
        return await _decisions.submit_packet(self, answers, source)

    async def answer_decision(self, decision_id: str, answer: str, source: str) -> dict:
        return await _decisions.answer_decision(self, decision_id, answer, source)


    # ---- read model (state_view.py) ---------------------------------------
    def task_view(self, row: dict) -> dict:
        return _state_view.task_view(self, row)

    def state(self) -> dict:
        return _state_view.state(self)

    def coordinator_state(self, pkg) -> dict:
        return _state_view.coordinator_state(self, pkg)

    def budgets(self) -> dict:
        return _state_view.budgets(self)

    async def task_detail(self, task_id: str) -> dict | None:
        return await _state_view.task_detail(self, task_id)


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
        intake_task = asyncio.create_task(self._intake_forever()) if self.intake.sources else None
        slack_task = asyncio.create_task(self.slack.run()) if self.slack else None
        try:
            while True:
                try:
                    await self.scheduler.tick()
                    self.research.reconcile()
                except Exception as exc:  # the loop must survive; record and go on
                    self.store.append_event(Event(type=ET.CONTROL.value, task_id=None, payload={"action": "tick_error", "error": repr(exc)[:300]},
                                                  provenance=Provenance(actor=Actor.CONTROL.value, source="scheduler", evidence=[repr(exc)[:100]])))
                await asyncio.sleep(self.cfg.scheduler.tick_sec)
        finally:                                  # cancelled at shutdown: take the poll loop down with us
            if intake_task:
                intake_task.cancel()
            if slack_task:
                slack_task.cancel()
