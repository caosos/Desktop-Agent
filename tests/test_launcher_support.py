"""Launcher support: per-task test script, prompt rendering, project model floor."""
import subprocess
from pathlib import Path

from desktop_agent.control.contracts import Budget, TaskContract
from desktop_agent.control.launcher import render_prompt, write_test_script
from desktop_agent.control.project import ProjectPackage
from desktop_agent.control.router import at_least
from desktop_agent.control.workspace import Workspace


def _project(tmp_path: Path, extra: str = "") -> ProjectPackage:
    y = tmp_path / "p.yaml"
    y.write_text("name: demo\nrepo_path: /tmp/demo\nremote_url: x\nintegration_branch: main\nstart_here: START.md\n"
                 "agents_file: AGENTS.md\ncurrent_state: docs/STATE.md\ntest_command: ./test.sh\n"
                 "test_env: {PORT: '{port}', DB: 'gate_{task_id}', EMPTY: ''}\n" + extra)
    return ProjectPackage.load(y)


def _contract() -> TaskContract:
    return TaskContract(task_id="fix-x-ab12cd", objective="o", why_now="w", project="demo", base_ref="main",
                        owned_area=["README.md"], read_list=["START.md", "AGENTS.md", "docs/STATE.md"],
                        acceptance_tests=["./test.sh"], allowed_tools=["Read"], forbidden_actions=["git push"],
                        model_class="cloud_cheap", budget=Budget(usd=1, max_turns=5, wall_clock_sec=60))


def test_test_script_sets_env_and_runs(tmp_path: Path):
    p = _project(tmp_path)
    task_dir = tmp_path / "task"
    repo = task_dir / "repo"; repo.mkdir(parents=True)
    (repo / "test.sh").write_text("#!/bin/sh\necho PORT=$PORT DB=$DB EMPTY=[$EMPTY] ARGS=$*\n"); (repo / "test.sh").chmod(0o755)
    script = write_test_script(task_dir, _contract(), p, 8123)
    out = subprocess.run([str(script), "-k", "x"], capture_output=True, text=True, check=True).stdout
    assert "PORT=8123 DB=gate_fix_x_ab12cd EMPTY=[] ARGS=-k x" in out


def test_prompt_mentions_script_and_tail_only(tmp_path: Path):
    p = _project(tmp_path)
    wsp = Workspace(path=tmp_path, branch="agent/fix-x-ab12cd", base_ref="main", base_sha="abc")
    prompt = render_prompt(_contract(), p, wsp, 8123, tmp_path / "bin" / "run_tests.sh")
    assert "docs/STATE.md (tail only" in prompt and str(tmp_path / "bin" / "run_tests.sh") in prompt
    assert "agent/fix-x-ab12cd" in prompt and "abc" in prompt and "Write/Edit tools" in prompt


def test_state_entry_policy(tmp_path: Path):
    worker_mode = _project(tmp_path)
    assert worker_mode.with_state_file(["panel/index.html"]) == ["panel/index.html", "docs/STATE.md"]
    assert worker_mode.with_state_file([]) == []                       # unrestricted stays unrestricted
    control_mode = _project(tmp_path, "state_entry_by: control\n")
    assert control_mode.with_state_file(["panel/index.html"]) == ["panel/index.html"]
    wsp = Workspace(path=tmp_path, branch="b", base_ref="main", base_sha="abc")
    p1 = render_prompt(_contract(), worker_mode, wsp, 1, None)
    p2 = render_prompt(_contract(), control_mode, wsp, 1, None)
    assert "Append the dated entry" in p1 and "Do NOT edit docs/STATE.md" in p2


def test_control_plane_state_entry_commit(tmp_path: Path):
    import asyncio, subprocess
    from desktop_agent.control.config import RuntimeConfig
    from desktop_agent.control.integrator import Integrator
    from desktop_agent.control.store import Store
    repo = tmp_path / "repo"; (repo / "docs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "docs" / "STATE.md").write_text("# state\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"], check=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    pkg = _project(tmp_path, "state_entry_by: control\n")
    cfg = RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t")
    integ = Integrator(cfg, Store(cfg.data_dir))
    wsp = Workspace(path=repo, branch="agent/x", base_ref="main", base_sha=base)
    sha = asyncio.run(integ.record_state_entry(_contract(), pkg, wsp, {"tests": [{"command": "./t.sh", "exit_code": 0}], "files": [{"path": "a.py"}]}))
    assert sha and sha != base
    text = (repo / "docs" / "STATE.md").read_text()
    assert "recorded by the control plane" in text and "./t.sh` exit 0" in text and "fix-x-ab12cd" in text
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""
    assert asyncio.run(integ.record_state_entry(_contract(), _project(tmp_path), wsp, {})) is None   # worker mode: no-op


def test_model_floor(tmp_path: Path):
    assert at_least("cloud_cheap", "cloud_strong") == "cloud_strong"
    assert at_least("cloud_max", "cloud_strong") == "cloud_max"
    assert at_least("cloud_cheap", None) == "cloud_cheap"
    p = _project(tmp_path, "min_model_class: cloud_strong\n")
    assert p.min_model_class == "cloud_strong"
