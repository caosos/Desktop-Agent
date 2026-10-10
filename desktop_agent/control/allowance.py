"""Included Claude allowance (subscription rate-limit windows) as a visible, truthful number.

Sources, freshest observation wins; percentages are never summed — the account is shared, every
session sees the same windows:
  * ``claude_code_statusline`` — the REGISTERED coordinator session's status-line command
    (``scripts/claude_statusline.py``) drops the ``rate_limits`` block it receives into
    ``<data_dir>/allowance.json``. Which session may write is decided here, by identity: the control
    plane registers the resolved Desktop-Agent coordinator's session id in ``<data_dir>/allowance_writer.json``
    (``register_writer``); the script writes only when its own session id matches, and ``ingest`` rejects
    a drop from any other session id. Home-directory equality is not a credential.
  * ``worker_run`` — the ``rate_limit_event`` a headless worker run reports (``launcher.py``).

Rules: a window that is absent, or whose ``resets_at`` has passed, is UNKNOWN (never 0 %, never the
last number); a newer sample with no windows makes the current value UNKNOWN and keeps the last known
value only as ``historical``; an observation older than STALE_AFTER_SEC is shown as stale; one CONTROL
receipt per meaningful change measured against the LAST RECEIPTED value (≥ 1 point on any window, a new
reset time, a new source, or known ↔ unknown), so sub-threshold drift cannot hide a cumulative change.
No model call, no API endpoint, no daemon: the file is written by the session itself.
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
WRITER_FILE = "allowance_writer.json"
RECEIPTED_KV = "quota_receipted"


# ---- writer registration (identity, not directory) ------------------------------------------------
def register_writer(path: Path, session: dict | None, note: str = "") -> dict:
    """Record which session may write the allowance drop: the resolved coordinator (by its session id).
    No session → an explicit empty registration (nobody may write). Rewritten only when it changes."""
    reg = ({"session_id": session.get("session_id"), "session_name": session.get("name"), "pid": session.get("pid"),
            "resolved_by": session.get("resolved_by"), "note": note} if session and session.get("session_id")
           else {"session_id": None, "session_name": None, "pid": None, "resolved_by": None, "note": note or "no eligible coordinator session"})
    cur = read_writer(path)
    if cur and all(cur.get(k) == reg[k] for k in ("session_id", "pid")):
        return cur
    reg["registered_at"] = time.time()
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(reg))
    os.replace(tmp, path)
    return reg


def read_writer(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) else None


# ---- the drop --------------------------------------------------------------------------------------
def read_file(path: Path) -> dict | None:
    """The raw drop from the status-line script, or None when absent/unreadable (no error, no claim)."""
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) and "rate_limits" in d else None


def from_statusline(drop: dict) -> dict:
    """Normalize a status-line drop into the shared ``quota`` record (same shape as the worker-run one).
    An empty ``rate_limits`` yields no windows (= UNKNOWN), deliberately."""
    rl = drop.get("rate_limits") if isinstance(drop.get("rate_limits"), dict) else {}
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
        return True                                   # known ↔ unknown, or a window appeared/vanished
    for k, v in nw.items():
        o = ow[k]
        if o.get("resets_at") != v.get("resets_at"):
            return True
        if abs(float(o.get("utilization") or 0) - float(v.get("utilization") or 0)) >= 0.01:
            return True
    return False


def _historical(cur: dict) -> dict | None:
    """The last KNOWN windows, to keep beside an UNKNOWN current value (explicitly historical)."""
    if cur.get("windows"):
        return {"windows": cur["windows"], "seen_at": cur.get("seen_at"), "source": cur.get("source")}
    return cur.get("historical")


def ingest(store: Store, path: Path, now: float | None = None, writer_path: Path | None = None) -> dict:
    """Watchdog tick: read the drop; accept it only from the registered writer; store it when newer than
    what we hold; one receipt per meaningful change against the last receipted value. Never raises."""
    now = now or time.time()
    drop = read_file(path)
    if not drop:
        return {"source": "claude_code_statusline", "found": False}
    writer = read_writer(writer_path) if writer_path else None
    if writer_path is not None and (not writer or not writer.get("session_id") or writer["session_id"] != drop.get("session_id")):
        return {"source": "claude_code_statusline", "found": True, "stored": False,
                "reason": f"drop from session {str(drop.get('session_id'))[:8]} is not the registered writer "
                          f"({str((writer or {}).get('session_id'))[:8] or 'none'}); ignored"}
    new = from_statusline(drop)
    cur = store.get_kv("quota") or {}
    if float(cur.get("seen_at") or 0) >= new["seen_at"]:
        return {"source": "claude_code_statusline", "found": True, "stored": False, "reason": "not newer than the held observation"}
    if not new["windows"]:
        hist = _historical(cur)
        if hist:
            new["historical"] = hist
        new["reason"] = "latest sample from the registered session reported no rate-limit windows"
    elif cur.get("historical") and not cur.get("windows"):
        pass                                           # known again: historical no longer needed
    last_receipted = store.get_kv(RECEIPTED_KV)
    changed = meaningful_change(last_receipted, new)
    store.set_kv("quota", new)
    if changed:
        store.set_kv(RECEIPTED_KV, {"source": new["source"], "windows": new["windows"], "seen_at": new["seen_at"]})
        store.append_event(Event(type=ET.CONTROL.value, task_id=None,
                                 payload={"project": "desktop_agent", "action": "allowance_observed", "source": new["source"],
                                          "windows": {k: {"used_percentage": v["used_percentage"], "resets_at": v["resets_at"]} for k, v in new["windows"].items()},
                                          "unknown": not new["windows"], "observed_at": new["seen_at"], "account_alias": new["account_alias"]},
                                 provenance=Provenance(actor=Actor.CONTROL.value, source="watchdog:allowance",
                                                       evidence=[str(path), f"session={new['source_detail'].get('session_id')}",
                                                                 f"observed_at={new['seen_at']}"])))
    return {"source": "claude_code_statusline", "found": True, "stored": True, "receipt": changed}


def view(q: dict | None, now: float | None = None) -> dict | None:
    """What the panel shows: every window as a number or UNKNOWN with its reason, freshness, and the
    last known value as explicitly historical when the current one is unknown."""
    if not q:
        return None
    now = now or time.time()
    seen = float(q.get("seen_at") or 0)
    age = now - seen if seen else None
    fresh = "fresh" if age is not None and age < STALE_AFTER_SEC else "stale" if age is not None else "unknown"
    rows = {}
    hist = q.get("historical") or {}
    for name in WINDOWS:
        w = (q.get("windows") or {}).get(name)
        if not w or w.get("utilization") is None:
            h = (hist.get("windows") or {}).get(name)
            rows[name] = {"state": "UNKNOWN", "reason": q.get("reason") or "not reported by the source", "used_percentage": None, "resets_at": None,
                          "historical": ({"used_percentage": h.get("used_percentage", round(float(h["utilization"]) * 100, 1)), "seen_at": hist.get("seen_at")}
                                         if h and h.get("utilization") is not None else None)}
            continue
        resets = w.get("resets_at")
        if resets and float(resets) <= now:
            rows[name] = {"state": "UNKNOWN", "reason": "window reset since the last observation", "used_percentage": None, "resets_at": float(resets), "historical": None}
            continue
        pct = w.get("used_percentage")
        if pct is None:
            pct = round(float(w["utilization"]) * 100, 1)
        rows[name] = {"state": "KNOWN", "used_percentage": pct, "resets_at": resets, "reason": None, "historical": None}
    return {**q, "view": {"windows": rows, "freshness": fresh, "age_sec": round(age) if age is not None else None,
                          "source": q.get("source") or ("worker_run" if q.get("task_id") else "unknown"),
                          "account_alias": q.get("account_alias") or "shared Claude subscription (this host's sessions)",
                          "observed_at": seen or None,
                          "writer": {k: (q.get("source_detail") or {}).get(k) for k in ("session_id", "session_name")}}}
