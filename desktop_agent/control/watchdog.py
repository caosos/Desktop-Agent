"""Coordinator watchdog: cheap, non-LLM checks per project coordinator, and a bounded
Claude wake only when a probe says useful work needs intelligence.

Per project (config `intake.coordinator`):
  state_command : prints one JSON object describing the coordinator (its own script; stdlib only)
  probe_command : prints {"verdict": "WAKE"|"IDLE", "reasons": [...]}; IDLE costs nothing
  wake          : on WAKE, one peer message `DA-HEARTBEAT reasons=...` to the live session, at most
                  once per cooldown unless the reasons change; recorded with a receipt.
Liaison inboxes (Michael Business OS): `docs/messages/inbox/*.md` on a branch, acks as files on the
coordinator's branch; read through the free GitHub contents API; no session to wake across users,
so items wait for the coordinator's own sync and are shown as such.
Quota: the last Claude Code rate-limit window seen by a worker is kept; when the scheduler paused
itself on a provider limit, it resumes once the window has reset.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time

from . import allowance
from .events import Actor, Event, EventType as ET, Provenance
from .intake import gh_api, IntakeSource  # noqa: F401  gh_api stays this module's symbol: liaison.py calls watchdog.gh_api, tests patch it here
from .receipts import FAILED, VERIFIED, write_receipt
from .store import Store
from .checks import Checks
from .liaison import LiaisonInbox

WAKE_COOLDOWN_SEC = 1800


async def run_json_command(cmd: str, cwd: str | None, timeout: int = 60) -> dict:
    """Run a coordinator's own stdlib script and parse its single JSON object. Never a model."""
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and not k.endswith("_TOKEN")}
    proc = await asyncio.create_subprocess_exec("/bin/sh", "-c", cmd, cwd=cwd, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd!r} exit {proc.returncode}: {err.decode(errors='replace')[:200]}")
    return json.loads(text[text.index("{"):])


class Watchdog:
    def __init__(self, store: Store, intake, deliverer, scheduler, cooldown_sec: int = WAKE_COOLDOWN_SEC):
        self.store, self.intake, self.deliverer, self.scheduler = store, intake, deliverer, scheduler
        self.cooldown_sec = cooldown_sec
        self.workers = None                                      # WorkerMonitor, set by the service (feeds + open-PR refresh)
        self.allowance_path = None                               # <data_dir>/allowance.json, set by the service
        self.allowance_project = "desktop_agent"                 # whose coordinator session is the registered allowance writer
        self._last_wake: dict[str, tuple[float, str]] = {}      # project → (ts, reasons key)
        self.liaison = LiaisonInbox(self)
        self.checks = Checks(store, intake, deliverer)           # ETA-driven checks live in this watchdog (checks.py)
        intake.checks = self.checks

    def _emit(self, project: str, etype: str, payload: dict, evidence: list) -> None:
        self.store.append_event(Event(type=etype, task_id=None, payload={"project": project, **payload},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source="watchdog", evidence=evidence)))

    # ---- one pass over all coordinators ------------------------------------------------
    async def tick(self) -> dict:
        report: dict = {}
        for src in self.intake.sources:
            coord = src.coordinator or {}
            entry: dict = {"checked_at": time.time()}
            try:
                if coord.get("state_command"):
                    entry["state"] = await run_json_command(coord["state_command"], coord.get("cwd"))
                if coord.get("probe_command"):
                    probe = await run_json_command(coord["probe_command"], coord.get("cwd"))
                    entry["probe"] = probe
                    if str(probe.get("verdict", "")).upper() == "WAKE":
                        entry["wake"] = await self._wake(src, probe.get("reasons") or [])
                entry["error"] = None
            except Exception as exc:
                entry["error"] = str(exc)[:300]
            self.store.save_coordinator_check(src.project, entry)
            report[src.project] = {k: v for k, v in entry.items() if k != "state"}
        if self.allowance_path is not None:
            writer_path = self.allowance_path.with_name(allowance.WRITER_FILE)
            own = next((s for s in self.intake.sources if s.project == self.allowance_project), None)
            if own is not None and hasattr(self.deliverer, "resolve_for"):
                sess, note = self.deliverer.resolve_for(own)
                allowance.register_writer(writer_path, sess, note)
            report["allowance"] = allowance.ingest(self.store, self.allowance_path, writer_path=writer_path)
        self._resume_after_quota_reset()
        if self.workers is not None:
            try:
                report["workers"] = await self.workers.refresh_remote()
            except Exception as exc:
                report["workers"] = {"error": str(exc)[:200]}
        if getattr(self, "drill", None) is not None:
            try:
                report["rosters"] = await self.drill.refresh_remote()
            except Exception as exc:
                report["rosters"] = {"error": str(exc)[:200]}
        try:
            report["checks"] = await self.checks.tick()          # due ETA-driven checks: exactly one outcome each
        except Exception as exc:
            report["checks"] = {"error": str(exc)[:200]}
        try:
            from .look_retention import sweep                    # stored screenshots expire after seven days (the capture receipts stay)
            removed = sweep(self.store, self.store.data_dir)
            if removed:
                report["look_retention"] = removed
        except Exception as exc:
            report["look_retention"] = {"error": str(exc)[:200]}
        return report

    async def _wake(self, src: IntakeSource, reasons: list) -> dict:
        key = hashlib.sha1(json.dumps(sorted(map(str, reasons))).encode()).hexdigest()[:8]
        last = self._last_wake.get(src.project)
        if last and time.time() - last[0] < self.cooldown_sec and last[1] == key:
            return {"sent": False, "note": "same reasons within cooldown"}
        text = (f"DA-HEARTBEAT reasons={json.dumps(reasons)[:800]}\n"
                f"Your heartbeat probe reported WAKE at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}. Sent once by the Desktop-Agent watchdog; "
                f"no reply to this message is needed; act through your normal receipts.")
        fake_item = {"item_id": f"hb-{key}", "project": src.project, "repo": src.repo, "issue": src.issues[0] if src.issues else 0,
                     "kind": "heartbeat", "author": "watchdog", "body": text, "url": "", "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        result = await self.deliverer._claude_peer(src, fake_item, backfill=False, prebuilt_text=text)
        self._last_wake[src.project] = (time.time(), key)
        label = VERIFIED if result.get("ok") else FAILED
        write_receipt(self.store, subject_type="coordinator", subject_id=src.project, claim=f"heartbeat wake: {', '.join(map(str, reasons))[:160]}",
                      actor=Actor.CONTROL.value, source="watchdog", result_label=label, evidence=result.get("evidence") or [result.get("note", "")])
        self._emit(src.project, ET.CONTROL.value, {"action": "heartbeat_wake", "ok": result.get("ok"), "reasons": reasons[:6], "note": result.get("note", "")[:200]},
                   result.get("evidence") or [])
        if result.get("ok"):
            self.store.save_coordinator_wake(src.project, time.time(), "heartbeat")
        return {"sent": bool(result.get("ok")), "note": result.get("note", "")[:200]}

    # ---- quota reset → resume ----------------------------------------------------------
    def _resume_after_quota_reset(self) -> None:
        if not self.scheduler.paused or not (self.scheduler.hold_reason or "").startswith("paused: provider limit"):
            return
        q = self.store.get_kv("quota") or {}
        resets = q.get("resets_at")
        if resets and time.time() > float(resets):
            self.scheduler.paused = False
            self.scheduler.hold_reason = None
            self._emit("desktop_agent", ET.CONTROL.value, {"action": "resume_after_quota_reset", "resets_at": resets}, [f"resets_at={resets}"])

    # ---- liaison inbox (file-based): liaison.py -------------------------------------------
    async def poll_liaison(self, src: IntakeSource) -> tuple[int, int]:
        return await self.liaison.poll_liaison(src)

    async def send_liaison(self, src: IntakeSource, item: dict) -> dict:
        return await self.liaison.send_liaison(src, item)
