"""Offline provider checks split out of llm.py: the pilot dry run (no network, no spend) and the free model listing."""
from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request

from .config import RuntimeConfig


def pilot_dry_run(cfg: RuntimeConfig, provider: str = "openai") -> dict:
    """Offline proof of the pilot path: what backend each class would select with the gate off
    (today) and on (after approval), what each call would be priced at (known or UNKNOWN), where
    it would be logged, and the rollback. Makes no network call and spends nothing."""
    from .llm import LLM
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
                            "budget": {"daily_max_paid_usd_default": 0.0, "effect": "no paid call is granted unless the owner sets a Daily maximum paid API spend above $0",
                                       "per_task_default_usd": cfg.default_budget_usd},
                            "provider_side": "owner sets an enforced hard spend limit in the provider console; the platform cannot stop in-flight provider metering"},
        "logging": {"per_call": "store.costs row (model, usd or NULL, tokens) + receipt on the planner/Aria action", "state": "/v0/state.budgets and .llm", "panel": "Daily maximum paid API spend bar; technical evidence card"},
        "rollback": {"step": f"set providers.{provider}.enabled: false in config/runtime.yaml and restart desktop-agent.service (or remove the key file)",
                     "effect": "backend falls back to the next available (claude_cli subscription) on the very next call; no restart needed if the key file is removed"},
        "network_calls_made_by_this_check": 0,
    }


def list_openai_models(cfg: RuntimeConfig, timeout: int = 30) -> list[str]:
    """Free verification call: the model ids this key can use (GET /v1/models, no charge)."""
    from .llm import LLMError
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
