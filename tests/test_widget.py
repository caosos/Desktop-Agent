"""Widget logic without a display: Aria's direct mode and tool dispatch against a stub client,
and the HTTP client against the real API in-process."""
import threading

import pytest

from widget.aria import Aria
from widget.client import ApiError, Client, RefreshCoalescer


class StubClient:
    url, token = "http://stub", "t"
    def __init__(self):
        self.goals, self.controls, self.answers = [], [], []
        self.paused = False
        self.st = {"projects": [{"name": "demo", "stage": "IDLE"}], "workers": [], "inbox": [], "blocked": [], "next": None,
                   "costs": {"today_usd": 0}, "paused": False, "tasks": []}
    def projects(self): return [{"name": "demo"}]
    def state(self): return self.st
    def submit_goal(self, project, text, task_type="code", owned_area=None, idempotency_key=None, **kw):
        self.goals.append((project, text, task_type)); return {"goal_id": "g1", "task_ids": ["t1"]}
    def control(self, action, task_id=None): self.controls.append(action); return {"paused": action == "pause"}
    def decide(self, did, answer): self.answers.append((did, answer)); return {}
    def task(self, tid): return {"task_id": tid, "status": "RUNNING", "stage": "BUILDING", "objective": "o", "receipts": [], "events": []}


def test_refresh_coalescer_keeps_one_trailing_refresh_per_burst():
    refreshes = RefreshCoalescer(interval=1.0)
    assert refreshes.request(10.0) == 0
    assert refreshes.request(10.2) is None
    refreshes.dispatched()
    assert refreshes.request(10.2) == pytest.approx(0.8)
    assert refreshes.request(10.9) is None
    refreshes.dispatched()
    assert refreshes.request(11.0) == pytest.approx(1.0)


def test_direct_mode_confirms_then_submits():
    c = StubClient(); a = Aria(c, api_key="")
    assert not a.conversational
    r = a.ask("Fix the README typo")
    assert r.startswith("Submit to demo:") and c.goals == []
    assert a.ask("yes") == "Submitted. Task t1." and c.goals == [("demo", "Fix the README typo", "code")]
    a.ask("in demo: another thing"); assert a.ask("no") == "Cancelled." and len(c.goals) == 1


def test_direct_mode_control_and_status():
    c = StubClient(); a = Aria(c, api_key="")
    assert a.ask("pause") == "Paused." and c.controls == ["pause"]
    assert "Nothing running" in a.ask("status")
    c.st["tasks"] = [{"task_id": "t1", "status": "RUNNING", "stage": "TESTING", "objective": "o", "last_activity": "tests: gate"}]
    c.st["inbox"] = [{"decision_id": "d1", "question": "merge?", "options": ["yes", "no"]}]
    s = a.ask("how is it going")
    assert "t1 is TESTING" in s and "Needs you: merge?" in s


def test_tool_dispatch():
    c = StubClient(); a = Aria(c, api_key="")
    assert a.run_tool("answer_decision", {"decision_id": "d1", "answer": "yes"}) == {} and c.answers == [("d1", "yes")]
    assert a.run_tool("explain_task", {"task_id": "t1"})["stage"] == "BUILDING"
    assert "error" in a.run_tool("nope", {})


def test_http_client_against_live_api(tmp_path):
    """Spin the real API up on a local port and drive it with the widget client."""
    import uvicorn
    from desktop_agent.control.api import build_app
    from desktop_agent.control.config import RuntimeConfig
    from desktop_agent.control.service import Service
    proj = tmp_path / "p.yaml"
    proj.write_text(f"name: demo\nrepo_path: {tmp_path}\nremote_url: x\nintegration_branch: main\nstart_here: S\nagents_file: A\n")
    cfg = RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t", project_files=[proj])
    svc = Service(cfg); svc.scheduler.paused = True
    server = uvicorn.Server(uvicorn.Config(build_app(svc, "tok"), host="127.0.0.1", port=8498, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    import time
    for _ in range(50):
        try:
            Client("http://127.0.0.1:8498", "tok").health(); break
        except ApiError:
            time.sleep(0.1)
    c = Client("http://127.0.0.1:8498", "tok")
    assert c.projects()[0]["name"] == "demo"
    r = c.submit_goal("demo", "Do a docs thing", "docs", idempotency_key="k")
    assert r["task_ids"] and c.task(r["task_ids"][0])["stage"] == "PLANNING"
    assert c.state()["next"]["task_id"] == r["task_ids"][0]
    with pytest.raises(ApiError):
        Client("http://127.0.0.1:8498", "wrong").state()
    got = []
    stop = threading.Event()
    t = threading.Thread(target=lambda: c.follow_events(got.append, 0, stop), daemon=True); t.start()
    for _ in range(50):
        if any(e["type"] == "TASK_CREATED" for e in got): break
        time.sleep(0.1)
    stop.set()
    assert any(e["type"] == "TASK_CREATED" for e in got)
    server.should_exit = True; th.join(timeout=5)
