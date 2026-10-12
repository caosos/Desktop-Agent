"""The one hard daily budget (owner directive da-8708eb5ec7, 2026-10-11 20:57 America/Chicago).

There is exactly one owner setting: **Daily maximum paid API spend**. Its default is $0, hardcoded below; only the owner
changes it (panel or `POST /v0/paid-budget`, each change receipted). It bounds *actual paid API spend* only. Claude CLI
and Codex usage included in a subscription, local work and cached work are not paid spend: they are never added to this
ledger and never called spend, cost, charged or a cap anywhere an owner reads. Their token-equivalent dollar figures are
internal diagnostics, shown only behind "technical evidence" and labelled `estimate; not billed`.

Every paid provider call goes through `LLM.complete`, which asks this gate first. A call is granted only if the day's
paid total plus a conservative hold still fits under the maximum; the check and the hold are one atomic ledger write
keyed by call id, so concurrent calls, retries, restarts and replays cannot exceed the maximum or count a call twice.
Provider billing receipts are the only authority for the final amount (`record_billing`); a provider-reported usage
figure is shown as such until a billing receipt covers it. A refused call spends nothing, runs on the subscription or
local route instead, and leaves one NEED receipt per day, provider and purpose.
"""
from __future__ import annotations

import time
from zoneinfo import ZoneInfo
from datetime import datetime

from .events import Actor, Event, EventType as ET, Provenance
from .receipts import VERIFIED, write_receipt

CHICAGO = ZoneInfo("America/Chicago")
DEFAULT_MAX_USD = 0.0          # hardcoded: no paid API authority unless the owner sets one
HOLD_USD = 0.10                # conservative hold per paid call until the provider reports (or a billing receipt covers) the real amount
KV_MAX = "paid_budget:max_usd"


class PaidBudget:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def day(self) -> str:
        return datetime.fromtimestamp(self.clock(), CHICAGO).strftime("%Y-%m-%d")

    def maximum(self) -> float:
        v = (self.store.get_kv(KV_MAX) or {}).get("usd")
        return DEFAULT_MAX_USD if v is None else max(0.0, float(v))

    def set_maximum(self, usd: float, source: str = "panel") -> dict:
        if usd != usd or usd < 0 or usd > 10000:
            raise ValueError("the daily maximum must be a number from 0 to 10000")
        before = self.maximum()
        self.store.set_kv(KV_MAX, {"usd": round(float(usd), 2), "at": self.clock(), "source": source})
        return write_receipt(self.store, subject_type="budget", subject_id="paid_api_daily_max",
                             claim=f"owner set the Daily maximum paid API spend from ${before:.2f} to ${usd:.2f}", actor=Actor.HUMAN.value, source=source,
                             result_label=VERIFIED, evidence=[f"before={before:.2f}", f"after={usd:.2f}", f"day={self.day()}"],
                             before_state={"daily_max_usd": before}, after_state={"daily_max_usd": round(float(usd), 2)})

    # ---- reading ------------------------------------------------------------
    def summary(self) -> dict:
        day, mx = self.day(), self.maximum()
        rows = self.store.paid_rows(day)
        paid = self.store.paid_spent(day)
        verified = sum(r["usd"] for r in rows if r["basis"] == "billing_receipt" and r["status"] == "settled")
        blocked = [k for k in (self.store.get_kv(f"paid_need_index:{day}") or {}).get("keys", [])]
        return {"day": day, "daily_max_paid_usd": mx, "paid_today_usd": round(paid, 4), "paid_today_verified_usd": round(verified, 4),
                "remaining_usd": round(max(0.0, mx - paid), 4), "blocked_attempts_today": sum((self.store.get_kv(k) or {}).get("count", 0) for k in blocked),
                "paid_calls_today": sum(1 for r in rows if r["basis"] != "billing_receipt" and r["status"] in ("reserved", "settled")),
                "note": "Subscription-included, local and cached work is not paid spend and is not counted here."}

    def permits(self) -> tuple[bool, str]:
        """Side-effect-free: could one more paid call be granted right now?"""
        mx, paid = self.maximum(), self.store.paid_spent(self.day())
        if mx <= 0:
            return False, f"the Daily maximum paid API spend is ${mx:.2f}"
        if paid + HOLD_USD > mx + 1e-9:
            return False, f"${paid:.2f} paid today leaves less than the ${HOLD_USD:.2f} one call needs under the ${mx:.2f} maximum"
        return True, ""

    # ---- the gate -----------------------------------------------------------
    def authorize(self, call_id: str, provider: str, model: str, purpose: str) -> dict:
        ok, spent, replay = self.store.paid_reserve(call_id, self.day(), provider, model, HOLD_USD, self.maximum(), purpose)
        return {"ok": ok, "replay": replay, "paid_today_usd": round(spent, 4),
                "reason": "" if ok else f"${spent:.2f} paid today under a ${self.maximum():.2f} daily maximum"}

    def holds(self, call_id: str) -> bool:
        return self.store.paid_holds(call_id)

    def settle(self, call_id: str, usd: float | None) -> None:
        self.store.paid_settle(call_id, usd)

    def record_billing(self, provider: str, usd: float, reference: str, day: str | None = None, covers: list[str] | None = None) -> dict | None:
        """A provider billing receipt/line (the only authority for actual paid spend). Idempotent by (provider, reference)."""
        day = day or self.day()
        if not self.store.paid_billing(f"bill:{provider}:{reference}", day, provider, float(usd), covers or [], f"billing {reference}"):
            return None
        return write_receipt(self.store, subject_type="budget", subject_id="paid_api_ledger",
                             claim=f"provider billing receipt recorded: {provider} ${usd:.4f} for {day} ({reference})", actor=Actor.CONTROL.value, source="paid_budget",
                             result_label=VERIFIED, evidence=[f"provider={provider}", f"reference={reference}", f"covers={len(covers or [])} ledger rows"])

    def note_block(self, provider: str, model: str, purpose: str, reason: str, fallback: str | None) -> dict | None:
        """One NEED receipt per day, provider and purpose for a refused paid call. It notifies the owner only when it blocks
        necessary work (no subscription/local route took over); otherwise it is a record, not an interruption."""
        day = self.day()
        key = f"paid_need:{day}:{provider}:{purpose[:60]}"
        known = self.store.get_kv(key)
        self.store.set_kv(key, {"count": (known or {}).get("count", 0) + 1, "at": self.clock()})
        if known:
            return None
        idx = self.store.get_kv(f"paid_need_index:{day}") or {"keys": []}
        self.store.set_kv(f"paid_need_index:{day}", {"keys": idx["keys"] + [key]})
        mx = self.maximum()
        blocks = fallback is None
        claim = (f"NEED_OWNER ({'BLOCKS necessary work' if blocks else 'recorded only; the ' + ('subscription route' if fallback == 'claude_cli' else fallback) + ' continued the work'}): a paid {provider} call for \"{purpose}\" "
                 f"was refused, {reason}. No charge was made. Smallest owner decision: raise the Daily maximum paid API spend from ${mx:.2f} to at least ${max(mx, 0) + HOLD_USD:.2f}.")
        rec = write_receipt(self.store, subject_type="budget", subject_id=day, claim=claim[:400], actor=Actor.CONTROL.value, source="paid_budget",
                            result_label=VERIFIED, evidence=[f"provider={provider}", f"model={model}", f"purpose={purpose}", f"daily_max={mx:.2f}", f"day={day}"],
                            after_state={"charged_usd": 0.0, "blocks_work": blocks, "fallback": fallback, "classification": "NEED_OWNER"})
        self.store.append_event(Event(type=ET.CONTROL.value, task_id=None, payload={"action": "paid_call_blocked", "provider": provider, "purpose": purpose,
                                                                                   "daily_max_usd": mx, "blocks_work": blocks, "charged_usd": 0.0},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source="paid_budget", evidence=[f"provider={provider}", f"daily_max={mx:.2f}"])))
        return rec
