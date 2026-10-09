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
import copy
import json
import os
import re
import time
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


class LLM:
    def __init__(self, cfg: RuntimeConfig, backend: str | None = None, claude_bin: str = "claude"):
        self.cfg = cfg
        self.claude_bin = claude_bin
        self._forced = backend
        self.backend = backend or self._pick_backend()

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
                "providers": {n: self.cfg.provider_status(n) for n in self.cfg.providers}}

    async def complete(self, prompt: str, schema: dict, model_class: str = "cloud_cheap",
                       system: str | None = None, timeout: int = 180) -> Completion:
        self.refresh()
        model = self.model_for(model_class)
        if self.backend == "openai_api":
            return await asyncio.to_thread(self._openai, prompt, schema, model, system, timeout)
        if self.backend == "anthropic_api":
            return await asyncio.to_thread(self._anthropic, prompt, schema, model, system, timeout)
        return await self._claude_cli(prompt, schema, model, system, timeout)

    # ---- backends -----------------------------------------------------------
    def _openai(self, prompt: str, schema: dict, model: str, system: str | None, timeout: int) -> Completion:
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

    def _anthropic(self, prompt: str, schema: dict, model: str, system: str | None, timeout: int) -> Completion:
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
                "--model", model, "--max-turns", "1", "--tools", "", "--no-session-persistence",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if system:
            argv += ["--append-system-prompt", system]
        started = time.time()
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}   # subscription path, no keys
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env | {"DESKTOP_AGENT_LLM": "1"}, limit=16 * 1024 * 1024)
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


def pilot_dry_run(cfg: RuntimeConfig, provider: str = "openai") -> dict:
    """Offline proof of the pilot path: what backend each class would select with the gate off
    (today) and on (after approval), what each call would be priced at (known or UNKNOWN), where
    it would be logged, and the rollback. Makes no network call and spends nothing."""
    st = cfg.provider_status(provider)
    backend_now = LLM(cfg).backend
    cfg_on = copy.deepcopy(cfg)
    cfg_on.providers[provider]["enabled"] = True
    on = LLM(cfg_on)
    classes = {}
    for cls in ("cloud_cheap", "cloud_strong", "cloud_max"):
        model = on.model_for(cls)
        classes[cls] = {"model": model, "price_known": cfg.price(model, 1, 1) is not None,
                        "cost_accounting": "USD from pricing table" if cfg.price(model, 1, 1) is not None else "tokens recorded, cost UNKNOWN (never estimated)"}
    return {
        "provider": provider, "key_present": st["key_present"], "key_file_mode": st["key_file_mode"], "configured": st["configured"],
        "enabled": st["enabled"], "backend_today": backend_now, "backend_if_enabled": on.backend,
        "routing_if_enabled": classes,
        "no_spend_checks": {"gate": "providers.<p>.enabled must be true", "key": "0600 file or env, control plane only",
                            "budgets": {"hourly_cap_usd": cfg.scheduler.hourly_cap_usd, "daily_cap_usd": cfg.scheduler.daily_cap_usd,
                                        "per_task_default_usd": cfg.default_budget_usd},
                            "provider_side": "owner sets an enforced hard spend limit in the provider console; the platform cannot stop in-flight provider metering"},
        "logging": {"per_call": "store.costs row (model, usd or NULL, tokens) + receipt on the planner/Aria action", "state": "/v0/state.budgets and .llm", "panel": "Cost / usage card: known vs unknown-cost usage, model backend"},
        "rollback": {"step": f"set providers.{provider}.enabled: false in config/runtime.yaml and restart desktop-agent.service (or remove the key file)",
                     "effect": "backend falls back to the next available (claude_cli subscription) on the very next call; no restart needed if the key file is removed"},
        "network_calls_made_by_this_check": 0,
    }


def list_openai_models(cfg: RuntimeConfig, timeout: int = 30) -> list[str]:
    """Free verification call: the model ids this key can use (GET /v1/models, no charge)."""
    key = cfg.provider_key("openai")
    if not key:
        raise LLMError("openai: no key")
    req = urllib.request.Request(OPENAI_MODELS_URL)
    req.add_header("authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raise LLMError(f"openai HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}") from None
    return sorted(m.get("id", "") for m in data.get("data", []))
