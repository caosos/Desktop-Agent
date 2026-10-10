"""Included Claude allowance (subscription rate-limit windows) as a visible, truthful number.

Sources, in the order they are trusted (freshest observation wins; percentages are never summed —
the account is shared, every session sees the same windows):
  * ``claude_code_statusline`` — ONE designated interactive session's status-line command
    (``scripts/claude_statusline.py``) drops the ``rate_limits`` block it receives into
    ``<data_dir>/allowance.json``; the watchdog ingests it on its normal 120 s tick.
  * ``worker_run`` — the ``rate_limit_event`` a headless worker run reports (``launcher.py``).

Rules: a window that is absent, or whose ``resets_at`` has passed, is UNKNOWN (never 0 %, never the
last number); an observation older than STALE_AFTER_SEC is shown as stale; one CONTROL receipt is
written per meaningful change (≥ 1 point on any window, a new reset time, or a new source), not per
tick. No model call, no API endpoint, no daemon: the file is written by the session itself.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .events import Actor, Event, EventType as ET, Provenance
from .store import Store

WINDOWS = ("five_hour", "seven_day")
STALE_AFTER_SEC = 30 * 60
ALLOWANCE_FILE = "allowance.json"


def read_file(path: Path) -> dict | None:
    """The raw drop from the status-line script, or None when absent/unreadable (no error, no claim)."""
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("rate_limits"), dict) else None


def from_statusline(drop: dict) -> dict:
    """Normalize a status-line drop into the shared ``quota`` record (same shape as the worker-run one)."""
    rl = drop.get("rate_limits") or {}
    windows = {}
    for name in WINDOWS:
        w = rl.get(name)
        if not isinstance(w, dict) or w.get("used_percentage") is None:
            continue
        pct = float(w["used_percentage"])
        windows[name] = {"utilization": round(pct / 100.0, 4), "used_percentage": pct, "resets_at": w.get("resets_at")}
    resets = [w["resets_at"] for w in windows.values() if w.get("resets_at")]
    return {"provider": "claude_subscription", "source": "claude_code_statusline",
            "source_detail": {"session_id": drop.get("session_id"), "session_name": drop.get("session_name"),
                              "cwd": drop.get("cwd"), "version": drop.get("version")},
            "account_alias": drop.get("account_alias") or "shared Claude subscription (this host's sessions)",
            "windows": windows, "resets_at": min(resets) if resets else None, "status": None,
            "seen_at": float(drop.get("observed_at") or 0)}


def meaningful_change(old: dict | None, new: dict) -> bool:
    if not old:
        return True
    if old.get("source") != new.get("source"):
        return True
    ow, nw = old.get("windows") or {}, new.get("windows") or {}
    if set(ow) != set(nw):
        return True
    for k, v in nw.items():
        o = ow[k]
        if o.get("resets_at") != v.get("resets_at"):
            return True
        if abs(float(o.get("utilization") or 0) - float(v.get("utilization") or 0)) >= 0.01:
            return True
    return False


def ingest(store: Store, path: Path, now: float | None = None) -> dict:
    """Watchdog tick: read the drop; store it when it is newer than what we hold; one receipt per
    meaningful change. Returns what happened (for the tick report), never raises."""
    now = now or time.time()
    drop = read_file(path)
    if not drop:
        return {"source": "claude_code_statusline", "found": False}
    new = from_statusline(drop)
    if not new["windows"]:
        return {"source": "claude_code_statusline", "found": True, "stored": False, "reason": "no windows in the drop"}
    cur = store.get_kv("quota") or {}
    if float(cur.get("seen_at") or 0) >= new["seen_at"]:
        return {"source": "claude_code_statusline", "found": True, "stored": False, "reason": "not newer than the held observation"}
    changed = meaningful_change(cur, new)
    store.set_kv("quota", new)
    if changed:
        store.append_event(Event(type=ET.CONTROL.value, task_id=None,
                                 payload={"project": "desktop_agent", "action": "allowance_observed", "source": new["source"],
                                          "windows": {k: {"used_percentage": v["used_percentage"], "resets_at": v["resets_at"]} for k, v in new["windows"].items()},
                                          "observed_at": new["seen_at"], "account_alias": new["account_alias"]},
                                 provenance=Provenance(actor=Actor.CONTROL.value, source="watchdog:allowance",
                                                       evidence=[str(path), f"session={new['source_detail'].get('session_name') or new['source_detail'].get('session_id')}",
                                                                 f"observed_at={new['seen_at']}"])))
    return {"source": "claude_code_statusline", "found": True, "stored": True, "receipt": changed}


def view(q: dict | None, now: float | None = None) -> dict | None:
    """What the panel shows: every window as a number or UNKNOWN, with its reason, plus freshness."""
    if not q:
        return None
    now = now or time.time()
    seen = float(q.get("seen_at") or 0)
    age = now - seen if seen else None
    fresh = "fresh" if age is not None and age < STALE_AFTER_SEC else "stale" if age is not None else "unknown"
    rows = {}
    for name in WINDOWS:
        w = (q.get("windows") or {}).get(name)
        if not w or w.get("utilization") is None:
            rows[name] = {"state": "UNKNOWN", "reason": "not reported by the source", "used_percentage": None, "resets_at": None}
            continue
        resets = w.get("resets_at")
        if resets and float(resets) <= now:
            rows[name] = {"state": "UNKNOWN", "reason": "window reset since the last observation", "used_percentage": None, "resets_at": float(resets)}
            continue
        pct = w.get("used_percentage")
        if pct is None:
            pct = round(float(w["utilization"]) * 100, 1)
        rows[name] = {"state": "KNOWN", "used_percentage": pct, "resets_at": resets, "reason": None}
    return {**q, "view": {"windows": rows, "freshness": fresh, "age_sec": round(age) if age is not None else None,
                          "source": q.get("source") or ("worker_run" if q.get("task_id") else "unknown"),
                          "account_alias": q.get("account_alias") or "shared Claude subscription (this host's sessions)",
                          "observed_at": seen or None}}
