"""End-to-end control-plane pipeline with a fake executor and a local git remote.

No Claude, no network: a shell-script "worker" emits stream-json lines,
edits a file and commits. The control plane creates the workspace, runs it
in the sandbox (bwrap/systemd when present), verifies in a clean checkout,
pushes to a bare local remote, and records receipts.
"""
import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop_agent.control.adapters.base import LaunchSpec
from desktop_agent.control.adapters.claude_headless import ClaudeHeadlessAdapter
from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig, ScopeLimits
from desktop_agent.control.events import EventType as ET, Stage
from desktop_agent.control.service import Service


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def world(tmp_path: Path):
    """A project repo with a bare 'remote', a worker script, config + project yaml."""
    remote = tmp_path / "remote.git"; sh("git", "init", "--bare", "-q", str(remote))
    src = tmp_path / "src"; src.mkdir()
    sh("git", "init", "-q", "-b", "main", str(src))
    sh("git", "-C", str(src), "config", "user.email", "t@t"); sh("git", "-C", str(src), "config", "user.name", "t")
    (src / "START.md").write_text("start\n"); (src / "AGENTS.md").write_text("rules\n")
    (src / "README.md").write_text("hello\n"); (src / "core.py").write_text("x=1\n")
    (src / "test.sh").write_text("#!/bin/sh\ngrep -q WORKED README.md && echo '1 passed' && exit 0; echo '1 failed'; exit 1\n")
    os.chmod(src / "test.sh", 0o755)
    sh("git", "-C", str(src), "add", "-A"); sh("git", "-C", str(src), "commit", "-q", "-m", "init")
    sh("git", "-C", str(src), "remote", "add", "origin", str(remote)); sh("git", "-C", str(src), "push", "-q", "origin", "main")

    worker = tmp_path / "fake_worker.sh"
    worker.write_text(r'''#!/bin/sh
# $1 = mode: good | bad | blocked
echo '{"type":"system","subtype":"init","session_id":"fake","tools":["Write"]}'
echo '{"type":"assistant","message":{"model":"fake-model","content":[{"type":"tool_use","id":"a","name":"Read","input":{"file_path":"AGENTS.md"}}]}}'
git config user.email w@w; git config user.name worker
if [ "$1" = "blocked" ]; then
  printf '{"type":"result","subtype":"success","result":"STATUS: BLOCKED\\nCOMMIT: none\\nTESTS: not run\\nFILES: none\\nBLOCKER: needs owner\\nNOTE: x","total_cost_usd":0.01,"usage":{"output_tokens":5}}\n'
  exit 0
fi
echo '{"type":"assistant","message":{"model":"fake-model","content":[{"type":"tool_use","id":"b","name":"Edit","input":{"file_path":"README.md"}}]}}'
if [ "$1" = "good" ]; then echo "hello WORKED" > README.md; else echo "hello broken" > README.md; fi
if [ "$1" = "outside" ]; then echo "y=2" >> core.py; fi
git add -A && git commit -q -m "worker change"
SHA=$(git rev-parse HEAD)
printf '{"type":"result","subtype":"success","result":"STATUS: DONE\\nCOMMIT: %s\\nTESTS: 1 passed\\nFILES: README.md\\nBLOCKER: none\\nNOTE: ok","total_cost_usd":0.05,"usage":{"input_tokens":10,"output_tokens":5}}\n' "$SHA"
''')
    os.chmod(worker, 0o755)

    proj = tmp_path / "proj.yaml"
    proj.write_text(f"name: demo\nrepo_path: {src}\nremote_url: {remote}\nintegration_branch: main\n"
                    "start_here: START.md\nagents_file: AGENTS.md\ntest_command: ./test.sh\n"
                    "shared_contract_paths: [core.py]\nworker_allowed_tools: [Read]\n")
    cfg = RuntimeConfig(data_dir=tmp_path / "data", workspaces_dir=tmp_path / "work", token_file=tmp_path / "token",
                        project_files=[proj], open_draft_pr=False, scope=ScopeLimits(runtime_max_sec=120))
    cfg.scheduler.tick_sec = 0.05
    cfg.scheduler.host_reserve_mb = 0
    cfg.use_bwrap = False          # the fake worker needs git config writes in HOME-less mode; bwrap is covered in test_sandbox
    cfg.use_systemd_scope = False
    return {"cfg": cfg, "worker": worker, "src": src, "remote": remote}


class FakeAdapter(ClaudeHeadlessAdapter):
    name = "claude_headless"
    def __init__(self, script: Path, mode: str):
        super().__init__(); self.script, self.mode = script, mode
    def launch(self, **kw):
        return LaunchSpec(argv=[str(self.script), self.mode])


async def _run_until(service: Service, task_id: str, timeout=60):
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        await service.scheduler.tick()
        row = service.store.get_task(task_id)
        if row["status"] in ("DONE", "BLOCKED", "FAILED") and task_id not in service.scheduler._tasks:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("task did not finish")


def _service(world, mode) -> Service:
    s = Service(world["cfg"])
    s.adapters["claude_headless"] = FakeAdapter(world["worker"], mode)
    s.launcher.adapters = s.adapters
    return s


def test_good_worker_end_to_end(world):
    s = _service(world, "good")
    res = s.submit_goal(project="demo", text="Mark README as worked", source="test", task_type="docs", owned_area=["README.md"])
    tid = res["task_ids"][0]
    row = asyncio.run(_run_until(s, tid))
    assert row["status"] == "DONE", row
    types = [e.type for e in s.store.events(tid)]
    for t in (ET.TASK_CREATED, ET.TASK_CLAIMED, ET.WORKER_STARTED, ET.FILE_READ, ET.FILE_CHANGED, ET.CLAIM_WRITTEN,
              ET.WORKER_FINISHED, ET.VERIFY_STARTED, ET.VERIFY_PASSED, ET.INTEGRATION_STARTED, ET.PUSHED, ET.TASK_DONE):
        assert t.value in types, t
    labels = [(r["actor"], r["result_label"]) for r in s.store.receipts("task", tid)]
    assert ("worker", "unverified") in labels and ("control", "verified") in labels
    # the branch really is on the remote at the worker's head
    remote_sha = sh("git", "ls-remote", str(world["remote"]), f"refs/heads/agent/{tid}").split()[0]
    assert remote_sha == row["result"]["integration"]["remote_sha"]
    # the source repo was never touched
    assert sh("git", "-C", str(world["src"]), "status", "--porcelain") == ""
    assert s.store.cost_summary()["today_usd"] == 0.05
    view = asyncio.run(s.task_detail(tid))
    assert view["stage"] == Stage.DONE.value and view["files"][0]["path"] == "README.md" and "WORKED" in view["diff"]


def test_failing_tests_retry_then_block(world):
    s = _service(world, "bad")
    tid = s.submit_goal(project="demo", text="Break it", source="test", task_type="docs", owned_area=["README.md"])["task_ids"][0]

    async def scenario():
        row = await _run_until(s, tid)
        assert row["status"] == "FAILED"
        assert any(e.type == ET.VERIFY_FAILED.value for e in s.store.events(tid))
        retries = [t for t in s.store.list_tasks() if t["contract"].get("supersedes") == tid]
        assert len(retries) == 1 and retries[0]["contract"]["attempt"] == 2 and retries[0]["contract"]["model_class"] == "cloud_cheap"
        row2 = await _run_until(s, retries[0]["task_id"])
        assert row2["status"] == "FAILED"
        third = [t for t in s.store.list_tasks() if t["contract"].get("supersedes") == retries[0]["task_id"]]
        assert third and third[0]["contract"]["model_class"] == "cloud_strong" and third[0]["contract"]["attempt"] == 3
        row3 = await _run_until(s, third[0]["task_id"])
        assert row3["status"] == "BLOCKED" and "attempt 3 failed" in row3["result"]["reason"]
        assert not [t for t in s.store.list_tasks() if t["contract"].get("supersedes") == third[0]["task_id"]]
    asyncio.run(scenario())


def test_shared_contract_violation_blocks_without_retry(world):
    s = _service(world, "outside")
    tid = s.submit_goal(project="demo", text="Touch core", source="test", task_type="docs", owned_area=["README.md"])["task_ids"][0]
    row = asyncio.run(_run_until(s, tid))
    ev = [e for e in s.store.events(tid) if e.type == ET.VERIFY_FAILED.value][0]
    assert any("shared contract" in r or "outside owned_area" in r for r in ev.payload["reasons"])
    assert row["status"] == "BLOCKED" and "bounds violation" in row["result"]["reason"]
    assert not [t for t in s.store.list_tasks() if t["contract"].get("supersedes") == tid], "no retry for a contract fault"


def test_max_attempts_one(world):
    s = _service(world, "bad")
    tid = s.submit_goal(project="demo", text="Break once", source="test", task_type="docs", owned_area=["README.md"], max_attempts=1)["task_ids"][0]
    row = asyncio.run(_run_until(s, tid))
    assert row["status"] == "BLOCKED" and "attempt 1 failed" in row["result"]["reason"]
    assert not [t for t in s.store.list_tasks() if t["contract"].get("supersedes") == tid]


def test_blocked_worker(world):
    s = _service(world, "blocked")
    tid = s.submit_goal(project="demo", text="Need owner", source="test", task_type="docs")["task_ids"][0]
    row = asyncio.run(_run_until(s, tid))
    assert row["status"] == "BLOCKED" and "needs owner" in row["result"]["reason"]


def test_api_state_goals_events(world):
    s = _service(world, "good")        # never the real executor in tests
    s.scheduler.paused = True          # the app's loop must not launch anything here
    app = build_app(s, "tok")
    with TestClient(app) as c:
        assert c.get("/v0/state").status_code == 401
        h = {"Authorization": "Bearer tok", "X-Source": "widget:aria"}
        r = c.post("/v0/goals", json={"project": "demo", "text": "Do a thing", "task_type": "docs"}, headers=h | {"Idempotency-Key": "k1"})
        assert r.status_code == 201
        tid = r.json()["task_ids"][0]
        assert c.post("/v0/goals", json={"project": "demo", "text": "Do a thing"}, headers=h | {"Idempotency-Key": "k1"}).json() == r.json()
        st = c.get("/v0/state", headers=h).json()
        assert st["projects"][0]["name"] == "demo" and st["next"]["task_id"] == tid and "slots" in st
        assert c.get(f"/v0/tasks/{tid}", headers=h).json()["stage"] == "PLANNING"
        assert c.post("/v0/control", json={"action": "resume"}, headers=h).json()["paused"] is False
        assert c.post("/v0/control", json={"action": "pause"}, headers=h).json()["paused"] is True
        assert c.get("/v0/receipts?task_id=" + tid, headers=h).status_code == 200
        assert c.get("/v0/receipts", params={"goal_id": r.json()["goal_id"]}, headers=h).json()["receipts"][0]["result_label"] == "verified"
        # SSE replay of stored events (non-following mode; live follow is exercised by the panel)
        resp = c.get("/v0/events", params={"since": 0, "follow": "false", "access_token": "tok"})
        assert resp.status_code == 200 and "event: GOAL_RECEIVED" in resp.text and "event: TASK_CREATED" in resp.text
        assert c.get("/v0/events", params={"since": 10**9, "follow": "false", "access_token": "tok"}).text == ""
        assert c.get("/").status_code == 200
