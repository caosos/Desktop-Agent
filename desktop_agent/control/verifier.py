"""Verifier: the control plane's own observation of a worker's result.

Clean checkout at the worker's HEAD, owned-area / shared-contract check,
acceptance tests run in the sandbox, VERIFY_* events and a receipt that is
`verified` or `failed` on the control plane's evidence alone.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from . import sandbox, workspace as ws
from .config import RuntimeConfig
from .contracts import TaskContract
from .events import Actor, Event, EventType as ET, Provenance
from .project import ProjectPackage
from .receipts import FAILED, VERIFIED, write_receipt
from .store import Store


class Verifier:
    def __init__(self, cfg: RuntimeConfig, store: Store):
        self.cfg, self.store = cfg, store

    def _emit(self, task_id: str, etype: str, payload: dict, contract_hash: str, evidence: list) -> Event:
        return self.store.append_event(Event(
            type=etype, task_id=task_id, payload=payload,
            provenance=Provenance(actor=Actor.CONTROL.value, source="verifier",
                                  contract_hash=contract_hash, evidence=evidence)))

    async def verify(self, contract: TaskContract, project: ProjectPackage, wsp: ws.Workspace,
                     test_port: int) -> dict:
        chash = contract.hash()
        tid = contract.task_id
        head = await ws.head_sha(wsp.path)
        self._emit(tid, ET.VERIFY_STARTED.value, {"head_sha": head, "base_sha": wsp.base_sha}, chash,
                   [f"head={head}"])
        result: dict = {"head_sha": head, "base_sha": wsp.base_sha, "ok": False, "reasons": []}

        if head == wsp.base_sha:
            result["reasons"].append("no commits on the task branch")
        files = await ws.changed_files(wsp.path, wsp.base_sha)
        result["files"] = files
        bounds = ws.outside_owned_area([f["path"] for f in files], contract.owned_area, project.shared_contract_paths)
        if bounds["outside"]:
            result["reasons"].append(f"files outside owned_area: {bounds['outside'][:10]}")
        if bounds["shared"]:
            result["reasons"].append(f"shared contract paths touched: {bounds['shared']}")
        if await ws.is_dirty(wsp.path):
            result["reasons"].append("uncommitted changes left in the workspace")

        if not result["reasons"] and contract.acceptance_tests:
            verify_dir = self.cfg.workspaces_dir / tid / "verify"
            await ws.clean_checkout(wsp.path, verify_dir, head)
            tests = []
            for cmd in contract.acceptance_tests:
                tests.append(await self._run_test(contract, project, verify_dir, cmd, test_port))
            result["tests"] = tests
            failed = [t for t in tests if t["exit_code"] != 0]
            if failed:
                result["reasons"].append(f"{len(failed)} acceptance test command(s) failed")
        else:
            result.setdefault("tests", [])
            if not contract.acceptance_tests:
                result["note"] = "no acceptance tests declared; verified on bounds and commits only"

        result["ok"] = not result["reasons"]
        etype = ET.VERIFY_PASSED if result["ok"] else ET.VERIFY_FAILED
        evidence = [f"head={head}", f"files={len(files)}"] + [
            f"test:{t['command'][:60]} exit={t['exit_code']}" for t in result.get("tests", [])]
        self._emit(tid, etype.value, {k: v for k, v in result.items() if k != "tests"} |
                   {"tests": [{"command": t["command"], "exit_code": t["exit_code"], "tail": t["tail"][-800:]}
                              for t in result.get("tests", [])]}, chash, evidence)
        write_receipt(self.store, subject_type="task", subject_id=tid,
                      claim=f"verifier: {'passed' if result['ok'] else 'failed'} at {head[:10]}",
                      actor=Actor.CONTROL.value, source="verifier",
                      result_label=VERIFIED if result["ok"] else FAILED, evidence=evidence,
                      correlation_id=contract.goal_id, before_state={"base_sha": wsp.base_sha},
                      after_state={"head_sha": head, "reasons": result["reasons"]})
        return result

    async def _run_test(self, contract: TaskContract, project: ProjectPackage, checkout: Path,
                        command: str, test_port: int) -> dict:
        env_extra = {k: v.replace("{port}", str(test_port)).replace("{task_id}", contract.task_id.replace("-", "_"))
                     for k, v in project.test_env.items()}
        home = self.cfg.workspaces_dir / contract.task_id / "verify-home"
        home.mkdir(parents=True, exist_ok=True)
        inner = ["/bin/bash", "-lc", command]
        argv = sandbox.wrap(self.cfg, unit_name=f"desktop-agent-verify-{contract.task_id[-12:]}-{int(time.time())}",
                            workspace=checkout, worker_home=home,
                            ro_paths=list(self.cfg.sandbox_ro_paths) + list(project.runtime_ro_paths),
                            inner=inner, runtime_max_sec=contract.budget.wall_clock_sec)
        env = sandbox.worker_env(home, test_port, env_extra)
        env["PATH"] = os.environ.get("PATH", env["PATH"])
        started = time.time()
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(checkout), env=env,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=contract.budget.wall_clock_sec)
        except asyncio.TimeoutError:
            proc.kill()
            out = b"[verifier] timed out"
        text = out.decode(errors="replace")
        (self.cfg.workspaces_dir / contract.task_id / "verify.log").write_text(text)
        return {"command": command, "exit_code": proc.returncode if proc.returncode is not None else -1,
                "duration_sec": round(time.time() - started, 1), "tail": text[-4000:]}
