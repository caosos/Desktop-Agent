"""Aria's conversational layer: a cheap cloud model whose only tools are the
control-plane API. Without an API key it falls back to a direct-command mode
so the widget stays useful.

Direct API calls over urllib (no SDK dependency); the key never leaves this
process and is never passed to the control plane.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .client import ApiError, Client

PROMPT_FILE = Path(__file__).resolve().parent / "aria_prompt.md"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-5-5"

TOOLS = [
    {"name": "list_projects", "description": "Projects the control plane manages.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_state", "description": "Current workers, tasks with stages, blocked items, open owner decisions, costs.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "submit_goal", "description": "Create one bounded task from Michael's confirmed instruction.",
     "input_schema": {"type": "object", "required": ["project", "text"], "properties": {
         "project": {"type": "string"}, "text": {"type": "string"},
         "task_type": {"type": "string", "enum": ["code", "docs", "tests_only", "config"]},
         "owned_area": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "explain_task", "description": "Details of one task: stage, events, receipts, files.",
     "input_schema": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}}},
    {"name": "answer_decision", "description": "Record Michael's answer to an open owner decision.",
     "input_schema": {"type": "object", "required": ["decision_id", "answer"], "properties": {"decision_id": {"type": "string"}, "answer": {"type": "string"}}}},
    {"name": "control", "description": "pause, resume, or stop (optionally one task).",
     "input_schema": {"type": "object", "required": ["action"], "properties": {"action": {"type": "string", "enum": ["pause", "resume", "stop"]}, "task_id": {"type": "string"}}}},
]


class Aria:
    def __init__(self, client: Client, api_key: str = "", model: str = DEFAULT_MODEL):
        self.client, self.api_key, self.model = client, api_key, model
        self.history: list[dict] = []
        self.system = PROMPT_FILE.read_text() if PROMPT_FILE.exists() else "You are Aria."
        self.pending_goal: dict | None = None        # direct mode: goal awaiting confirmation

    @property
    def conversational(self) -> bool:
        return bool(self.api_key)

    # ---- tools ------------------------------------------------------------
    def run_tool(self, name: str, inp: dict | None = None) -> dict:
        inp = inp or {}
        try:
            if name == "list_projects":
                return {"projects": self.client.projects()}
            if name == "get_state":
                s = self.client.state()
                return {k: s[k] for k in ("projects", "workers", "inbox", "blocked", "next", "costs", "paused")} | \
                       {"tasks": [{k: t[k] for k in ("task_id", "status", "stage", "objective", "last_activity")} for t in s["tasks"][-10:]]}
            if name == "submit_goal":
                return self.client.submit_goal(inp["project"], inp["text"], inp.get("task_type", "code"),
                                               inp.get("owned_area"), idempotency_key=uuid.uuid4().hex)
            if name == "explain_task":
                t = self.client.task(inp["task_id"])
                return {k: t.get(k) for k in ("task_id", "status", "stage", "objective", "last_activity", "result", "files")} | \
                       {"receipts": [{"actor": r["actor"], "label": r["result_label"], "claim": r["claim"][:200]} for r in t.get("receipts", [])],
                        "events": [e["text"] for e in t.get("events", [])[-15:]]}
            if name == "answer_decision":
                return self.client.decide(inp["decision_id"], inp["answer"])
            if name == "control":
                return self.client.control(inp["action"], inp.get("task_id"))
            return {"error": f"unknown tool {name}"}
        except ApiError as exc:
            return {"error": str(exc)}

    # ---- conversational mode ---------------------------------------------
    def ask(self, text: str) -> str:
        if not self.conversational:
            return self.direct(text)
        self.history.append({"role": "user", "content": text})
        for _ in range(6):
            resp = self._anthropic(self.history)
            content = resp.get("content", [])
            self.history.append({"role": "assistant", "content": content})
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                return " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip() or "(no reply)"
            results = [{"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(self.run_tool(b["name"], b.get("input") or {}), default=str)[:12000]}
                       for b in tool_uses]
            self.history.append({"role": "user", "content": results})
        return "I stopped after several tool calls; the panel has the full state."

    def _anthropic(self, messages: list[dict]) -> dict:
        body = {"model": self.model, "max_tokens": 400, "system": self.system, "tools": TOOLS, "messages": messages[-30:]}
        req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode(), method="POST")
        req.add_header("content-type", "application/json")
        req.add_header("x-api-key", self.api_key)
        req.add_header("anthropic-version", ANTHROPIC_VERSION)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            return {"content": [{"type": "text", "text": f"Model call failed ({exc.code}): {exc.read().decode(errors='replace')[:200]}"}]}
        except (urllib.error.URLError, OSError) as exc:
            return {"content": [{"type": "text", "text": f"Model unreachable: {exc}"}]}

    # ---- direct mode (no API key) ------------------------------------------
    _CTRL = re.compile(r"^\s*(pause|resume|stop)\b", re.I)
    _STATUS = re.compile(r"^\s*(status|how('s| is) it going|what('s| is) (running|happening)|progress)\b", re.I)
    _GOAL = re.compile(r"^\s*(?:in|on|for)\s+([\w.-]+)\s*[:,]\s*(.+)$", re.I | re.S)

    def direct(self, text: str) -> str:
        t = text.strip()
        if not t:
            return ""
        if self.pending_goal:
            if t.lower() in ("yes", "y", "go", "do it", "ok", "okay", "confirm"):
                g, self.pending_goal = self.pending_goal, None
                r = self.run_tool("submit_goal", g)
                return f"Submitted. Task {', '.join(r.get('task_ids', []))}." if "task_ids" in r else f"Not submitted: {r.get('error')}"
            if t.lower() in ("no", "n", "cancel"):
                self.pending_goal = None
                return "Cancelled."
        m = self._CTRL.match(t)
        if m:
            r = self.run_tool("control", {"action": m.group(1).lower()})
            return f"{m.group(1).capitalize()}d." if "error" not in r else r["error"]
        if self._STATUS.match(t):
            return self._status_line()
        m = self._GOAL.match(t)
        projects = [p["name"] for p in self.run_tool("list_projects").get("projects", [])]
        if m and m.group(1) in projects:
            self.pending_goal = {"project": m.group(1), "text": m.group(2).strip(), "task_type": "code"}
        elif len(projects) == 1:
            self.pending_goal = {"project": projects[0], "text": t, "task_type": "code"}
        else:
            return f"Which project? Say 'in <project>: <what to do>'. Projects: {', '.join(projects) or 'none'}."
        return f"Submit to {self.pending_goal['project']}: \"{self.pending_goal['text'][:120]}\"? (yes/no)"

    def _status_line(self) -> str:
        s = self.run_tool("get_state")
        if "error" in s:
            return s["error"]
        running = [t for t in s["tasks"] if t["status"] == "RUNNING"]
        parts = []
        if running:
            parts.append("; ".join(f"{t['task_id']} is {t['stage']}: {t['last_activity']}" for t in running))
        else:
            parts.append("Nothing running." + (" Paused." if s.get("paused") else ""))
        if s["blocked"]:
            parts.append(f"{len(s['blocked'])} blocked.")
        if s["inbox"]:
            parts.append(f"Needs you: {s['inbox'][0]['question']}")
        elif s["next"]:
            parts.append(f"Next: {s['next']['task_id']}.")
        return " ".join(parts)
