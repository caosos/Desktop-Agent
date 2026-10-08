"""Worker launcher: workspace → sandbox → executor → event stream → exit.

Owns the process. Emits WORKER_STARTED/MODEL_SELECTED/…/WORKER_FINISHED
with provenance, writes the worker's claim receipt (unverified), and
records cost. Never pushes, never merges.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import sandbox, workspace as ws
from .adapters.base import Adapter
from .config import RuntimeConfig
from .contracts import TaskContract
from .events import Actor, Event, EventType as ET, Provenance
from .project import ProjectPackage
from .receipts import UNVERIFIED, write_receipt
from .store import Store

PROMPT_TEMPLATE = Path(__file__).resolve().parents[2] / "config" / "worker_prompt.md"


@dataclass
class WorkerRun:
    worker_id: str
    task_id: str
    workspace: ws.Workspace
    proc: asyncio.subprocess.Process | None = None
    started_at: float = 0.0
    last_event_at: float = 0.0
    final: dict | None = None
    exit_code: int | None = None
    killed: bool = False


def render_prompt(contract: TaskContract, project: ProjectPackage, wsp: ws.Workspace, test_port: int) -> str:
    tpl = PROMPT_TEMPLATE.read_text()
    fields = {
        "project": project.name, "task_id": contract.task_id, "attempt": contract.attempt,
        "objective": contract.objective, "why_now": contract.why_now,
        "read_list": ", ".join(contract.read_list), "branch": wsp.branch,
        "base_ref": wsp.base_ref, "base_sha": wsp.base_sha,
        "owned_area": ", ".join(contract.owned_area) or "(whole repo)",
        "shared_contract_paths": ", ".join(project.shared_contract_paths) or "(none)",
        "never_read": ", ".join(project.never_read) or "(none)",
        "forbidden_actions": "; ".join(contract.forbidden_actions),
        "acceptance_tests": "; ".join(contract.acceptance_tests) or "(none declared)",
        "test_port": test_port,
        "expected_artifacts": ", ".join(contract.expected_artifacts) or "(see objective)",
    }
    for k, v in fields.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


class Launcher:
    def __init__(self, cfg: RuntimeConfig, store: Store, adapters: dict[str, Adapter]):
        self.cfg, self.store, self.adapters = cfg, store, adapters
        self.running: dict[str, WorkerRun] = {}

    # ---- events -----------------------------------------------------------
    def _emit(self, run: WorkerRun, etype: str, payload: dict, *, actor: str, source: str,
              contract_hash: str, model: str | None = None, evidence: list | None = None) -> Event:
        ev = Event(type=etype, task_id=run.task_id, worker_id=run.worker_id, payload=payload,
                   provenance=Provenance(actor=actor, source=source, contract_hash=contract_hash,
                                         model=model, evidence=evidence or [f"worker:{run.worker_id}"]))
        run.last_event_at = time.time()
        return self.store.append_event(ev)

    # ---- lifecycle --------------------------------------------------------
    async def run(self, contract: TaskContract, project: ProjectPackage, model: str, test_port: int) -> WorkerRun:
        adapter = self.adapters[contract.worker_adapter]
        worker_id = f"w-{uuid.uuid4().hex[:8]}"
        chash = contract.hash()
        wsp = await ws.create(project, self.cfg.workspaces_dir, contract.task_id, contract.branch_name())
        run = WorkerRun(worker_id=worker_id, task_id=contract.task_id, workspace=wsp, started_at=time.time())
        self.running[contract.task_id] = run
        home = sandbox.prepare_worker_home(self.cfg, self.cfg.workspaces_dir / contract.task_id / "home")
        prompt = render_prompt(contract, project, wsp, test_port)
        (self.cfg.workspaces_dir / contract.task_id / "prompt.md").write_text(prompt)
        spec = adapter.launch(contract=contract, project=project, prompt=prompt, model=model, workspace=wsp.path)
        argv = sandbox.wrap(self.cfg, unit_name=f"desktop-agent-{worker_id}", workspace=wsp.path, worker_home=home,
                            ro_paths=list(self.cfg.sandbox_ro_paths) + list(project.runtime_ro_paths),
                            inner=spec.argv, runtime_max_sec=contract.budget.wall_clock_sec)
        env = sandbox.worker_env(home, test_port, spec.env_extra)
        self._emit(run, ET.WORKER_STARTED.value,
                   {"adapter": adapter.name, "model": model, "branch": wsp.branch, "base_sha": wsp.base_sha,
                    "workspace": str(wsp.path), "sandbox": {"bwrap": "bwrap" in argv, "scope": "systemd-run" in argv},
                    "test_port": test_port},
                   actor=Actor.CONTROL.value, source="launcher", contract_hash=chash, model=model,
                   evidence=[f"argv_len={len(argv)}", f"prompt_sha={_sha(prompt)}"])
        self._emit(run, ET.MODEL_SELECTED.value, {"model_class": contract.model_class, "model": model},
                   actor=Actor.CONTROL.value, source="router", contract_hash=chash, model=model)
        log_path = self.cfg.workspaces_dir / contract.task_id / "worker.stream.jsonl"
        run.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(wsp.path), env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                assert run.proc.stdout is not None
                while True:
                    raw = await run.proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode(errors="replace")
                    log.write(line)
                    parsed = adapter.parse_line(line)
                    for etype, payload in parsed.events:
                        actor = Actor.WORKER.value if etype != ET.MODEL_SELECTED.value else Actor.PROVIDER.value
                        self._emit(run, etype, payload, actor=actor, source=adapter.name,
                                   contract_hash=chash, model=payload.get("model") or model)
                    if parsed.final:
                        run.final = parsed.final
            run.exit_code = await run.proc.wait()
        finally:
            self.running.pop(contract.task_id, None)
        await self._finish(run, contract, project, adapter.name, model, chash)
        return run

    async def _finish(self, run: WorkerRun, contract: TaskContract, project: ProjectPackage,
                      adapter_name: str, model: str, chash: str) -> None:
        final = run.final or {}
        head = None
        try:
            head = await ws.head_sha(run.workspace.path)
        except ws.GitError:
            pass
        commits = []
        if head and head != run.workspace.base_sha:
            commits = await ws.commits_since(run.workspace.path, run.workspace.base_sha)
        if final.get("cost_usd"):
            self.store.add_cost(run.task_id, run.worker_id, model, final["cost_usd"],
                                final.get("input_tokens", 0), final.get("output_tokens", 0))
        payload = {
            "exit_code": run.exit_code, "killed": run.killed, "head_sha": head,
            "commits": commits, "cost_usd": final.get("cost_usd"), "num_turns": final.get("num_turns"),
            "duration_sec": round(time.time() - run.started_at, 1),
            "claim_status": final.get("claim_status"), "subtype": final.get("subtype"),
        }
        self._emit(run, ET.WORKER_KILLED.value if run.killed else ET.WORKER_FINISHED.value, payload,
                   actor=Actor.CONTROL.value, source="launcher", contract_hash=chash, model=model,
                   evidence=[f"exit_code={run.exit_code}", f"head={head}"])
        if final.get("claim"):
            write_receipt(self.store, subject_type="task", subject_id=run.task_id,
                          claim=final["claim"][:4000], actor=Actor.WORKER.value, source=adapter_name,
                          result_label=UNVERIFIED, evidence=[f"worker:{run.worker_id}", f"head={head}",
                                                              f"session={final.get('session_id')}"],
                          correlation_id=contract.goal_id, after_state={"head_sha": head, "commits": len(commits)})

    async def kill(self, task_id: str, reason: str) -> bool:
        run = self.running.get(task_id)
        if not run or not run.proc or run.proc.returncode is not None:
            return False
        run.killed = True
        run.proc.terminate()
        try:
            await asyncio.wait_for(run.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            run.proc.kill()
        self.store.append_event(Event(type=ET.CONTROL.value, task_id=task_id, worker_id=run.worker_id,
                                      payload={"action": "kill", "reason": reason},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source="launcher",
                                                            evidence=[reason])))
        return True

    def stalled(self, stall_after_sec: int) -> list[str]:
        now = time.time()
        return [tid for tid, r in self.running.items() if now - (r.last_event_at or r.started_at) > stall_after_sec]


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:16]
