"""Ask Aria research jobs v1 (docs/RESEARCH_BRIEF.md §8): job + task creation, idempotency, reconcile, cancel,
the report checker and the API. No network, no provider, no model call beyond FakeLLM."""
import asyncio
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from desktop_agent.control.api import build_app
from desktop_agent.control.receipts import chain
from tests.test_ask_aria import FakeLLM, _service

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures" / "research"
Q = "What law governs receipts?"
H = {"Authorization": "Bearer tok"}


def _task(s, job):
    return s.store.get_task(job["task_id"])


def test_request_creates_one_job_and_one_task(tmp_path):
    s = _service(tmp_path)
    job = s.research.request("turn-1", Q, "mbos", "test")
    jid, path = job["job_id"], f"docs/research/{job['job_id']}.md"
    assert job["state"] == "RUNNING" and job["duplicate"] is False and job["report_path"] == path
    assert [h["state"] for h in job["history"]] == ["PLANNED", "RUNNING"]
    tasks = s.store.list_tasks()
    assert len(tasks) == 1 and tasks[0]["task_id"] == job["task_id"] and tasks[0]["status"] == "READY"
    c = tasks[0]["contract"]
    assert c["owned_area"] == [path] and c["expected_artifacts"] == [path] and c["model_class"] == "cloud_strong"
    assert c["acceptance_tests"] == [f"python3 scripts/research_check.py {path}"] and c["budget"]["usd"] == 1.5
    assert jid in c["objective"] and Q in c["objective"] and "No web retrieval" in c["objective"]
    notes = [t for t in s.aria.transcript() if t.get("kind") == "research"]
    assert len(notes) == 1 and jid in notes[0]["text"] and "RUNNING" not in notes[0]["text"] and "started" in notes[0]["text"]


def test_same_turn_is_one_job_other_turn_is_new(tmp_path):
    s = _service(tmp_path)
    a = s.research.request("turn-a", Q, "mbos")
    b = s.research.request("turn-a", Q, "mbos")
    assert b["duplicate"] is True and b["job_id"] == a["job_id"] and b["task_id"] == a["task_id"]
    assert len(s.store.list_tasks()) == 1 and len(chain(s.store, "research", a["job_id"])) == 2
    c = s.research.request("turn-b", Q + " again, differently", "mbos")
    assert c["job_id"] != a["job_id"] and len(s.store.list_tasks()) == 2
    assert [j["job_id"] for j in s.research.list()] == [c["job_id"], a["job_id"]]


def test_reconcile_done_moves_once_and_failed_carries_reason(tmp_path):
    s = _service(tmp_path)
    ok = s.research.request("t-ok", Q, "mbos")
    bad = s.research.request("t-bad", Q + " other", "mbos")
    s.research.reconcile()
    assert s.research.get(ok["job_id"])["state"] == "RUNNING"          # nothing finished: nothing moves
    s.store.set_task_status(ok["task_id"], "DONE", {"integration": {"pr_url": "https://example.test/pr/1"}})
    s.store.set_task_status(bad["task_id"], "FAILED", {"reason": "verifier failed: checker exit 1"})
    s.research.reconcile()
    s.research.reconcile()
    j = s.research.get(ok["job_id"])
    assert j["state"] == "VERIFIED" and j["pr_url"] == "https://example.test/pr/1" and j["receipts"] == 4
    rs = chain(s.store, "research", ok["job_id"])
    # PLANNED, RUNNING, VERIFIED, then the publication step: no report in this test workspace → a failed receipt, state unchanged
    assert [r["result_label"] for r in rs] == ["verified"] * 3 + ["failed"] and "citations resolve" in rs[2]["claim"] and "unverified" in rs[2]["claim"]
    assert rs[2]["actor"] == "control" and rs[2]["correlation_id"] == "t-ok" and f"task_id={ok['task_id']}" in rs[2]["evidence"]
    assert rs[3]["claim"].startswith("report published: no PDF: report not found") and j["published"]["pdf"]["ok"] is False
    f = s.research.get(bad["job_id"])
    assert f["state"] == "FAILED" and "verifier failed" in f["reason"]
    assert [r["result_label"] for r in chain(s.store, "research", bad["job_id"])] == ["verified", "verified", "failed"]
    texts = [t["text"] for t in s.aria.transcript() if t.get("kind") == "research"]
    assert any("VERIFIED" in t and j["report_path"] in t and "citations resolve; the content is the worker's and unverified" in t for t in texts)
    assert any("FAILED" in t and bad["job_id"] in t for t in texts)


def test_cancel_running_stops_task_and_terminal_is_refused(tmp_path):
    s = _service(tmp_path)
    stopped = []

    async def fake_control(action, task_id, source):
        stopped.append((action, task_id))
        return {}
    s.control = fake_control
    job = s.research.request("t-c", Q, "mbos")
    out = asyncio.run(s.research.cancel(job["job_id"], "test"))
    assert out["cancelled"] is True and out["state"] == "CANCELLED" and stopped == [("stop", job["task_id"])]
    again = asyncio.run(s.research.cancel(job["job_id"], "test"))
    assert again["cancelled"] is False and "already CANCELLED" in again["reason"]
    done = s.research.request("t-d", Q + " done", "mbos")
    s.store.set_task_status(done["task_id"], "DONE", {})
    s.research.reconcile()
    refused = asyncio.run(s.research.cancel(done["job_id"], "test"))
    assert refused["cancelled"] is False and refused["state"] == "VERIFIED" and len(stopped) == 1


def test_rejected_transitions_are_recorded_not_receipted(tmp_path):
    s = _service(tmp_path)
    job = s.research.request("t-r", Q, "mbos")
    before = len(chain(s.store, "research", job["job_id"]))
    res = s.research.apply(job["job_id"], "PLANNED", "backwards")
    assert res["applied"] is False and "not allowed" in res["reason"]
    s.research.apply(job["job_id"], "VERIFIED", "ok")
    res = s.research.apply(job["job_id"], "RUNNING", "after terminal")
    assert res["applied"] is False and res["state"] == "VERIFIED"
    assert len(chain(s.store, "research", job["job_id"])) == before + 2   # VERIFIED, then its publication step; the two rejections add nothing
    assert len(s.store.get_kv(f"research:{job['job_id']}")["rejected"]) == 2
    assert s.research.apply("rj-nope", "RUNNING")["reason"] == "no such job"


def test_unknown_project_and_empty_question_are_refused(tmp_path):
    s = _service(tmp_path)
    for args in (("t1", Q, "nope"),):
        try:
            s.research.request(*args)
            assert False
        except KeyError:
            pass
    try:
        s.research.request("t2", "  ", "mbos")
        assert False
    except ValueError:
        pass
    assert s.research.list() == [] and s.store.list_tasks() == []


def _check(name):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "research_check.py"), str(FIX / name), "--repo", str(ROOT)],
                          capture_output=True, text=True)


def test_checker_fixtures():
    ok = _check("pass.md")
    assert ok.returncode == 0 and ok.stdout == "", ok.stdout
    for name, rule in (("uncited.md", "uncited-finding"), ("missing_path.md", "missing-path"), ("no_limits_sentence.md", "limits-sentence")):
        r = _check(name)
        assert r.returncode == 1 and rule in r.stdout, (name, r.stdout)
    assert subprocess.run([sys.executable, str(ROOT / "scripts" / "research_check.py"), str(FIX / "nope.md")], capture_output=True, text=True).returncode == 1


def test_checker_line_beyond_file_and_unresolved_citation(tmp_path):
    rep = tmp_path / "r.md"
    rep.write_text((FIX / "pass.md").read_text().replace("AGENTS.md:2", "AGENTS.md:999999").replace("[2] AGENTS.md", "[3] AGENTS.md"))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "research_check.py"), str(rep), "--repo", str(ROOT)], capture_output=True, text=True)
    assert r.returncode == 1 and "unresolved-citation: [2]" in r.stdout and "missing-line: [3]" in r.stdout


def test_ask_aria_request_research_path(tmp_path):
    s = _service(tmp_path)
    s.aria.llm = FakeLLM([{"reply": "Plan: four passes, one worker, cloud_strong, up to $1.5, local sources. Starting.",
                           "action": {"name": "request_research", "args": {"question": Q, "project": "mbos"}}},
                          {"reply": "Research job started.", "action": {"name": "research_status", "args": {"job_id": "rj-nope"}}},
                          {"reply": "Started; the report will appear here.", "action": None}])
    r = asyncio.run(s.aria.chat("Please investigate how receipts work"))
    assert r["kind"] == "research" and r["actions"][0]["ok"] and r["actions"][0]["evidence"]["job_id"].startswith("rj-")
    assert len(s.research.list()) == 1 and len(s.store.list_tasks()) == 1
    assert s.research.list()[0]["turn"].startswith("turn-")


def test_routes(tmp_path):
    s = _service(tmp_path)
    s.control = lambda *a, **k: _noop()
    with TestClient(build_app(s, "tok")) as c:
        assert c.post("/v0/research", json={"question": Q, "project": "mbos"}).status_code == 401
        assert c.get("/v0/research").status_code == 401
        r = c.post("/v0/research", json={"question": Q, "project": "mbos"}, headers=H)
        assert r.status_code == 201 and r.json()["state"] == "RUNNING"
        jid = r.json()["job_id"]
        assert [j["job_id"] for j in c.get("/v0/research", headers=H).json()["jobs"]] == [jid]
        assert c.get(f"/v0/research/{jid}", headers=H).json()["task_id"] == r.json()["task_id"]
        assert c.post("/v0/research", json={"question": Q, "project": "nope"}, headers=H).status_code == 404
        assert c.post("/v0/research", json={"question": " ", "project": "mbos"}, headers=H).status_code == 400
        out = c.post(f"/v0/research/{jid}/cancel", headers=H).json()
        assert out["cancelled"] is True and out["state"] == "CANCELLED"
        assert c.post(f"/v0/research/{jid}/cancel", headers=H).json()["cancelled"] is False
        assert c.get("/v0/research/rj-nope", headers=H).status_code == 404
        assert c.post("/v0/research/rj-nope/cancel", headers=H).status_code == 404


async def _noop():
    return {}


def test_reconcile_follows_a_scheduler_retry_instead_of_failing_the_job(tmp_path):
    """The scheduler retries a failed attempt as a new task that `supersedes` the old one (seen live: rj-3dba7e19b7
    was marked FAILED while attempt 2 was still running). The job must move to the retry task and stay RUNNING."""
    s = _service(tmp_path)
    job = s.research.request("t-retry", Q, "mbos")
    first = s.store.get_task(job["task_id"])
    s.store.set_task_status(first["task_id"], "FAILED", {"reason": "1 acceptance test command(s) failed"})
    c2 = dict(first["contract"]); c2.update(task_id=first["task_id"] + "-r2", attempt=2, supersedes=first["task_id"])
    s.store.save_task(c2["task_id"], first.get("goal_id"), "mbos", "READY", c2, "h2")
    s.research.reconcile()
    j = s.research.get(job["job_id"])
    assert j["state"] == "RUNNING" and j["task_id"] == first["task_id"] + "-r2" and "supersedes" in j["history"][-1]["note"]
    s.research.reconcile()                                  # idempotent: no second hop, no second receipt
    rs = chain(s.store, "research", job["job_id"])
    assert sum("retry attempt 2" in r["claim"] for r in rs) == 1 and all(r["result_label"] == "verified" for r in rs)
    assert any("retrying as task" in t["text"] for t in s.aria.transcript() if t.get("kind") == "research")
    s.store.set_task_status(first["task_id"] + "-r2", "DONE", {"integration": {"pr_url": "https://example.test/pr/2"}})
    s.research.reconcile()
    assert s.research.get(job["job_id"])["state"] == "VERIFIED"
    # a FAILED attempt without a retry, or a BLOCKED task, still fails the job
    other = s.research.request("t-final", Q + " again", "mbos")
    s.store.set_task_status(other["task_id"], "BLOCKED", {"reason": "attempt 2 failed: checker"})
    s.research.reconcile()
    assert s.research.get(other["job_id"])["state"] == "FAILED"
