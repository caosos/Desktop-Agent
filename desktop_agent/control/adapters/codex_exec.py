"""OpenAI Codex CLI adapter: `codex exec --json` (JSON Lines events).

Codex brings its own Landlock/seccomp sandbox (`--sandbox workspace-write`)
inside our bwrap + systemd scope. Auth comes from the per-worker HOME
(`.codex/auth.json`, copied by the launcher when configured). Running on a
ChatGPT subscription reports no dollar cost; tokens are recorded and the
cost is marked unknown rather than invented.

Codex reads "additional input" from stdin and waits while it is open, so the
launcher's stdin=DEVNULL is required (a probe with an open stdin hung).

Event shapes (codex-cli 0.153.4 `exec --json`, from a real probe on
2026-10-08; see tests/test_codex_adapter.py):
  {"type":"thread.started","thread_id":...}
  {"type":"turn.started"}
  {"type":"item.started"|"item.completed","item":{"id":..,"type":"command_execution","command":..,"aggregated_output":..,"exit_code":..,"status":..}}
  {"type":"item.completed","item":{"type":"file_change","changes":[{"path":..,"kind":"add|update|delete"}],"status":..}}
  {"type":"item.completed","item":{"type":"agent_message","text":...}}
  {"type":"item.completed","item":{"type":"reasoning","text":...}}
  {"type":"turn.completed","usage":{"input_tokens":..,"cached_input_tokens":..,"output_tokens":..}}
  {"type":"turn.failed","error":{"message":...}} | {"type":"error","message":...}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..contracts import TaskContract
from ..events import EventType as ET
from ..project import ProjectPackage
from .base import LaunchSpec, Parsed
from .claude_headless import _COMMIT_RE, _LIMIT_RE, _STATUS_RE, _TEST_HINT, _test_passed

_COMMIT_HINT = re.compile(r"\bgit\s+commit\b")


class CodexExecAdapter:
    name = "codex_exec"

    def __init__(self, codex_bin: str = "codex"):
        self.codex_bin = codex_bin
        self._test_cmds: list[str] = []
        self._last_message = ""
        self._usage: dict = {}
        self._error: str | None = None
        self.session_id: str | None = None

    def launch(self, *, contract: TaskContract, project: ProjectPackage, prompt: str,
               model: str, workspace: Path) -> LaunchSpec:
        self._test_cmds = [c.split()[0] for c in contract.acceptance_tests if c.strip()]
        self._last_message, self._usage, self._error = "", {}, None
        argv = [self.codex_bin, "exec", "--json", "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "workspace-write", "-C", str(workspace), "-m", model, prompt]
        return LaunchSpec(argv=argv)

    def _is_test(self, cmd: str) -> bool:
        return any(t and t in cmd for t in self._test_cmds) or bool(_TEST_HINT.search(cmd))

    def parse_line(self, line: str) -> Parsed:
        line = line.strip()
        if not line.startswith("{"):
            return Parsed()
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return Parsed()
        t = msg.get("type", "")
        if t == "thread.started":
            self.session_id = msg.get("thread_id")
            return Parsed()
        if t == "item.completed":
            return self._item(msg.get("item") or {})
        if t == "turn.completed":
            self._usage = msg.get("usage") or {}
            return self._final(ok=True)
        if t in ("turn.failed", "error"):
            self._error = (msg.get("error") or {}).get("message") if isinstance(msg.get("error"), dict) else msg.get("message")
            return self._final(ok=False)
        return Parsed()

    def _item(self, item: dict) -> Parsed:
        kind = item.get("type")
        if kind == "command_execution":
            cmd = str(item.get("command", ""))[:400]
            out = str(item.get("aggregated_output") or "")
            code = item.get("exit_code")
            if self._is_test(cmd):
                passed = (code == 0) if code is not None else _test_passed(out)
                return Parsed(events=[(ET.TEST_STARTED.value, {"command": cmd}),
                                      ((ET.TEST_PASSED if passed else ET.TEST_FAILED).value, {"command": cmd, "tail": out[-1500:]})])
            if _COMMIT_HINT.search(cmd) and code == 0:
                m = re.search(r"\[[\w./-]+ ([0-9a-f]{7,40})\]", out) or re.search(r"\b([0-9a-f]{40})\b", out)
                return Parsed(events=[(ET.TOOL_CALLED.value, {"tool": "shell", "command": cmd, "intent": "commit"}),
                                      (ET.COMMIT_CREATED.value, {"sha": m.group(1) if m else None, "tail": out[-500:]})])
            etype = ET.TOOL_DENIED if (code not in (None, 0) and "denied" in out.lower()[:300]) else ET.TOOL_CALLED
            return Parsed(events=[(etype.value, {"tool": "shell", "command": cmd, "exit_code": code})])
        if kind == "file_change":
            evs = []
            for ch in item.get("changes") or []:
                evs.append((ET.FILE_CHANGED.value, {"tool": "apply_patch", "path": ch.get("path"), "kind": ch.get("kind")}))
            return Parsed(events=evs)
        if kind == "agent_message":
            self._last_message = str(item.get("text") or "")
        return Parsed()

    def _final(self, ok: bool) -> Parsed:
        text = self._last_message or (self._error or "")
        status = _STATUS_RE.search(text)
        commit = _COMMIT_RE.search(text)
        u = self._usage
        final = {
            "ok": ok and self._error is None,
            "subtype": "success" if ok else "error",
            "claim": text,
            "claim_status": status.group(1) if status else None,
            "claim_commit": commit.group(1) if commit and commit.group(1).lower() != "none" else None,
            "cost_usd": 0.0,
            "cost_known": False,                 # subscription: no dollar figure is reported
            "input_tokens": int(u.get("input_tokens") or 0) + int(u.get("cached_input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "num_turns": None,
            "duration_ms": None,
            "session_id": self.session_id,
            "provider_limited": bool(_LIMIT_RE.search(text)) or bool(self._error and _LIMIT_RE.search(self._error)),
            "error": self._error,
        }
        return Parsed(events=[(ET.CLAIM_WRITTEN.value, {"status": final["claim_status"], "commit": final["claim_commit"],
                                                       "claim": text[:4000], "error": self._error})], final=final)
