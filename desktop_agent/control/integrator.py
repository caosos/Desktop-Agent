"""Integrator: push the verified branch with the control plane's credential,
confirm the SHA on the remote, open a draft PR. Never merges (Stage 1)."""
from __future__ import annotations

import asyncio
import shutil

from . import workspace as ws
from .config import RuntimeConfig
from .contracts import TaskContract
from .events import Actor, Event, EventType as ET, Provenance
from .project import ProjectPackage
from .receipts import FAILED, VERIFIED, write_receipt
from .store import Store


class Integrator:
    def __init__(self, cfg: RuntimeConfig, store: Store):
        self.cfg, self.store = cfg, store

    def _emit(self, task_id: str, etype: str, payload: dict, chash: str, evidence: list) -> None:
        self.store.append_event(Event(type=etype, task_id=task_id, payload=payload,
                                      provenance=Provenance(actor=Actor.CONTROL.value, source="integrator",
                                                            contract_hash=chash, evidence=evidence)))

    async def integrate(self, contract: TaskContract, project: ProjectPackage, wsp: ws.Workspace,
                        title: str, body: str) -> dict:
        chash, tid = contract.hash(), contract.task_id
        head = await ws.head_sha(wsp.path)
        self._emit(tid, ET.INTEGRATION_STARTED.value, {"branch": wsp.branch, "head_sha": head}, chash, [f"head={head}"])
        result: dict = {"branch": wsp.branch, "head_sha": head, "remote_sha": None, "pr_url": None, "ok": False}
        try:
            remote_sha = await ws.push(wsp.path, project.remote_url, wsp.branch)
            result["remote_sha"] = remote_sha
            if remote_sha != head:
                raise ws.GitError(f"remote reports {remote_sha[:10]} but local head is {head[:10]}")
            self._emit(tid, ET.PUSHED.value, {"branch": wsp.branch, "remote_sha": remote_sha}, chash,
                       [f"ls-remote={remote_sha}"])
            if self.cfg.open_draft_pr and project.github_repo and shutil.which("gh"):
                url = await self._open_pr(project, wsp.branch, title, body)
                result["pr_url"] = url
                self._emit(tid, ET.PR_OPENED.value, {"url": url, "base": project.integration_branch}, chash, [url])
            result["ok"] = True
            self._emit(tid, ET.INTEGRATION_PASSED.value, result, chash, [f"ls-remote={remote_sha}", result["pr_url"] or "no-pr"])
        except Exception as exc:  # push/PR failure is a truthful failure, not a crash
            result["error"] = str(exc)[:500]
            self._emit(tid, ET.INTEGRATION_FAILED.value, result, chash, [str(exc)[:200]])
        write_receipt(self.store, subject_type="task", subject_id=tid,
                      claim=f"branch {wsp.branch} pushed" + (f", draft PR {result['pr_url']}" if result.get("pr_url") else ""),
                      actor=Actor.CONTROL.value, source="integrator",
                      result_label=VERIFIED if result["ok"] else FAILED,
                      evidence=[f"ls-remote={result.get('remote_sha')}", result.get("pr_url") or result.get("error") or "n/a"],
                      correlation_id=contract.goal_id, after_state=result)
        return result

    async def _open_pr(self, project: ProjectPackage, branch: str, title: str, body: str) -> str:
        argv = ["gh", "pr", "create", "--repo", project.github_repo, "--base", project.integration_branch,
                "--head", branch, "--draft", "--title", title[:240], "--body", body]
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {err.decode(errors='replace').strip()[:300]}")
        return out.decode().strip().splitlines()[-1]
