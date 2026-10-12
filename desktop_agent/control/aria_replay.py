"""Idempotency for Ask Aria sends: find the stored exchange of a client_id so a repeated delivery returns it instead of
running again. `stored_exchange` returns the result dict for a finished exchange, False when the owner turn was recorded
but no reply was (an interrupted exchange: never re-executed, because its actions may already have run), None when the
id is new."""
from __future__ import annotations


def stored_exchange(history: list[dict], client_id: str):
    for i, row in enumerate(history):
        if row.get("role") == "owner" and row.get("client_id") == client_id:
            for nxt in history[i + 1:]:
                if nxt.get("role") == "owner":
                    break
                if nxt.get("role") == "aria" and nxt.get("usage") is not None:
                    u = nxt["usage"]
                    return {"reply": nxt.get("text") or "(no reply)", "actions": nxt.get("actions") or [], "kind": nxt.get("kind"), "model": u.get("model"),
                            "cost_usd": u.get("cost_usd"), "tokens": u.get("tokens"), "duration_ms": u.get("duration_ms"), "usage": u}
            return False
    return None


def interrupted_result() -> dict:
    from .aria_usage import usage_record
    u = usage_record(backend=None, model=None, cost_usd=0.0, rounds=0, tokens_in=0, tokens_out=0, duration_ms=0)
    return {"reply": "This request was received earlier but was interrupted before it finished (for example by a service restart). It was not run again; "
                     "check the transcript, or send it as a new message.", "actions": [], "kind": "error", "model": None, "cost_usd": 0.0,
            "tokens": {"in": 0, "out": 0}, "duration_ms": 0, "usage": u}
