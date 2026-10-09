"""Claude Code headless adapter: `claude -p ... --output-format stream-json`.

Stream shapes observed on 2026-10-08 with Claude Code 2.1.293:
  {"type":"system","subtype":"init",...,"session_id":...}
  {"type":"assistant","message":{"model":..., "content":[{"type":"tool_use","name":"Write","input":{...}} | {"type":"text","text":...}]}}
  {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":..., "content":...,"is_error":?}]}}
  {"type":"rate_limit_event",...}
  {"type":"result","subtype":"success"|...,"total_cost_usd":...,"usage":{...},"result":"<final text>"}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..contracts import TaskContract
from ..events import EventType as ET
from ..project import ProjectPackage
from .base import LaunchSpec, Parsed

_READ_TOOLS = {"Read", "Glob", "Grep", "NotebookRead"}
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_TEST_HINT = re.compile(r"(run_tests\.sh|run_backend_tests|\bpytest\b|npm test|yarn test|npm run test|CI=true .*build|\./test\.sh)", re.I)
_COMMIT_HINT = re.compile(r"\bgit\s+commit\b")
_STATUS_RE = re.compile(r"^STATUS:\s*(DONE|BLOCKED|FAILED)", re.M)
_LIMIT_RE = re.compile(r"(hit your (session|usage) limit|rate limit(ed)?|usage limit reached|resets \d{1,2}:\d{2}\s*[ap]m)", re.I)
_COMMIT_RE = re.compile(r"^COMMIT:\s*([0-9a-f]{7,40}|none)", re.M | re.I)


class ClaudeHeadlessAdapter:
    name = "claude_headless"
    commits_itself = True

    def __init__(self, claude_bin: str = "claude"):
        self.claude_bin = claude_bin
        self._pending_tools: dict[str, tuple[str, str]] = {}   # tool_use_id -> (name, summary)
        self._test_cmds: list[str] = []                        # the contract's acceptance commands
        self.session_id: str | None = None
        self.last_rate_limit: dict = {}                        # Claude Code's own quota window report

    def _is_test(self, cmd: str) -> bool:
        return any(t and t in cmd for t in self._test_cmds) or bool(_TEST_HINT.search(cmd))

    def launch(self, *, contract: TaskContract, project: ProjectPackage, prompt: str,
               model: str, workspace: Path) -> LaunchSpec:
        self._test_cmds = [c.split()[0] for c in contract.acceptance_tests if c.strip()]
        argv = [
            self.claude_bin, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--no-session-persistence",
            "--model", model,
            "--max-turns", str(contract.budget.max_turns),
            "--max-budget-usd", f"{contract.budget.usd:.2f}",
            "--permission-mode", "acceptEdits",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ]
        if contract.allowed_tools:
            argv += ["--allowedTools", *contract.allowed_tools]
        argv += ["--disallowedTools", "WebFetch", "WebSearch", "Task", "SendMessage",
                 "Bash(git push:*)", "Bash(gh:*)", "Bash(sudo:*)", "Bash(rm -rf:*)", "Bash(curl:*)", "Bash(wget:*)",
                 "Bash(ssh:*)", "Bash(scp:*)", "Bash(git reset --hard:*)", "Bash(git push --force:*)"]
        return LaunchSpec(argv=argv)

    # ---- parsing ----------------------------------------------------------
    def parse_line(self, line: str) -> Parsed:
        line = line.strip()
        if not line.startswith("{"):
            return Parsed()
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return Parsed()
        t = msg.get("type")
        if t == "system" and msg.get("subtype") == "init":
            self.session_id = msg.get("session_id")      # evidence for the final receipt; the router already emitted MODEL_SELECTED
            return Parsed()
        if t == "rate_limit_event":
            self.last_rate_limit = msg.get("rate_limit_info") or {}
            return Parsed()
        if t == "assistant":
            return self._assistant(msg)
        if t == "user":
            return self._tool_results(msg)
        if t == "result" or ("total_cost_usd" in msg and "stop_reason" in msg):
            return self._result(msg)
        return Parsed()

    def _assistant(self, msg: dict) -> Parsed:
        model = (msg.get("message") or {}).get("model")
        out: list[tuple[str, dict]] = []
        for block in (msg.get("message") or {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            name, inp, tid = block.get("name", ""), block.get("input") or {}, block.get("id", "")
            path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
            if name in _READ_TOOLS:
                summary = path or inp.get("pattern") or ""
                out.append((ET.FILE_READ.value, {"tool": name, "path": summary, "model": model}))
            elif name in _WRITE_TOOLS:
                summary = path or ""
                out.append((ET.FILE_CHANGED.value, {"tool": name, "path": summary, "model": model}))
            elif name == "Bash":
                cmd = str(inp.get("command", ""))[:400]
                summary = cmd
                if self._is_test(cmd):
                    out.append((ET.TEST_STARTED.value, {"command": cmd, "model": model}))
                elif _COMMIT_HINT.search(cmd):
                    out.append((ET.TOOL_CALLED.value, {"tool": name, "command": cmd, "intent": "commit", "model": model}))
                else:
                    out.append((ET.TOOL_CALLED.value, {"tool": name, "command": cmd, "model": model}))
            else:
                summary = json.dumps(inp)[:200]
                out.append((ET.TOOL_CALLED.value, {"tool": name, "input": summary, "model": model}))
            if tid:
                self._pending_tools[tid] = (name, summary)
        return Parsed(events=out)

    def _tool_results(self, msg: dict) -> Parsed:
        out: list[tuple[str, dict]] = []
        for block in (msg.get("message") or {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id", "")
            name, summary = self._pending_tools.pop(tid, ("?", ""))
            content = block.get("content")
            text = content if isinstance(content, str) else json.dumps(content)[:2000] if content else ""
            is_error = bool(block.get("is_error")) or "permission" in text.lower()[:300] and "denied" in text.lower()[:300]
            if name == "Bash" and self._is_test(summary):
                passed = _test_passed(text)
                out.append(((ET.TEST_PASSED if passed else ET.TEST_FAILED).value,
                            {"command": summary, "tail": text[-1500:]}))
            elif name == "Bash" and _COMMIT_HINT.search(summary) and not is_error:
                m = re.search(r"\[[\w./-]+ ([0-9a-f]{7,40})\]", text) or re.search(r"\b([0-9a-f]{40})\b", text)
                out.append((ET.COMMIT_CREATED.value, {"sha": m.group(1) if m else None, "tail": text[-500:]}))
            elif is_error:
                out.append((ET.TOOL_DENIED.value, {"tool": name, "summary": summary, "error": text[:500]}))
        return Parsed(events=out)

    def _result(self, msg: dict) -> Parsed:
        text = msg.get("result") or ""
        status = _STATUS_RE.search(text)
        commit = _COMMIT_RE.search(text)
        usage = msg.get("usage") or {}
        final = {
            "ok": msg.get("subtype", "success") == "success" and not msg.get("is_error", False),
            "subtype": msg.get("subtype"),
            "claim": text,
            "claim_status": status.group(1) if status else None,
            "claim_commit": commit.group(1) if commit and commit.group(1).lower() != "none" else None,
            "cost_usd": float(msg.get("total_cost_usd") or 0.0),
            "input_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
                            + int(usage.get("cache_creation_input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "num_turns": msg.get("num_turns"),
            "duration_ms": msg.get("duration_ms"),
            "session_id": msg.get("session_id"),
            "provider_limited": bool(_LIMIT_RE.search(text)) and (msg.get("num_turns") or 0) <= 1,
            "rate_limit": self.last_rate_limit or None,
        }
        return Parsed(events=[(ET.CLAIM_WRITTEN.value, {"status": final["claim_status"], "commit": final["claim_commit"],
                                                       "claim": text[:4000]})], final=final)


def _test_passed(text: str) -> bool:
    t = text.lower()
    if re.search(r"\b\d+ failed\b", t) and not re.search(r"\b0 failed\b", t):
        return False
    if "error" in t[-400:] and "passed" not in t:
        return False
    return bool(re.search(r"\b\d+ passed\b", t)) or "exit 0" in t[-100:]
