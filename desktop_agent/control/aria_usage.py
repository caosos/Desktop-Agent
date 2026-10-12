"""What an Ask Aria exchange actually used, stated once and persisted on the reply: model, route, rounds and tokens.

Owner-facing wording follows the one-budget directive (da-8708eb5ec7): a reply says only HOW it was served
(`subscription-included`, `paid-api` or `none`). Subscription-included usage carries no dollar figure for the owner; the
Claude CLI's token-equivalent number is kept as `estimate_usd` for technical evidence and is always labelled
`estimate; not billed`. A paid API call carries `paid_usd`, the provider-reported amount (billing receipts are the
authority, see paid_budget.py)."""
from __future__ import annotations

ESTIMATE_LABEL = "estimate; not billed"
BILLING_TEXT = {"subscription-included": "included in your subscription", "paid-api": "paid API (under your daily maximum)", "none": "no model call"}


def billing_kind(backend: str | None) -> str:
    if backend is None:
        return "none"
    return "subscription-included" if backend == "claude_cli" else "paid-api"


def usage_record(*, backend: str | None, model: str | None, reported_usd: float | None, rounds: int, tokens_in: int, tokens_out: int,
                 duration_ms: int, retries: int = 0, models_billed: dict | None = None) -> dict:
    kind = billing_kind(backend)
    rec = {"backend": backend, "model": model, "billing": kind,
           "estimate_usd": round(reported_usd, 5) if kind == "subscription-included" and reported_usd is not None else None,
           "estimate_label": ESTIMATE_LABEL if kind == "subscription-included" else None,
           "paid_usd": round(reported_usd, 5) if kind == "paid-api" and reported_usd is not None else None,
           "rounds": rounds, "retries": retries, "tokens": {"in": tokens_in, "out": tokens_out}, "duration_ms": duration_ms}
    if models_billed:
        rec["models_billed"] = models_billed          # technical evidence: per model usd (estimate) and token classes, as the backend reported them
    return rec


def merge_models(total: dict, add: dict | None) -> dict:
    """Sum the per-model breakdowns of several rounds of one exchange."""
    for name, v in (add or {}).items():
        t = total.setdefault(name, {"usd": 0.0, "in": 0, "out": 0, "cache_read": 0, "cache_create": 0})
        for k in t:
            t[k] = round(t[k] + v.get(k, 0), 5) if k == "usd" else t[k] + int(v.get(k, 0))
    return total


def route_note(backend: str | None, available: bool) -> str:
    """Header text for the drawer: how replies are served. No dollar figure and no promise of one."""
    if not available or backend is None:
        return ""
    if backend == "claude_cli":
        return "Claude CLI, included in your subscription"
    return "paid API, only within your Daily maximum paid API spend"
