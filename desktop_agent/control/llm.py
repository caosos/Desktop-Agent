"""Structured model calls for the control plane itself (planner, Aria).

Provider-neutral: `complete(prompt, schema, model_class)` → Completion.
Backends, tried in the configured order, first available wins:
  - openai_api     : Chat Completions with a JSON-schema response format; key from a 0600 file or env.
  - anthropic_api  : Messages API with a forced tool call carrying the schema; key from file or env.
  - claude_cli     : `claude -p --json-schema` with no tools (subscription; reports its own USD).
Costs: API backends price the reported token usage with the configured price table;
without a price the cost is recorded as unknown, never estimated. Keys live only in
this process and are never passed to workers.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import json
import os
import re
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import RuntimeConfig

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


@dataclass
class Completion:
    data: dict
    model: str
    backend: str
    cost_usd: float | None          # None = unknown (no figure reported or no price configured)
    input_tokens: int
    output_tokens: int
    duration_ms: int
    raw_text: str = ""
    model_usage: dict | None = None  # per-model breakdown the backend itself reported (Claude CLI `modelUsage`): which model was billed what

    @property
    def cost_known(self) -> bool:
        return self.cost_usd is not None


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


def _post_json(url: str, body: dict, headers: dict[str, str], timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("content-type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raise LLMError(f"{url.split('/')[2]} HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise LLMError(f"{url.split('/')[2]} unreachable: {exc}") from None


_PAID_ENV_EXACT = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"}


def subscription_env(environ) -> dict:
    """Environment for any Claude CLI the control plane starts: no API key, auth token, gateway or cloud-billing switch (subscription login only)."""
    return {k: v for k, v in environ.items() if not k.endswith("_API_KEY") and k not in _PAID_ENV_EXACT}


class LLM:
    def __init__(self, cfg: RuntimeConfig, backend: str | None = None, claude_bin: str = "claude"):
        self.cfg = cfg
        self.claude_bin = claude_bin
        self._forced = backend
        self.backend = backend or self._pick_backend()
        self._calls: dict[tuple[str, str], int] = {}        # (backend, day) → API calls made by this process
        self.gate = None                                    # PaidBudget; without one no paid backend is ever selected (fail closed)

    def refresh(self) -> str:
        """Re-pick the backend (a key file may have appeared since start; no restart needed)."""
        if not self._forced:
            self.backend = self._pick_backend()
        return self.backend

    # ---- selection ----------------------------------------------------------
    def available(self, backend: str) -> bool:
        if backend == "claude_cli":
            return True
        name = backend.replace("_api", "")
        st = self.cfg.provider_status(name)
        return st["key_present"] and st["configured"] and st["enabled"]

    def _pick_backend(self) -> str:
        for b in self.cfg.llm_backend_order:
            if self.available(b):
                return b
        return "claude_cli"

    def model_for(self, model_class: str, backend: str | None = None) -> str:
        b = backend or self.backend
        if b in ("openai_api", "anthropic_api"):
            models = (self.cfg.providers.get(b.replace("_api", "")) or {}).get("models") or {}
            m = models.get(model_class) or models.get("cloud_cheap")
            if m:
                return m
        return self.cfg.models.get(model_class) or self.cfg.models["cloud_cheap"]

    def status(self) -> dict:
        return {"backend": self.backend, "order": list(self.cfg.llm_backend_order),
                "providers": {n: self.cfg.provider_status(n) for n in self.cfg.providers},
                "backend_by_class": {c: self.backend_for(c) for c in ("cloud_cheap", "cloud_strong", "cloud_max")},
                "pilot_usage": self.pilot_usage(), "paid_budget": self.gate.summary() if self.gate else None}

    # ---- pilot scope: a provider may be limited to some classes and to N calls per day ------
    def backend_for(self, model_class: str) -> str:
        """The backend for this call: the first available one whose provider config admits the class
        (`classes:`) and has not used up its `pilot_max_calls_per_day`; claude_cli otherwise."""
        self.refresh()
        today = time.strftime("%Y-%m-%d")
        for b in self.cfg.llm_backend_order:
            if not self.available(b):
                continue
            if b == "claude_cli":
                return b
            pv = self.cfg.providers.get(b.replace("_api", "")) or {}
            if pv.get("classes") and model_class not in pv["classes"]:
                continue
            cap = pv.get("pilot_max_calls_per_day")
            if cap is not None and self._calls.get((b, today), 0) >= int(cap):
                continue
            if not self._budget_ok():
                continue                                   # the daily maximum paid API spend: skip every paid route
            return b
        return "claude_cli"

    def _budget_ok(self) -> bool:
        return bool(self.gate and self.gate.permits()[0])

    def _skipped_for_budget(self, model_class: str) -> list[str]:
        """Paid backends that would have served this call but for the daily maximum (no side effects)."""
        if self._budget_ok():
            return []
        out = []
        for b in self.cfg.llm_backend_order:
            if b == "claude_cli" or not self.available(b):
                continue
            pv = self.cfg.providers.get(b.replace("_api", "")) or {}
            if pv.get("classes") and model_class not in pv["classes"]:
                continue
            out.append(b)
        return out

    def pilot_usage(self) -> dict:
        today = time.strftime("%Y-%m-%d")
        return {b.replace("_api", ""): {"calls_today": n, "cap": (self.cfg.providers.get(b.replace("_api", "")) or {}).get("pilot_max_calls_per_day"),
                                        "classes": (self.cfg.providers.get(b.replace("_api", "")) or {}).get("classes")}
                for (b, d), n in self._calls.items() if d == today}

    async def complete(self, prompt: str, schema: dict, model_class: str = "cloud_cheap",
                       system: str | None = None, timeout: int = 180, purpose: str = "control-plane model call") -> Completion:
        skipped = self._skipped_for_budget(model_class)
        backend = self.backend_for(model_class)
        model = self.model_for(model_class, backend)
        call_id = None
        if backend in ("openai_api", "anthropic_api"):
            provider = backend.replace("_api", "")
            call_id = f"llm-{uuid.uuid4().hex}"
            grant = self.gate.authorize(call_id, provider, model, purpose) if self.gate else {"ok": False, "reason": "no paid-spend ledger attached"}
            if not grant["ok"]:                            # lost a race at the boundary: nothing was held, nothing is charged
                skipped.append(backend)
                backend, call_id = "claude_cli", None
                model = self.model_for(model_class, backend)
            else:
                key = (backend, time.strftime("%Y-%m-%d"))
                self._calls[key] = self._calls.get(key, 0) + 1
        for b in skipped:                                  # one NEED receipt per day, provider and purpose; it notifies only if no other route ran
            self.gate.note_block(b.replace("_api", ""), self.model_for(model_class, b), purpose,
                                 self.gate.permits()[1] or "the daily maximum was reached by a concurrent call", backend)
        if call_id:
            fn = self._openai if backend == "openai_api" else self._anthropic
            comp = await asyncio.to_thread(fn, prompt, schema, model, system, timeout, call_id)
            self.gate.settle(call_id, comp.cost_usd)       # the provider-reported amount replaces the hold (unknown price: the hold stays)
            return comp
        return await self._claude_cli(prompt, schema, model, system, timeout)

    # ---- backends -----------------------------------------------------------
    def _held(self, call_id: str) -> None:
        if not (self.gate and self.gate.holds(call_id)):
            raise LLMError("paid API call refused: no daily-maximum hold exists for this call")

    def _openai(self, prompt: str, schema: dict, model: str, system: str | None, timeout: int, call_id: str = "") -> Completion:
        self._held(call_id)
        key = self.cfg.provider_key("openai")
        if not key:
            raise LLMError("openai_api: no key")
        started = time.time()
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body: dict[str, Any] = {"model": model, "messages": messages,
                                "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}}
        resp = _post_json(OPENAI_URL, body, {"authorization": "Bearer " + key}, timeout)
        text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        data = _extract_json(text)
        u = resp.get("usage") or {}
        itok, otok = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        return Completion(data=data, model=resp.get("model") or model, backend="openai_api",
                          cost_usd=self.cfg.price(resp.get("model") or model, itok, otok) if self.cfg.pricing else self.cfg.price(model, itok, otok),
                          input_tokens=itok, output_tokens=otok, duration_ms=int((time.time() - started) * 1000), raw_text=text[:2000])

    def _anthropic(self, prompt: str, schema: dict, model: str, system: str | None, timeout: int, call_id: str = "") -> Completion:
        self._held(call_id)
        key = self.cfg.provider_key("anthropic")
        if not key:
            raise LLMError("anthropic_api: no key")
        started = time.time()
        tool = {"name": "emit", "description": "Return the structured answer.", "input_schema": schema}
        body: dict[str, Any] = {"model": model, "max_tokens": 4000, "tools": [tool],
                                "tool_choice": {"type": "tool", "name": "emit"},
                                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        resp = _post_json(ANTHROPIC_URL, body, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        data = next((b["input"] for b in resp.get("content", []) if b.get("type") == "tool_use"), None)
        if not isinstance(data, dict):
            raise LLMError("anthropic_api returned no tool_use block")
        u = resp.get("usage") or {}
        itok, otok = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
        return Completion(data=data, model=model, backend="anthropic_api", cost_usd=self.cfg.price(model, itok, otok),
                          input_tokens=itok, output_tokens=otok, duration_ms=int((time.time() - started) * 1000))

    async def _claude_cli(self, prompt: str, schema: dict, model: str, system: str | None, timeout: int) -> Completion:
        argv = [self.claude_bin, "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema),
                "--model", model, "--max-turns", "3", "--tools", "", "--no-session-persistence",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if system:
            argv += ["--append-system-prompt", system]
        started = time.time()
        env = subscription_env(os.environ)                  # subscription path only: no key, token or gateway
        # Neutral, empty working directory: Claude Code auto-loads CLAUDE.md from the cwd, and this repo's imports the
        # 218 KB PROJECT_STATE.md into every call (measured 96 480 vs 4 366 cache tokens for an identical trivial call).
        # Aria and the planner are grounded through their own actions and prompts, not through that file.
        neutral = Path(self.cfg.data_dir) / "llm-cwd"
        neutral.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env | {"DESKTOP_AGENT_LLM": "1"}, limit=16 * 1024 * 1024, cwd=str(neutral))
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise LLMError("claude_cli call timed out")
        text = out.decode(errors="replace")
        try:
            msg = json.loads(text[text.index("{"):]) if "{" in text else {}
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude_cli returned non-JSON: {text[:200]!r} ({exc})")
        if msg.get("is_error") and not msg.get("structured_output"):
            detail = msg.get("result") or msg.get("error") or msg.get("errors") or msg.get("subtype") or {k: msg[k] for k in ("subtype", "num_turns", "stop_reason") if k in msg}
            raise LLMError(f"claude_cli error: {str(detail)[:300]}")
        data = msg.get("structured_output")
        if not isinstance(data, dict):
            data = _extract_json(str(msg.get("result") or ""))
        usage = msg.get("usage") or {}
        return Completion(
            data=data, model=model, backend="claude_cli", cost_usd=msg.get("total_cost_usd"),
            input_tokens=int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
                         + int(usage.get("cache_creation_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            duration_ms=int((time.time() - started) * 1000), raw_text=str(msg.get("result") or "")[:2000],
            model_usage=_slim_model_usage(msg.get("modelUsage")))


def _slim_model_usage(raw) -> dict | None:
    """The CLI's own per-model figures, reduced to numbers: cost and the four token classes per model name."""
    if not isinstance(raw, dict) or not raw:
        return None
    out = {}
    for name, v in raw.items():
        if isinstance(v, dict):
            out[str(name)] = {"usd": round(float(v.get("costUSD") or 0), 5), "in": int(v.get("inputTokens") or 0), "out": int(v.get("outputTokens") or 0),
                              "cache_read": int(v.get("cacheReadInputTokens") or 0), "cache_create": int(v.get("cacheCreationInputTokens") or 0)}
    return out or None


from .llm_pilot import list_openai_models, pilot_dry_run   # noqa: E402,F401  (kept importable from here)
