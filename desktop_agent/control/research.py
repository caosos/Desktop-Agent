"""Ask Aria research jobs v1: one bounded worker per job, local repository sources only, no web.

A job follows exactly one control-plane task (created through Service.submit_goal). State lives in kv
`research:<job_id>` and moves only through `ResearchService.apply` (compare-and-set against TRANSITIONS).
The receipt chain on subject ("research", job_id) is the trace. Design: docs/RESEARCH_BRIEF.md.
"""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from pathlib import Path

from . import receipts as rc
from .research_pdf import render_pdf

TERMINAL = {"VERIFIED", "FAILED", "CANCELLED"}
TRANSITIONS = {
    "REQUESTED": {"PLANNED"},
    "PLANNED": {"RUNNING", "FAILED", "CANCELLED"},
    "RUNNING": {"VERIFIED", "FAILED", "CANCELLED"},
    "VERIFIED": set(), "FAILED": set(), "CANCELLED": set(),
}
INDEX_KV = "research:index"
MODEL_CLASS = "cloud_strong"
BUDGET_USD = 1.5
CAVEAT = "citations resolve; the content is the worker's and unverified"


def report_path(job_id: str) -> str:
    return f"docs/research/{job_id}.md"


def _find(obj, key: str):
    """First value stored under `key` anywhere in a nested result dict (the integrator's result nests)."""
    if isinstance(obj, dict):
        if obj.get(key):
            return obj[key]
        for v in obj.values():
            found = _find(v, key)
            if found:
                return found
    return None


class ResearchService:
    def __init__(self, store, aria, service):
        self.store = store
        self.aria = aria
        self.service = service
        self._lock = threading.RLock()

    # ---- helpers ----------------------------------------------------------
    def _key(self, job_id: str) -> str:
        return f"research:{job_id}"

    def _receipt(self, job: dict, claim: str, label: str, source: str, before=None) -> None:
        rc.write_receipt(
            self.store, subject_type="research", subject_id=job["job_id"], claim=claim, actor="control", source="research",
            result_label=label, evidence=[f"job_id={job['job_id']}", f"task_id={job['task_id']}", f"turn={job['turn']}"],
            before_state=before, after_state={"state": job["state"]}, correlation_id=job["turn"],
            extra={"job_id": job["job_id"], "research_task_id": job["task_id"], "turn": job["turn"], "via": source})

    def _note(self, job: dict, text: str) -> None:
        self.aria.note(text, "research", {"job_id": job["job_id"], "task_id": job["task_id"], "state": job["state"]})

    def _view(self, job: dict, duplicate: bool = False) -> dict:
        out = {k: job.get(k) for k in ("job_id", "turn", "question", "project", "source", "state", "task_id", "report_path", "pr_url",
                                       "reason", "history", "created_at", "updated_at", "published")}
        out["duplicate"] = duplicate
        out["receipts"] = len(rc.chain(self.store, "research", job["job_id"]))
        return out

    def worker_text(self, job: dict) -> str:
        jid, path = job["job_id"], report_path(job["job_id"])
        return (
            f"Research job {jid} for project {job['project']}. QUESTION: {job['question']}\n\n"
            "Local sources only: use this repository checkout on disk (docs, code, tests, PROJECT_STATE). No web retrieval, no guesses.\n"
            "Work in four passes, in order, and name the pass in each commit message:\n"
            "1. researcher: gather evidence from the repository; record each fragment as path:line plus a quote of at most 120 characters.\n"
            "2. analyst: group the fragments into findings; a finding without a source is dropped, not padded.\n"
            f"3. writer: write {path} in exactly this format:\n"
            "   # Research: <question>\n"
            f"   job: {jid}  ·  project: {job['project']}  ·  sources: local repository only (no web retrieval)  ·  <YYYY-MM-DD>\n"
            "   then the sections, in this order: ## Question, ## Findings (numbered, each ending with [n] citations), ## Analysis,\n"
            "   ## Limits (must contain the sentence \"No web retrieval was used.\" and say what could not be answered),\n"
            "   ## Sources (lines like: [1] path/to/file.md:123 — \"quote\"), ## Review (one entry per finding).\n"
            "4. reviewer: check every finding against its cited lines, mark each OK or corrected (what changed) in the Review section, "
            f"then run `.venv/bin/python scripts/research_check.py {path}` and fix until it exits 0.\n"
            "Rules: every claim cites [n]; commit on the task branch; do not touch any other file.")

    # ---- commands ---------------------------------------------------------
    def request(self, turn: str, question: str, project: str, source: str = "test") -> dict:
        question = str(question or "").strip()[:1000]
        if not question:
            raise ValueError("question is required")
        if project not in self.service.projects:
            raise KeyError(f"unknown project {project!r}; known: {list(self.service.projects)}")
        with self._lock:
            seen = self.store.idempotent("research:" + turn)
            if seen:
                return self._view(self.store.get_kv(self._key(seen["job_id"])), duplicate=True)
            job = {"job_id": "rj-" + uuid.uuid4().hex[:10], "turn": turn, "question": question, "project": project, "source": source,
                   "state": "REQUESTED", "task_id": None, "report_path": None, "pr_url": None, "reason": None, "history": [],
                   "rejected": [], "created_at": time.time(), "updated_at": time.time()}
            job["report_path"] = report_path(job["job_id"])
            self.store.set_kv(self._key(job["job_id"]), job)
            self.store.remember("research:" + turn, {"job_id": job["job_id"]})
            self.store.set_kv(INDEX_KV, {"ids": (self.store.get_kv(INDEX_KV) or {}).get("ids", []) + [job["job_id"]]})
            self.apply(job["job_id"], "PLANNED", "research job planned: four passes in one bounded worker, "
                       f"model class {MODEL_CLASS}, budget ceiling ${BUDGET_USD}, local repository sources only", source)
            path = job["report_path"]
            try:
                res = self.service.submit_goal(
                    project=project, text=self.worker_text(job), source=source, task_type="docs", owned_area=[path],
                    expected_artifacts=[path], acceptance_tests=[f".venv/bin/python scripts/research_check.py {path}"],
                    model_class=MODEL_CLASS, budget_usd=BUDGET_USD, max_attempts=2, check_duplicate=False,
                    why_now=f"research job {job['job_id']} confirmed by the owner in Ask Aria")
            except (KeyError, ValueError) as exc:
                self.apply(job["job_id"], "FAILED", f"task could not be created: {exc}"[:300], source)
                return self._view(self.store.get_kv(self._key(job["job_id"])))
            self.apply(job["job_id"], "RUNNING", f"one bounded worker started (task {res['task_ids'][0]}); local sources only", source,
                       task_id=res["task_ids"][0])
            return self._view(self.store.get_kv(self._key(job["job_id"])))

    def apply(self, job_id: str, state: str, note: str = "", source: str = "research", task_id: str | None = None,
              pr_url: str | None = None) -> dict:
        with self._lock:
            job = self.store.get_kv(self._key(job_id))
            if not job:
                return {"applied": False, "reason": "no such job", "state": None}
            if state not in TRANSITIONS.get(job["state"], set()):
                job.setdefault("rejected", []).append({"from": job["state"], "to": state, "at": time.time()})
                self.store.set_kv(self._key(job_id), job)
                return {"applied": False, "reason": f"transition {job['state']} -> {state} not allowed", "state": job["state"]}
            before = {"state": job["state"]}
            job.update(state=state, updated_at=time.time())
            if task_id:
                job["task_id"] = task_id
            if pr_url:
                job["pr_url"] = pr_url
            if state in ("FAILED", "CANCELLED"):
                job["reason"] = note
            job["history"].append({"ts": time.time(), "state": state, "note": note})
            self.store.set_kv(self._key(job_id), job)
            claim = f"{state}: {note}" + (f" ({CAVEAT})" if state == "VERIFIED" else "")
            self._receipt(job, claim, rc.FAILED if state == "FAILED" else rc.VERIFIED, source, before)
        if state == "VERIFIED":
            self._publish(job)
        if state in ("RUNNING", "VERIFIED", "FAILED", "CANCELLED"):
            self._note(job, self._note_text(job, state, note))
        return {"applied": True, "reason": None, "state": state}

    # ---- result publication (convenience after VERIFIED; never changes the job's state) ----
    def published_dir(self) -> Path:
        return Path(self.service.cfg.data_dir) / "research"

    def _publish(self, job: dict) -> None:
        """Copy the verified report out of the task workspace and render it to PDF with the installed Chrome.
        Outcome is recorded on the job (`published`, `pdf`) and in one receipt; a failure here is reported, not hidden."""
        src = Path(self.service.cfg.workspaces_dir) / str(job["task_id"]) / "repo" / job["report_path"]
        out = self.published_dir()
        pub: dict = {"source": str(src)}
        if src.is_file():
            out.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out / f"{job['job_id']}.md")
            pub["report"] = str(out / f"{job['job_id']}.md")
            pub["pdf"] = render_pdf(src, out / f"{job['job_id']}.pdf", footer=f"Desktop-Agent research job {job['job_id']} - {CAVEAT}")
        else:
            pub["pdf"] = {"ok": False, "reason": f"report not found in the task workspace: {src}"}
        with self._lock:
            cur = self.store.get_kv(self._key(job["job_id"])) or job
            cur["published"] = pub
            self.store.set_kv(self._key(job["job_id"]), cur)
            job.update(published=pub)
        ok = pub["pdf"].get("ok")
        self._receipt(job, f"report published: {'PDF ' + str(pub['pdf'].get('bytes')) + ' bytes, sha256 ' + pub['pdf']['sha256'][:12] if ok else 'no PDF: ' + pub['pdf']['reason']}",
                      rc.VERIFIED if ok else rc.FAILED, "publish")

    def _note_text(self, job: dict, state: str, note: str) -> str:
        jid = job["job_id"]
        if state == "RUNNING":
            return f"Research job {jid} started: one bounded worker (task {job['task_id']}), local sources only, no web. The report will appear at {job['report_path']}."
        if state == "VERIFIED":
            pdf = (job.get("published") or {}).get("pdf") or {}
            where = f" PDF ready: GET /v0/research/{jid}/pdf ({pdf.get('bytes')} bytes)." if pdf.get("ok") else f" No PDF: {pdf.get('reason', 'not rendered')}."
            return (f"Research job {jid} VERIFIED: {job['report_path']} ({CAVEAT})"
                    + (f" - PR {job['pr_url']}" if job.get("pr_url") else "") + "." + where)
        return f"Research job {jid} {state}: {note}"

    async def cancel(self, job_id: str, source: str = "test") -> dict:
        job = self.store.get_kv(self._key(job_id))
        if not job:
            raise KeyError(job_id)
        if job["state"] in TERMINAL:
            return self._view(job) | {"cancelled": False, "reason": f"already {job['state']}"}
        if job["task_id"]:
            await self.service.control("stop", job["task_id"], source)
        res = self.apply(job_id, "CANCELLED", "cancelled by the owner side; the worker was stopped", source)
        return self._view(self.store.get_kv(self._key(job_id))) | {"cancelled": res["applied"]}

    def reconcile(self) -> None:
        """Follow each RUNNING job's task once: DONE -> VERIFIED, FAILED/ARCHIVED -> FAILED. Idempotent."""
        for job_id in (self.store.get_kv(INDEX_KV) or {}).get("ids", []):
            job = self.store.get_kv(self._key(job_id))
            if not job or job["state"] != "RUNNING" or not job["task_id"]:
                continue
            task = self.store.get_task(job["task_id"])
            if not task:
                continue
            result = task.get("result") or {}
            if task["status"] == "DONE":
                self.apply(job_id, "VERIFIED", f"task {job['task_id']} verified; report {job['report_path']}", "reconcile",
                           pr_url=_find(result, "pr_url"))
            elif task["status"] in ("FAILED", "ARCHIVED", "BLOCKED"):
                why = _find(result, "reason") or _find(result, "error") or _find(result, "detail") or f"task {task['status'].lower()}"
                self.apply(job_id, "FAILED", f"task {job['task_id']} {task['status']}: {str(why)[:200]}", "reconcile")

    def get(self, job_id: str) -> dict | None:
        job = self.store.get_kv(self._key(job_id))
        return self._view(job) if job else None

    def list(self) -> list[dict]:
        ids = (self.store.get_kv(INDEX_KV) or {}).get("ids", [])
        return [v for v in (self.get(i) for i in reversed(ids)) if v]
