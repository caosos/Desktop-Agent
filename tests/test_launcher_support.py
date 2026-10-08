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


def test_model_floor(tmp_path: Path):
    assert at_least("cloud_cheap", "cloud_strong") == "cloud_strong"
    assert at_least("cloud_max", "cloud_strong") == "cloud_max"
    assert at_least("cloud_cheap", None) == "cloud_cheap"
    p = _project(tmp_path, "min_model_class: cloud_strong\n")
    assert p.min_model_class == "cloud_strong"
