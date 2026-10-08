"""Adapter parsing against real stream-json shapes, sandbox wrapping, scheduler admission."""
import json
from pathlib import Path

from desktop_agent.control import sandbox
from desktop_agent.control.adapters.claude_headless import ClaudeHeadlessAdapter
from desktop_agent.control.config import RuntimeConfig, SchedulerPolicy
from desktop_agent.control.contracts import Budget, TaskContract
from desktop_agent.control.events import EventType as ET
from desktop_agent.control.scheduler import admission, areas_overlap, free_port
from desktop_agent.control.router import default_class, escalate, resolve

INIT = {"type": "system", "subtype": "init", "cwd": "/w", "session_id": "s1", "tools": ["Read", "Write"]}
WRITE = {"type": "assistant", "message": {"model": "claude-sonnet-5-5", "content": [
    {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/w/hello.txt", "content": "hi"}}]}}
WRITE_RES = {"type": "user", "message": {"content": [{"tool_use_id": "t1", "type": "tool_result", "content": "File created"}]}}
READ = {"type": "assistant", "message": {"model": "m", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/w/AGENTS.md"}}]}}
TEST = {"type": "assistant", "message": {"model": "m", "content": [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "backend/scripts/run_backend_tests.sh -q"}}]}}
TEST_RES = {"type": "user", "message": {"content": [{"tool_use_id": "t3", "type": "tool_result", "content": "... 298 passed, 31 skipped in 40s"}]}}
COMMIT = {"type": "assistant", "message": {"model": "m", "content": [{"type": "tool_use", "id": "t4", "name": "Bash", "input": {"command": "git commit -m 'x'"}}]}}
COMMIT_RES = {"type": "user", "message": {"content": [{"tool_use_id": "t4", "type": "tool_result", "content": "[agent/x abc1234] x\n 1 file changed"}]}}
DENY = {"type": "user", "message": {"content": [{"tool_use_id": "t9", "type": "tool_result", "is_error": True, "content": "Permission denied: Bash(git push)"}]}}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 6823, "num_turns": 3, "result": "STATUS: DONE\nCOMMIT: abc1234def\nTESTS: 298 passed\nFILES: a.py (120)\nBLOCKER: none\nNOTE: ok",
          "session_id": "s1", "total_cost_usd": 0.27, "usage": {"input_tokens": 34, "cache_creation_input_tokens": 12995, "cache_read_input_tokens": 27731, "output_tokens": 136}}


def lines(*msgs):
    return [json.dumps(m) for m in msgs]


def test_adapter_parses_stream():
    a = ClaudeHeadlessAdapter()
    out = []
    final = None
    for ln in lines(INIT, WRITE, WRITE_RES, READ, TEST, TEST_RES, COMMIT, COMMIT_RES, RESULT) + ["not json", ""]:
        p = a.parse_line(ln)
        out += [t for t, _ in p.events]
        if p.final:
            final = p.final
    assert out == [ET.FILE_CHANGED.value, ET.FILE_READ.value, ET.TEST_STARTED.value,
                   ET.TEST_PASSED.value, ET.TOOL_CALLED.value, ET.COMMIT_CREATED.value, ET.CLAIM_WRITTEN.value]
    assert a.session_id == "s1"
    # ordinary commands mentioning "test" in a path are not test runs
    p = a.parse_line(json.dumps({"type": "assistant", "message": {"model": "m", "content": [
        {"type": "tool_use", "id": "t7", "name": "Bash", "input": {"command": "ls backend/tests | wc -l"}}]}}))
    assert p.events[0][0] == ET.TOOL_CALLED.value
    assert final["ok"] and final["claim_status"] == "DONE" and final["claim_commit"] == "abc1234def"
    assert final["cost_usd"] == 0.27 and final["input_tokens"] == 34 + 12995 + 27731


def test_adapter_denied_and_failed_tests():
    a = ClaudeHeadlessAdapter()
    a._pending_tools["t9"] = ("Bash", "git push")
    p = a.parse_line(json.dumps(DENY))
    assert p.events[0][0] == ET.TOOL_DENIED.value
    a.parse_line(json.dumps(TEST))
    bad = dict(TEST_RES); bad["message"] = {"content": [{"tool_use_id": "t3", "type": "tool_result", "content": "2 failed, 290 passed"}]}
    assert a.parse_line(json.dumps(bad)).events[0][0] == ET.TEST_FAILED.value


def test_adapter_launch_argv(tmp_path: Path):
    a = ClaudeHeadlessAdapter()
    c = TaskContract(task_id="t", objective="o", why_now="w", project="p", base_ref="main", owned_area=[], read_list=[],
                     acceptance_tests=[], allowed_tools=["Read", "Bash(git commit:*)"], forbidden_actions=[],
                     model_class="cloud_strong", budget=Budget(usd=2.5, max_turns=12, wall_clock_sec=60))
    spec = a.launch(contract=c, project=None, prompt="hello", model="claude-sonnet-5-5", workspace=tmp_path)
    argv = spec.argv
    assert argv[:3] == ["claude", "-p", "hello"]
    assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "12"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.50"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert "Bash(git push:*)" in argv[argv.index("--disallowedTools"):]


def test_sandbox_wrap_shapes(tmp_path: Path):
    cfg = RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t")
    ws = tmp_path / "w" / "repo"; ws.mkdir(parents=True)
    home = tmp_path / "w" / "home"; home.mkdir()
    argv = sandbox.wrap(cfg, unit_name="u", workspace=ws, worker_home=home, ro_paths=[str(tmp_path)], inner=["true"])
    if sandbox.have_systemd_run():
        assert argv[0] == "systemd-run" and "-pMemoryMax=3G" in argv
    if sandbox.have_bwrap():
        i = argv.index("bwrap")
        assert argv[i + 1:i + 3] == ["--ro-bind", "/"]
        assert "--tmpfs" in argv and str(Path.home()) in argv
        assert argv[-1] == "true"
    cfg.use_bwrap = cfg.use_systemd_scope = False
    assert sandbox.wrap(cfg, unit_name="u", workspace=ws, worker_home=home, ro_paths=[], inner=["true"]) == ["true"]


def test_sandbox_wrap_executes(tmp_path: Path):
    """The wrapped command must actually run on this host (catches bad systemd/bwrap arguments)."""
    import os, subprocess, uuid
    import pytest
    if os.environ.get("DESKTOP_AGENT_WORKER"):
        pytest.skip("already inside a worker sandbox; nested systemd scopes are unavailable")
    cfg = RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t")
    ws = tmp_path / "w" / "repo"; ws.mkdir(parents=True)
    home = tmp_path / "w" / "home"; home.mkdir()
    argv = sandbox.wrap(cfg, unit_name=f"desktop-agent-test-{uuid.uuid4().hex[:6]}", workspace=ws, worker_home=home,
                        ro_paths=[], inner=["/bin/sh", "-c", "echo HOME=$HOME; touch $HOME/ok; pwd"], runtime_max_sec=30)
    r = subprocess.run(argv, env=sandbox.worker_env(home), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert f"HOME={home}" in r.stdout and str(ws) in r.stdout
    assert (home / "ok").exists()
    if sandbox.have_bwrap() and cfg.use_bwrap:
        r2 = subprocess.run(sandbox.wrap(cfg, unit_name=f"desktop-agent-test-{uuid.uuid4().hex[:6]}", workspace=ws,
                                         worker_home=home, ro_paths=[], inner=["/bin/sh", "-c", "touch /usr/forbidden"]),
                            env=sandbox.worker_env(home), capture_output=True, text=True, timeout=60)
        assert r2.returncode != 0, "root must be read-only inside the sandbox"
    env = sandbox.worker_env(home, 8101, {"ANTHROPIC_API_KEY": "x", "FOO": "1"})
    assert "ANTHROPIC_API_KEY" not in env and env["FOO"] == "1" and env["HOME"] == str(home)


def test_prepare_worker_home_copies_only_credentials(tmp_path: Path):
    cfg = RuntimeConfig(data_dir=tmp_path, workspaces_dir=tmp_path, token_file=tmp_path / "t",
                        worker_credentials=tmp_path / "c.json", worker_claude_config=tmp_path / "cfg.json")
    (tmp_path / "c.json").write_text("{}"); (tmp_path / "cfg.json").write_text("{}")
    home = sandbox.prepare_worker_home(cfg, tmp_path / "home")
    assert sorted(p.name for p in home.iterdir()) == [".claude", ".claude.json"]
    assert (home / ".claude" / ".credentials.json").stat().st_mode & 0o777 == 0o600


def test_admission_and_overlap(tmp_path: Path):
    cfg = RuntimeConfig(data_dir=tmp_path, workspaces_dir=tmp_path, token_file=tmp_path / "t",
                        scheduler=SchedulerPolicy(ceiling=4, reserved_cores=0, cores_per_worker=1, host_reserve_mb=0,
                                                  mem_per_worker_mb=1, hourly_cap_usd=10, daily_cap_usd=20,
                                                  expected_cost_per_worker_hour_usd=2))
    a = admission(cfg, running=1, spend_last_hour=0, spend_today=0)
    assert a["slots"] == 4 and a["free"] == 3 and a["budget_slots"] == 5
    assert admission(cfg, 0, spend_last_hour=9.5, spend_today=0)["slots"] == 0
    assert admission(cfg, 0, 0, spend_today=25)["slots"] == 0
    assert areas_overlap(["backend/routes/*"], ["backend/routes/alerts.py"])
    assert areas_overlap(["backend/"], ["backend/x.py"])
    assert not areas_overlap(["backend/a.py"], ["frontend/b.js"])
    assert areas_overlap([], ["x"])
    p = free_port(8100, 8199, taken={8100})
    assert 8101 <= p <= 8199


def test_router(tmp_path: Path):
    cfg = RuntimeConfig(data_dir=tmp_path, workspaces_dir=tmp_path, token_file=tmp_path / "t")
    assert default_class("docs") == "cloud_cheap" and default_class("code") == "cloud_strong"
    assert resolve(cfg, "cloud_strong") == "claude-sonnet-5-5"
    assert escalate("cloud_cheap") == "cloud_strong" and escalate("cloud_max") is None
