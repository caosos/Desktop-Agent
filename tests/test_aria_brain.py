"""Server-side Aria with a fake model: status answers, confirm-then-submit, action receipts."""
import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.llm import Completion
from desktop_agent.control.service import Service
from widget.aria import Aria


class FakeLLM:
    def __init__(self, answers): self.answers = list(answers); self.prompts = []
    def model_for(self, mc): return "fake"
    async def complete(self, prompt, schema, model_class="cloud_cheap", system=None, timeout=0):
        self.prompts.append(prompt)
        return Completion(data=self.answers.pop(0), model="fake", backend="fake", cost_usd=0.002, input_tokens=5, output_tokens=5, duration_ms=1)


def _service(tmp_path: Path) -> Service:
    repo = tmp_path / "alpha"; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
    y = tmp_path / "a.yaml"; y.write_text(f"name: alpha\nrepo_path: {repo}\nremote_url: x\nintegration_branch: main\nstart_here: S\nagents_file: A\n")
    s = Service(RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t", project_files=[y]))
    s.scheduler.paused = True
    return s


def test_status_question_uses_get_state(tmp_path):
    s = _service(tmp_path)
    s.aria.llm = FakeLLM([{"reply": "Checking.", "action": {"name": "get_state", "args": {}}},
                          {"reply": "Nothing is running and the queue is empty.", "action": None}])
    r = asyncio.run(s.aria.chat("how is it going?"))
    assert r["reply"].startswith("Nothing is running") and r["actions"][0]["name"] == "get_state" and r["actions"][0]["ok"]
    assert r["cost_usd"] == 0.004 and "get_state →" in s.aria.llm.prompts[1]
    assert s.store.cost_summary()["today_usd"] == 0.004


def test_confirm_then_submit_goal_plans(tmp_path):
    s = _service(tmp_path)
    s.planner.llm = FakeLLM([{"project": "alpha", "summary": "one task", "questions": [], "tasks": [
        {"objective": "Fix the README typo in alpha", "task_type": "docs", "owned_area": ["README.md"], "model_class": "cloud_cheap", "why_now": "asked"}]}])
    s.aria.llm = FakeLLM([{"reply": "Submit to alpha: fix the README typo? (yes/no)", "action": None},
                          {"reply": "Submitting.", "action": {"name": "submit_goal", "args": {"text": "Fix the README typo in alpha", "project": "alpha"}}},
                          {"reply": "Submitted as one task.", "action": None}])
    assert "yes/no" in asyncio.run(s.aria.chat("fix the readme typo in alpha"))["reply"]
    r = asyncio.run(s.aria.chat("yes"))
    assert r["reply"] == "Submitted as one task." and r["actions"][0]["name"] == "submit_goal"
    assert len(s.store.list_tasks(status="READY")) == 1
    assert s.store.events()[0].provenance.source == "aria"


def test_widget_uses_server_side_aria_and_api_route(tmp_path):
    s = _service(tmp_path)
    s.aria.llm = FakeLLM([{"reply": "Paused.", "action": {"name": "control", "args": {"action": "pause"}}},
                          {"reply": "The platform is paused.", "action": None}])
    with TestClient(build_app(s, "tok")) as c:
        r = c.post("/v0/aria/chat", json={"text": "please pause everything"}, headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200 and r.json()["reply"] == "The platform is paused." and r.json()["actions"][0]["ok"]
        assert c.post("/v0/aria/chat", json={"text": "x"}).status_code == 401

    class ChatClient:
        url, token = "http://stub", "t"
        def chat(self, text, reset=False): return {"reply": "server reply for " + text}
        def projects(self): return [{"name": "alpha"}]
        def control(self, action, task_id=None): return {"paused": True}
    a = Aria(ChatClient(), api_key="")
    assert a.server_side and a.ask("what's running?") == "server reply for what's running?"
    assert a.ask("pause") == "Paused."            # single-word controls skip the model
