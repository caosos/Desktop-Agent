"""Service: wires config, store, projects, launcher, verifier, integrator and
scheduler; exposes the operations the API needs; owns the live event bus."""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from . import router, workspace as ws
from .adapters.claude_headless import ClaudeHeadlessAdapter
from .config import RuntimeConfig
from .contracts import TaskContract, compile_contract
from .events import Actor, Event, EventType as ET, Provenance, derive_stage
from .integrator import Integrator
from .launcher import Launcher
from .project import load_projects
from .receipts import VERIFIED, write_receipt
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


class Service:
    def __init__(self, cfg: RuntimeConfig):
        self.cfg = cfg
        self.store = Store(cfg.data_dir)
        self.projects = load_projects(cfg.project_files)
        self.adapters = {"claude_headless": ClaudeHeadlessAdapter()}
        self.launcher = Launcher(cfg, self.store, self.adapters)
        self.verifier = Verifier(cfg, self.store)
        self.integrator = Integrator(cfg, self.store)
        self.scheduler = Scheduler(cfg, self.store, self.launcher, self.verifier, self.integrator,
                                   self.projects, self._retry)
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
                    expected_artifacts: list[str] | None = None, budget_usd: float | None = None) -> dict:
        if project not in self.projects:
            raise KeyError(f"unknown project {project!r}")
        pkg = self.projects[project]
        goal_id = f"g-{uuid.uuid4().hex[:8]}"
        self.store.save_goal(goal_id, project, text, source)
        self.store.append_event(Event(type=ET.GOAL_RECEIVED.value, task_id=None, payload={"goal_id": goal_id, "project": project, "text": text},
                                      provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[f"goal={goal_id}"])))
        mc = router.at_least(model_class or router.default_class(task_type), pkg.min_model_class)
        router.resolve(self.cfg, mc)
        contract = compile_contract(
            project=pkg, objective=text, why_now=why_now, goal_id=goal_id, task_type=task_type,
            owned_area=owned_area or [], model_class=mc,
            budget_usd=budget_usd or self.cfg.default_budget_usd, max_turns=self.cfg.default_max_turns,
            wall_clock_sec=self.cfg.scope.runtime_max_sec, acceptance_tests=acceptance_tests,
            expected_artifacts=expected_artifacts)
        self._create_task(contract, source=source)
        write_receipt(self.store, subject_type="goal", subject_id=goal_id, claim=f"goal accepted; task {contract.task_id} created",
                      actor=Actor.CONTROL.value, source="service", result_label=VERIFIED,
                      evidence=[f"contract={contract.hash()}"], correlation_id=goal_id)
        return {"goal_id": goal_id, "task_ids": [contract.task_id]}

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

    def answer_decision(self, decision_id: str, answer: str, source: str) -> dict:
        row = self.store.answer_decision(decision_id, answer)
        if not row:
            raise KeyError(decision_id)
        self.store.append_event(Event(type=ET.OWNER_DECISION_RECORDED.value, task_id=row.get("task_id"),
                                      payload={"decision_id": decision_id, "answer": answer},
                                      provenance=Provenance(actor=Actor.HUMAN.value, source=source, evidence=[decision_id])))
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
            projects.append({"name": name, "integration_branch": pkg.integration_branch,
                             "stage": (active[0]["stage"] if active else (newest["stage"] if newest else "IDLE")),
                             "open_tasks": len(active), "last_activity": newest["last_activity"] if newest else "",
                             "last_activity_at": newest["last_activity_at"] if newest else None})
        workers = [{"worker_id": r.worker_id, "task_id": tid, "started_at": r.started_at, "last_event_at": r.last_event_at}
                   for tid, r in self.launcher.running.items()]
        return {
            "ts": time.time(), "uptime_sec": round(time.time() - self.started_at), "paused": self.scheduler.paused,
            "projects": projects, "tasks": tasks[-50:], "workers": workers,
            "inbox": self.store.open_decisions(), "costs": self.store.cost_summary(),
            "blocked": [t for t in tasks if t["status"] == "BLOCKED"][-20:],
            "next": next((t for t in tasks if t["status"] == "READY"), None),
            "slots": self.scheduler.slots(), "last_seq": self.store.last_seq(),
        }

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

    # ---- loop -------------------------------------------------------------
    async def run_forever(self) -> None:
        while True:
            try:
                await self.scheduler.tick()
            except Exception as exc:  # the loop must survive; record and go on
                self.store.append_event(Event(type=ET.CONTROL.value, task_id=None, payload={"action": "tick_error", "error": repr(exc)[:300]},
                                              provenance=Provenance(actor=Actor.CONTROL.value, source="scheduler", evidence=[repr(exc)[:100]])))
            await asyncio.sleep(self.cfg.scheduler.tick_sec)
