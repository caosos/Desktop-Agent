"""Structured model calls for the control plane itself (planner, classifier).

Provider-neutral surface: `complete(prompt, schema, model_class)` → dict.
Backends:
  - claude_cli: `claude -p --output-format json --json-schema ... --tools ""` with no
    tools and no MCP, so it is a pure model call billed to the subscription and
    reported with its own `total_cost_usd`.
  - anthropic_api / openai_api: direct API calls when a key is configured (Stage 2+).
Every call returns the parsed object plus cost and model so the caller can
write a receipt with provenance.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import RuntimeConfig

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


@dataclass
class Completion:
    data: dict
    model: str
    backend: str
    cost_usd: float | None          # None = unknown (no figure reported)
    input_tokens: int
    output_tokens: int
    duration_ms: int
    raw_text: str = ""


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("no JSON object in model output")
    return json.loads(m.group(0))


class LLM:
    def __init__(self, cfg: RuntimeConfig, backend: str | None = None, claude_bin: str = "claude"):
        self.cfg = cfg
        self.claude_bin = claude_bin
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.backend = backend or ("anthropic_api" if self.anthropic_key else "claude_cli")

    def model_for(self, model_class: str) -> str:
        return self.cfg.models.get(model_class) or self.cfg.models["cloud_cheap"]

    async def complete(self, prompt: str, schema: dict, model_class: str = "cloud_cheap",
                       system: str | None = None, timeout: int = 180) -> Completion:
        model = self.model_for(model_class)
        if self.backend == "anthropic_api":
            return await asyncio.to_thread(self._anthropic, prompt, schema, model, system, timeout)
        return await self._claude_cli(prompt, schema, model, system, timeout)

    # ---- backends ---------------------------------------------------------
    async def _claude_cli(self, prompt: str, schema: dict, model: str, system: str | None, timeout: int) -> Completion:
        argv = [self.claude_bin, "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema),
                "--model", model, "--max-turns", "1", "--tools", "", "--no-session-persistence",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if system:
            argv += ["--append-system-prompt", system]
        started = time.time()
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "DESKTOP_AGENT_LLM": "1"}, limit=16 * 1024 * 1024)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise LLMError("claude_cli call timed out")
        text = out.decode(errors="replace")
        try:
            msg = json.loads(text[text.index("{"):]) if "{" in text else {}
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude_cli returned non-JSON: {text[:200]!r} ({exc})")
        if msg.get("is_error") and not msg.get("structured_output"):
            raise LLMError(f"claude_cli error: {str(msg.get('result'))[:300]}")
        data = msg.get("structured_output")
        if not isinstance(data, dict):
            data = _extract_json(str(msg.get("result") or ""))
        usage = msg.get("usage") or {}
        return Completion(
            data=data, model=model, backend="claude_cli", cost_usd=msg.get("total_cost_usd"),
            input_tokens=int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
                         + int(usage.get("cache_creation_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            duration_ms=int((time.time() - started) * 1000), raw_text=str(msg.get("result") or "")[:2000])

    def _anthropic(self, prompt: str, schema: dict, model: str, system: str | None, timeout: int) -> Completion:
        started = time.time()
        tool = {"name": "emit", "description": "Return the structured answer.", "input_schema": schema}
        body: dict[str, Any] = {"model": model, "max_tokens": 4000, "tools": [tool],
                                "tool_choice": {"type": "tool", "name": "emit"},
                                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode(), method="POST")
        req.add_header("content-type", "application/json"); req.add_header("x-api-key", self.anthropic_key)
        req.add_header("anthropic-version", "2023-06-01")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode())
        data = next((b["input"] for b in resp.get("content", []) if b.get("type") == "tool_use"), None)
        if not isinstance(data, dict):
            raise LLMError("anthropic_api returned no tool_use block")
        u = resp.get("usage") or {}
        return Completion(data=data, model=model, backend="anthropic_api", cost_usd=None,
                          input_tokens=int(u.get("input_tokens") or 0), output_tokens=int(u.get("output_tokens") or 0),
                          duration_ms=int((time.time() - started) * 1000))
