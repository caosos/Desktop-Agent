"""Scheduler: admission (IMPLEMENTATION_PLAN §4), task lifecycle, stall,
retry and escalation. Plain code; no model is consulted here."""
from __future__ import annotations

import asyncio
import fnmatch
import os
import socket
import time
from typing import Callable

from . import metrics, router
from .config import RuntimeConfig
from .contracts import TaskContract
from .events import Actor, Event, EventType as ET, Provenance
from .integrator import Integrator
from .launcher import Launcher
from .project import ProjectPackage
from .store import Store
from .verifier import Verifier

MAX_ATTEMPTS = 3


def mem_available_mb() -> int:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def admission(cfg: RuntimeConfig, running: int, spend_last_hour: float, spend_today: float,
              ceiling: int | None = None) -> dict:
    s = cfg.scheduler
    ceiling_eff = s.ceiling if ceiling is None else min(ceiling, s.ceiling)
    cores = os.cpu_count() or 2
    cpu_slots = max(0, (cores - s.reserved_cores) // max(1, s.cores_per_worker))
    mem_slots = max(0, (mem_available_mb() + running * s.mem_per_worker_mb - s.host_reserve_mb) // max(1, s.mem_per_worker_mb))
    budget_slots = max(0, int((s.hourly_cap_usd - spend_last_hour) // max(0.01, s.expected_cost_per_worker_hour_usd)))
    daily_ok = spend_today < s.daily_cap_usd
    slots = min(cpu_slots, mem_slots, budget_slots, ceiling_eff) if daily_ok else 0
    return {"cpu_slots": cpu_slots, "mem_slots": int(mem_slots), "budget_slots": budget_slots,
            "ceiling": ceiling_eff, "owner_ceiling": s.ceiling, "daily_cap_ok": daily_ok, "slots": int(slots),
            "running": running, "free": max(0, int(slots) - running)}


def areas_overlap(a: list[str], b: list[str]) -> bool:
    if not a or not b:
        return True          # an unrestricted task conflicts with everything in its project
    for x in a:
        for y in b:
            if x == y or fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x):
                return True
            px, py = x.rstrip("*").rstrip("/"), y.rstrip("*").rstrip("/")
            if px and py and (px.startswith(py + "/") or py.startswith(px + "/")):
                return True
    return False


def free_port(lo: int, hi: int, taken: set[int]) -> int:
    for port in range(lo, hi + 1):
        if port in taken:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free test port in range")


class Scheduler:
    def __init__(self, cfg: RuntimeConfig, store: Store, launcher: Launcher, verifier: Verifier,
                 integrator: Integrator, projects: dict[str, ProjectPackage],
                 retry_factory: Callable[[TaskContract, str | None], TaskContract]):
        self.cfg, self.store, self.launcher = cfg, store, launcher
        self.verifier, self.integrator, self.projects = verifier, integrator, projects
        self.retry_factory = retry_factory
        self.paused = False
        self.hold_reason: str | None = None     # set when the scheduler pauses itself
        self._ports: set[int] = set()
        self._tasks: dict[str, asyncio.Task] = {}

    def _emit(self, task_id: str | None, etype: str, payload: dict, evidence: list, source: str = "scheduler") -> None:
        self.store.append_event(Event(type=etype, task_id=task_id, payload=payload,
                                      provenance=Provenance(actor=Actor.CONTROL.value, source=source, evidence=evidence)))

    def slots(self) -> dict:
        c = self.store.cost_summary()
        fb = metrics.feedback(self.store)
        ceiling, why = metrics.effective_ceiling(self.cfg.scheduler.ceiling, fb)
        out = admission(self.cfg, len(self.launcher.running), c["last_hour_usd"], c["today_usd"], ceiling)
        out["ceiling_reason"] = why
        return out

    def eligible(self) -> list[dict]:
        done = {t["task_id"] for t in self.store.list_tasks(status="DONE")}
        blocked = {t["task_id"] for t in self.store.list_tasks(status="BLOCKED")}
        running = self.store.list_tasks(status="RUNNING")
        out = []
        for t in self.store.list_tasks(status="READY"):
            c = t["contract"]
            dead = [d for d in c.get("dependencies", []) if d in blocked]
            if dead:
                reason = f"dependency {dead[0]} is BLOCKED"
                self.store.set_task_status(t["task_id"], "BLOCKED", {"reason": reason})
                self._emit(t["task_id"], ET.BLOCKED.value, {"reason": reason, "dependency": dead[0]}, [reason])
                continue
            if any(d not in done for d in c.get("dependencies", [])):
                continue
            if any(r["project"] == t["project"] and areas_overlap(c.get("owned_area", []), r["contract"].get("owned_area", []))
                   for r in running):
                continue
            out.append(t)
        out.sort(key=lambda t: (t["created_at"], len(t["contract"].get("owned_area", []) or [0])))
        return out

    async def tick(self) -> dict:
        for tid in self.launcher.stalled(self.cfg.scheduler.stall_after_sec):
            self._emit(tid, ET.WORKER_STALLED.value, {"stall_after_sec": self.cfg.scheduler.stall_after_sec},
                       [f"no events for {self.cfg.scheduler.stall_after_sec}s"])
            await self.launcher.kill(tid, "stalled")
        if self.paused:
            return {"paused": True}
        slots = self.slots()
        launched = []
        for t in self.eligible()[: slots["free"]]:
            if t["task_id"] in self._tasks:
                continue
            self._tasks[t["task_id"]] = asyncio.create_task(self.run_task(t["task_id"]))
            launched.append(t["task_id"])
        return {"slots": slots, "launched": launched}

    async def run_task(self, task_id: str) -> None:
        row = self.store.get_task(task_id)
        if not row:
            return
        contract = TaskContract.from_dict(row["contract"])
        project = self.projects[contract.project]
        model = router.resolve(self.cfg, contract.model_class, contract.worker_adapter)
        port = free_port(*self.cfg.test_port_range, taken=self._ports)
        self._ports.add(port)
        self.store.set_task_status(task_id, "RUNNING")
        self._emit(task_id, ET.TASK_CLAIMED.value, {"model": model, "test_port": port, "attempt": contract.attempt},
                   [f"contract={contract.hash()}"])
        try:
            run = await self.launcher.run(contract, project, model, port)
            final = run.final or {}
            if run.killed:
                await self._fail(contract, "worker killed", retryable=True)
            elif final.get("provider_limited"):
                # Not the worker's fault and retrying is pointless: pause the platform and say why.
                msg = (final.get("claim") or "provider limit")[:200]
                self.paused = True
                self.hold_reason = f"paused: provider limit ({msg})"
                self._emit(task_id, ET.BUDGET_WARNING.value, {"provider_limit": msg}, [msg])
                self.store.set_task_status(task_id, "READY", {"reason": "requeued after provider limit: " + msg})
                self._emit(task_id, ET.TASK_CREATED.value, {"requeued": True, "reason": msg, "model_class": contract.model_class}, [msg])
            elif final.get("claim_status") == "BLOCKED":
                await self._block(contract, "worker reported BLOCKED: " + (final.get("claim") or "")[:500])
            elif run.exit_code != 0 or not final:
                await self._fail(contract, f"worker exit {run.exit_code}, subtype={final.get('subtype')}", retryable=True)
            else:
                result = await self.verifier.verify(contract, project, run.workspace, port)
                if not result["ok"] and result.get("bounds_violation"):
                    # The contract, not the worker, is at fault: this needs an owner, not a retry.
                    await self._block(contract, "bounds violation: " + "; ".join(result["reasons"]), result=_slim(result))
                elif not result["ok"]:
                    await self._fail(contract, "; ".join(result["reasons"]), retryable=True, result=result)
                else:
                    await self.integrator.record_state_entry(contract, project, run.workspace, result)
                    integ = await self.integrator.integrate(
                        contract, project, run.workspace,
                        title=f"[{contract.task_id}] {contract.objective[:80]}",
                        body=_pr_body(contract, run, result))
                    if integ["ok"]:
                        self.store.set_task_status(task_id, "DONE", {"verify": _slim(result), "integration": integ})
                        self._emit(task_id, ET.TASK_DONE.value, {"head_sha": result["head_sha"], "pr_url": integ.get("pr_url")},
                                   [f"ls-remote={integ.get('remote_sha')}"])
                        self.archive_superseded(contract)
                    else:
                        await self._block(contract, "integration failed: " + str(integ.get("error")), result=integ)
        except Exception as exc:  # keep the loop alive; the failure is recorded, not hidden
            await self._fail(contract, f"control-plane error: {exc!r}"[:500], retryable=False)
        finally:
            self._ports.discard(port)
            self._tasks.pop(task_id, None)

    def archive_superseded(self, done: TaskContract) -> list[str]:
        """Earlier BLOCKED/FAILED attempts at the same objective in the same project are history once a
        later attempt is DONE; archive them so the blocked list shows only what still needs attention."""
        key = done.objective.strip()[:80].lower()
        archived = []
        for t in self.store.list_tasks(status="BLOCKED") + self.store.list_tasks(status="FAILED"):
            c = t["contract"]
            if t["project"] == done.project and t["task_id"] != done.task_id and (c.get("objective") or "").strip()[:80].lower() == key:
                self.store.set_task_status(t["task_id"], "ARCHIVED", {"reason": f"superseded by {done.task_id} (DONE)"})
                self._emit(t["task_id"], ET.CONTROL.value, {"action": "archived", "superseded_by": done.task_id}, [done.task_id])
                archived.append(t["task_id"])
        return archived

    async def _block(self, contract: TaskContract, reason: str, result: dict | None = None) -> None:
        self.store.set_task_status(contract.task_id, "BLOCKED", {"reason": reason} | ({"detail": result} if result else {}))
        self._emit(contract.task_id, ET.BLOCKED.value, {"reason": reason}, [reason[:200]])
        # Dependents cannot proceed; say so instead of leaving them READY forever.
        for t in self.store.list_tasks(status="READY"):
            if contract.task_id in (t["contract"].get("dependencies") or []):
                dep_reason = f"dependency {contract.task_id} is BLOCKED: {reason[:120]}"
                self.store.set_task_status(t["task_id"], "BLOCKED", {"reason": dep_reason})
                self._emit(t["task_id"], ET.BLOCKED.value, {"reason": dep_reason, "dependency": contract.task_id}, [dep_reason[:200]])

    async def _fail(self, contract: TaskContract, reason: str, retryable: bool, result: dict | None = None) -> None:
        self.store.set_task_status(contract.task_id, "FAILED", {"reason": reason} | ({"detail": _slim(result)} if result else {}))
        detail = _slim(result) if result else None
        if not retryable or contract.attempt >= min(MAX_ATTEMPTS, contract.max_attempts):
            await self._block(contract, f"attempt {contract.attempt} failed: {reason}" + ("" if retryable else " (not retryable)"), result=detail)
            return
        next_class = contract.model_class if contract.attempt == 1 else (router.escalate(contract.model_class) or contract.model_class)
        if contract.attempt >= 2 and next_class == contract.model_class:
            await self._block(contract, f"attempt {contract.attempt} failed and no higher model class: {reason}", result=detail)
            return
        new = self.retry_factory(contract, next_class)
        self._emit(new.task_id, ET.TASK_CREATED.value,
                   {"retry_of": contract.task_id, "attempt": new.attempt, "model_class": new.model_class, "reason": reason},
                   [f"supersedes={contract.task_id}"])


def _slim(result: dict | None) -> dict | None:
    if not result:
        return result
    r = dict(result)
    r["tests"] = [{"command": t["command"], "exit_code": t["exit_code"], "tail": t["tail"][-600:]} for t in r.get("tests", [])]
    return r


def _pr_body(contract: TaskContract, run, result: dict) -> str:
    tests = "\n".join(f"- `{t['command']}` → exit {t['exit_code']}" for t in result.get("tests", [])) or "- none declared"
    files = "\n".join(f"- {f['path']} (+{f['added']}/-{f['deleted']})" for f in result.get("files", []))
    return (f"Bounded worker task `{contract.task_id}` (attempt {contract.attempt}) launched by Desktop-Agent.\n\n"
            f"**Objective:** {contract.objective}\n\n**Why now:** {contract.why_now}\n\n"
            f"**Base:** `{run.workspace.base_sha}` on `{run.workspace.base_ref}`\n\n"
            f"**Verifier (control plane, clean checkout):**\n{tests}\n\n**Files:**\n{files}\n\n"
            f"Worker claim is recorded as an unverified receipt; verification above is the control plane's own run.\n\n"
            f"🤖 Generated with [Claude Code](https://claude.com/claude-code)")
