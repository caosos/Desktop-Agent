"""What an Ask Aria exchange actually used, stated once and persisted on the reply: model, backend, rounds, tokens,
cost and what kind of cost it is. Three kinds are never mixed: `token-equivalent` (the Claude CLI on the subscription
reports a dollar-shaped figure computed from tokens; it is not an invoice), `metered` (a priced API backend; this
is billed), and `unknown` (no price for that model, or the backend reported none)."""
from __future__ import annotations

BASIS_TEXT = {
    "token-equivalent": "token-equivalent from the Claude CLI on your subscription, not an invoice",
    "metered": "metered API cost at the configured price",
    "unknown": "cost unknown (no price reported)",
    "none": "no model call",
}


def cost_basis(backend: str | None, cost_usd: float | None) -> str:
    if backend is None:
        return "none"
    if cost_usd is None:
        return "unknown"
    return "token-equivalent" if backend == "claude_cli" else "metered"


def usage_record(*, backend: str | None, model: str | None, cost_usd: float | None, rounds: int, tokens_in: int, tokens_out: int,
                 duration_ms: int, retries: int = 0) -> dict:
    return {"backend": backend, "model": model, "cost_usd": None if cost_usd is None else round(cost_usd, 5), "cost_basis": cost_basis(backend, cost_usd),
            "rounds": rounds, "retries": retries, "tokens": {"in": tokens_in, "out": tokens_out}, "duration_ms": duration_ms}


def status_cost_note(backend: str | None, available: bool) -> str:
    """Header text for the drawer: what backend and what kind of cost, without promising an amount (the amount is
    whatever the exchange reports; tool-free structured calls were measured at 0.1–12 cents depending on context)."""
    if not available or backend is None:
        return ""
    if backend == "claude_cli":
        return "Claude CLI on your subscription; each reply shows its token-equivalent cost (not an invoice)"
    return "metered API; each reply shows its cost at the configured price, or UNKNOWN if the model has no price"
