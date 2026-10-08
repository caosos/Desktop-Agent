"""Codex adapter parsed against the real `codex exec --json` probe output (2026-10-08, codex-cli 0.153.4)."""
import json
from pathlib import Path

from desktop_agent.control.adapters.codex_exec import CodexExecAdapter
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.contracts import Budget, TaskContract
from desktop_agent.control.events import EventType as ET
from desktop_agent.control.router import resolve
from desktop_agent.control import sandbox

PROBE = [
    {"type": "thread.started", "thread_id": "01a11d00-0316-76b3-aad6-bc4e4e69819e"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "item_0", "type": "file_change", "changes": [{"path": "/w/hello.txt", "kind": "add"}], "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_0", "type": "file_change", "changes": [{"path": "/w/hello.txt", "kind": "add"}], "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "./test.sh", "aggregated_output": "1 passed", "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_2", "type": "command_execution", "command": "git commit -q -m x && git rev-parse HEAD", "aggregated_output": "0123456789abcdef0123456789abcdef01234567\n", "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": "STATUS: DONE\nCOMMIT: 0123456789abcdef0123456789abcdef01234567\nTESTS: 1 passed\nFILES: hello.txt\nBLOCKER: none\nNOTE: ok"}},
    {"type": "turn.completed", "usage": {"input_tokens": 27143, "cached_input_tokens": 22272, "cache_write_input_tokens": 0, "output_tokens": 86, "reasoning_output_tokens": 35}},
]


def _contract():
    return TaskContract(task_id="t", objective="o", why_now="w", project="p", base_ref="main", owned_area=[], read_list=[],
                        acceptance_tests=["./test.sh"], allowed_tools=[], forbidden_actions=[], model_class="cloud_strong",
                        budget=Budget(usd=1, max_turns=5, wall_clock_sec=60), worker_adapter="codex_exec")


def test_codex_parse_probe():
    a = CodexExecAdapter()
    a.launch(contract=_contract(), project=None, prompt="p", model="gpt-5.6-sol", workspace=Path("/w"))
    types, final = [], None
    for m in PROBE:
        p = a.parse_line(json.dumps(m))
        types += [t for t, _ in p.events]
        if p.final:
            final = p.final
    assert types == [ET.FILE_CHANGED.value, ET.TEST_STARTED.value, ET.TEST_PASSED.value, ET.TOOL_CALLED.value,
                     ET.COMMIT_CREATED.value, ET.CLAIM_WRITTEN.value]
    assert final["ok"] and final["claim_status"] == "DONE" and final["claim_commit"].startswith("0123456789")
    assert final["cost_known"] is False and final["cost_usd"] == 0.0 and final["input_tokens"] == 27143 + 22272
    assert a.session_id == "01a11d00-0316-76b3-aad6-bc4e4e69819e"


def test_codex_failure_and_limit():
    a = CodexExecAdapter()
    a.launch(contract=_contract(), project=None, prompt="p", model="m", workspace=Path("/w"))
    p = a.parse_line(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. resets 3:00pm"}}))
    assert p.final and not p.final["ok"] and p.final["provider_limited"]


def test_codex_launch_argv_and_models(tmp_path: Path):
    a = CodexExecAdapter()
    spec = a.launch(contract=_contract(), project=None, prompt="hello", model="gpt-5.6-sol", workspace=tmp_path)
    assert spec.argv[:3] == ["codex", "exec", "--json"] and "--ephemeral" in spec.argv and spec.argv[-1] == "hello"
    assert spec.argv[spec.argv.index("-C") + 1] == str(tmp_path)
    cfg = RuntimeConfig(data_dir=tmp_path, workspaces_dir=tmp_path, token_file=tmp_path / "t",
                        adapter_models={"codex_exec": {"cloud_strong": "gpt-5.6-sol"}})
    assert resolve(cfg, "cloud_strong", "codex_exec") == "gpt-5.6-sol"
    assert resolve(cfg, "cloud_strong") == "claude-sonnet-5-5"


def test_worker_home_extra_files(tmp_path: Path):
    (tmp_path / "auth.json").write_text("{}")
    cfg = RuntimeConfig(data_dir=tmp_path, workspaces_dir=tmp_path, token_file=tmp_path / "t",
                        worker_credentials=tmp_path / "none", worker_claude_config=tmp_path / "none",
                        worker_home_files=[(tmp_path / "auth.json", ".codex/auth.json"), (tmp_path / "missing", ".codex/x")])
    home = sandbox.prepare_worker_home(cfg, tmp_path / "home")
    assert (home / ".codex" / "auth.json").exists() and not (home / ".codex" / "x").exists()
    assert (home / ".codex" / "auth.json").stat().st_mode & 0o777 == 0o600
