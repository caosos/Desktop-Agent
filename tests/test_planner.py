"""Planner with a fake model: project pick, task validation, owner questions and re-plan after an answer."""
import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.events import EventType as ET
from desktop_agent.control.llm import Completion
from desktop_agent.control.planner import Planner, project_context
from desktop_agent.control.project import ProjectPackage
from desktop_agent.control.service import Service


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers); self.calls = []
    def model_for(self, mc): return "fake-" + mc
    async def complete(self, prompt, schema, model_class="cloud_cheap", system=None, timeout=0):
        self.calls.append((prompt, model_class))
        return Completion(data=self.answers.pop(0), model="fake-" + model_class, backend="fake", cost_usd=0.001,
                          input_tokens=10, output_tokens=5, duration_ms=1)


def _projects(tmp_path: Path) -> dict:
    out = {}
    for name in ("alpha", "beta"):
        repo = tmp_path / name; repo.mkdir()
        (repo / "START.md").write_text(f"start of {name}\n" * 5); (repo / "AGENTS.md").write_text("rules\n")
        (repo / "Q.md").write_text("queue\n")
        y = tmp_path / f"{name}.yaml"
        y.write_text(f"name: {name}\nrepo_path: {repo}\nremote_url: x\nintegration_branch: main\nstart_here: START.md\n"
                     f"agents_file: AGENTS.md\nready_queue: Q.md\ntest_command: ./t.sh\nshared_contract_paths: [core.py]\n")
        out[name] = ProjectPackage.load(y)
    return out


GOOD_PLAN = {"project": "alpha", "summary": "two small tasks", "questions": [], "tasks": [
    {"objective": "Add docs", "task_type": "docs", "owned_area": ["docs/*"], "model_class": "cloud_cheap", "why_now": "asked",
     "acceptance": "file exists"},
    {"objective": "Add tests", "task_type": "tests_only", "owned_area": ["tests/*"], "model_class": "cloud_cheap", "why_now": "asked",
     "dependencies": [0]},
    {"objective": "Edit core", "task_type": "code", "owned_area": ["core.py"], "model_class": "cloud_strong", "why_now": "x"},
]}


def test_plan_picks_project_and_validates(tmp_path):
    projects = _projects(tmp_path)
    llm = FakeLLM([{"project": "alpha", "reason": "mentions alpha"}, GOOD_PLAN])
    plan = asyncio.run(Planner(llm, projects).plan("Please improve alpha's docs and tests"))
    assert plan.project == "alpha" and len(plan.tasks) == 2        # the core.py task is dropped (shared contract)
    assert plan.problems and "shared contract" in plan.problems[0]
    assert plan.tasks[1]["dependencies"] == [0]
    assert len(llm.calls) == 2 and "PROJECT alpha" in llm.calls[1][0] and "start of alpha" in llm.calls[1][0]
    ctx = project_context(projects["alpha"]); assert "core.py" in ctx and "./t.sh" in ctx


def test_owned_area_validation_against_tracked_files():
    from desktop_agent.control.planner import owned_area_matches
    files = ["desktop_agent/control/config.py", "desktop_agent/control/api.py", "panel/index.html", "tests/test_core.py", "README.md"]
    kept = owned_area_matches(["desktop_agent/control/runtime*.py", "desktop_agent/control/config.py", "tests/test_runtime*.py",
                               "panel/*", "widget/*.py", "NEWFILE.md", "docs/PROJECT_STATE.md"], files)
    assert kept == ["desktop_agent/control/runtime*.py", "desktop_agent/control/config.py", "tests/test_runtime*.py", "panel/*", "NEWFILE.md"]
    # new files inside existing directories (or at the top level) are allowed; widget/ and docs/ do not exist here so they are dropped


def test_plan_with_hint_skips_pick(tmp_path):
    projects = _projects(tmp_path)
    llm = FakeLLM([GOOD_PLAN])
    plan = asyncio.run(Planner(llm, projects).plan("do it", project_hint="alpha"))
    assert plan.project == "alpha" and len(llm.calls) == 1


def test_unknown_project_becomes_question(tmp_path):
    projects = _projects(tmp_path)
    llm = FakeLLM([{"project": "unknown"}])
    plan = asyncio.run(Planner(llm, projects).plan("do something somewhere"))
    assert not plan.tasks and plan.questions and "Which project" in plan.questions[0]["question"]


def test_service_plan_goal_then_decision_replans(tmp_path):
    projects = _projects(tmp_path)
    cfg = RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t",
                        project_files=[tmp_path / "alpha.yaml", tmp_path / "beta.yaml"])
    s = Service(cfg); s.scheduler.paused = True
    s.planner.llm = FakeLLM([
        {"project": "alpha", "reason": "x"},
        {"project": "alpha", "summary": "need a choice", "tasks": [], "questions": [
            {"question": "Keep the old endpoint?", "options": ["yes", "no"], "why": "product decision"}]},
        GOOD_PLAN,
    ])
    app = build_app(s, "tok")
    with TestClient(app) as c:
        h = {"Authorization": "Bearer tok"}
        r = c.post("/v0/goals", json={"text": "Improve alpha docs; decide about the old endpoint"}, headers=h)
        assert r.status_code == 201 and r.json()["status"] == "WAITING_OWNER", r.json()
        gid = r.json()["goal_id"]
        st = c.get("/v0/state", headers=h).json()
        assert st["inbox"] and st["inbox"][0]["goal_id"] == gid
        did = st["inbox"][0]["decision_id"]
        r2 = c.post(f"/v0/decisions/{did}", json={"answer": "yes"}, headers=h)
        assert r2.status_code == 200 and r2.json()["replan"]["status"] == "PLANNED"
        tids = r2.json()["replan"]["task_ids"]
        assert len(tids) == 2
        t2 = c.get(f"/v0/tasks/{tids[1]}", headers=h).json()
        assert t2["contract"]["dependencies"] == [tids[0]] and t2["contract"]["task_type"] == "tests_only"
        recs = c.get("/v0/receipts", params={"goal_id": gid}, headers=h).json()["receipts"]
        assert any(r["source"] == "planner" and "planned 2 task" in r["claim"] for r in recs)
        assert s.store.cost_summary()["today_usd"] > 0
        assert "yes" in str(s.planner.llm.calls[-1][0])          # the answer reached the re-plan prompt
        # explicit contracts still work and skip the planner
        r3 = c.post("/v0/goals", json={"project": "alpha", "text": "explicit docs task", "owned_area": ["README.md"], "task_type": "docs"}, headers=h)
        assert r3.status_code == 201 and len(r3.json()["task_ids"]) == 1
        types = [e.type for e in s.store.events()]
        assert ET.OWNER_DECISION_REQUESTED.value in types and ET.OWNER_DECISION_RECORDED.value in types
        st = c.get("/v0/state", headers=h).json()
        assert "feedback" in st and st["feedback"]["verified_tasks"] == 0 and "outcomes" in st
        assert st["slots"]["owner_ceiling"] >= st["slots"]["ceiling"]
