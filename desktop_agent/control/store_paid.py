"""Store mixin: the paid-API ledger (owner directive da-8708eb5ec7). One row per paid provider call or provider billing
receipt, keyed by `call_id`, so a restart or a replay can never reset or double-count a day's paid spend. Subscription-
included, local and cached work never appears here (those are recorded as usage in `costs`, not as paid spend)."""
from __future__ import annotations

import time

COUNTED = ("reserved", "settled")        # statuses that count against the daily maximum


class PaidLedgerMixin:
    def paid_spent(self, day: str) -> float:
        with self._lock:
            marks = ",".join("?" * len(COUNTED))
            return float(self._db.execute(f"SELECT COALESCE(SUM(usd),0) s FROM paid_ledger WHERE day=? AND status IN ({marks})", (day, *COUNTED)).fetchone()["s"])

    def paid_reserve(self, call_id: str, day: str, provider: str, model: str, usd: float, maximum: float, purpose: str) -> tuple[bool, float, bool]:
        """Atomically hold `usd` for one call. (granted, spent_before, replay). The same call_id again is a replay and
        changes nothing. The check and the insert happen under one lock, so concurrent calls cannot jointly exceed the maximum."""
        with self._lock:
            row = self._db.execute("SELECT status FROM paid_ledger WHERE call_id=?", (call_id,)).fetchone()
            spent = self.paid_spent(day)
            if row:
                return row["status"] in COUNTED, spent, True
            if spent + usd > maximum + 1e-9:
                return False, spent, False
            self._db.execute("INSERT INTO paid_ledger(call_id, ts, day, provider, model, usd, basis, status, purpose) VALUES (?,?,?,?,?,?,?,?,?)",
                             (call_id, time.time(), day, provider, model, usd, "reserve", "reserved", purpose[:120]))
            self._db.commit()
            return True, spent, False

    def paid_settle(self, call_id: str, usd: float | None, basis: str = "provider_reported") -> None:
        """Replace the hold by what the provider reported; usd=None keeps the hold (the price is unknown: stay conservative)."""
        with self._lock:
            if usd is None:
                self._db.execute("UPDATE paid_ledger SET status='settled' WHERE call_id=? AND status='reserved'", (call_id,))
            else:
                self._db.execute("UPDATE paid_ledger SET usd=?, basis=?, status='settled' WHERE call_id=? AND status IN ('reserved','settled')", (usd, basis, call_id))
            self._db.commit()

    def paid_billing(self, call_id: str, day: str, provider: str, usd: float, covers: list[str], purpose: str) -> bool:
        """Record a verified provider billing line; rows it covers stop counting (superseded), so nothing is counted twice."""
        with self._lock:
            if self._db.execute("SELECT 1 FROM paid_ledger WHERE call_id=?", (call_id,)).fetchone():
                return False
            for c in covers:
                self._db.execute("UPDATE paid_ledger SET status='superseded' WHERE call_id=?", (c,))
            self._db.execute("INSERT INTO paid_ledger(call_id, ts, day, provider, model, usd, basis, status, purpose) VALUES (?,?,?,?,?,?,?,?,?)",
                             (call_id, time.time(), day, provider, None, usd, "billing_receipt", "settled", purpose[:120]))
            self._db.commit()
            return True

    def paid_rows(self, day: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM paid_ledger WHERE day=? ORDER BY ts", (day,)).fetchall()]

    def paid_holds(self, call_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT status FROM paid_ledger WHERE call_id=?", (call_id,)).fetchone()
        return bool(row and row["status"] in COUNTED)
