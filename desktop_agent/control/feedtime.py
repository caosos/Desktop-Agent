"""One rule for "how current is a project's own feed": the feed's OWN observation time (`data.last_check`)
decides, not the moment this control plane last fetched it. Fetching a stale document every two minutes
never makes it current. Missing or unparseable observation time = UNKNOWN, never "fresh". An ack file
observed recently contradicts a STOPPED/no-session claim (something in that account is processing the inbox),
so such a claim is shown as UNKNOWN with both facts and never becomes a restart instruction.
"""
from __future__ import annotations

import time

FEED_TTL_SEC = 15 * 60      # an observation older than this proves nothing about "now"
FUTURE_SLACK_SEC = 5 * 60   # beyond this, a future observation time is a clock problem, not evidence


def parse_iso(s) -> float | None:
    """'2026-10-10T06:36:34Z' / '+00:00' / naive (treated as UTC) / date-only → epoch seconds, else None."""
    if not isinstance(s, str) or len(s) < 10:
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1]
    try:
        if "T" in t:
            base, _, rest = t.partition("T")
            tz = 0.0
            for sign in ("+", "-"):
                if sign in rest:
                    rest, _, off = rest.partition(sign)
                    hh, _, mm = off.partition(":")
                    tz = (int(hh) * 3600 + int(mm or 0) * 60) * (1 if sign == "+" else -1)
                    break
            rest = rest.split(".")[0]
            parts = [int(x) for x in rest.split(":")]
            while len(parts) < 3:
                parts.append(0)
            y, mo, d = (int(x) for x in base.split("-"))
            if not (1 <= mo <= 12 and 1 <= d <= 31 and 0 <= parts[0] < 24 and 0 <= parts[1] < 60 and 0 <= parts[2] < 61):
                return None
            return time.mktime((y, mo, d, parts[0], parts[1], parts[2], 0, 0, 0)) - time.timezone - tz
        y, mo, d = (int(x) for x in t.split("-"))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None
        return time.mktime((y, mo, d, 0, 0, 0, 0, 0, 0)) - time.timezone
    except (ValueError, OverflowError):
        return None


def ago(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec // 3600}h" if sec < 86400 else f"{sec // 86400}d"


def feed_freshness(feed: dict | None, now: float | None = None, ttl: int = FEED_TTL_SEC, last_ack: float | None = None) -> dict:
    """status: fresh | stale | unknown_time | missing. `fresh` is True only when the feed's own observation
    time is valid and within ttl (and the fetch is too). `ack_recent`: an ack file was observed within ttl —
    evidence that the inbox is being processed, whatever the feed says about its session."""
    now = now or time.time()
    if not feed:
        return {"status": "missing", "fresh": False, "fetch_age": None, "source_time": None, "source_age": None,
                "label": "no feed record", "ack_recent": False, "last_ack": last_ack}
    fetch_age = now - float(feed.get("fetched_at") or 0)
    src = (feed.get("data") or {}).get("last_check")
    ts = parse_iso(src)
    ack_recent = bool(last_ack and (now - float(last_ack)) < ttl)
    if ts is None:
        return {"status": "unknown_time", "fresh": False, "fetch_age": fetch_age, "source_time": src if isinstance(src, str) else None, "source_age": None,
                "label": f"feed carries no valid observation time ({src!r}; read {ago(fetch_age)} ago) — its state cannot be dated",
                "ack_recent": ack_recent, "last_ack": last_ack}
    source_age = now - ts
    if source_age < -FUTURE_SLACK_SEC:
        return {"status": "unknown_time", "fresh": False, "fetch_age": fetch_age, "source_time": src, "source_age": source_age,
                "label": f"feed observation time {src} is {ago(-source_age)} in the future (clock skew?; read {ago(fetch_age)} ago) — its state cannot be dated",
                "ack_recent": ack_recent, "last_ack": last_ack}
    if source_age < ttl and fetch_age < ttl:
        status, label = "fresh", f"observed {src} ({ago(source_age)} ago), read {ago(fetch_age)} ago"
    else:
        status, label = "stale", f"last observation {src} is {ago(source_age)} old (TTL {ttl // 60} min; read {ago(fetch_age)} ago) — not current"
    return {"status": status, "fresh": status == "fresh", "fetch_age": fetch_age, "source_time": src, "source_age": source_age,
            "label": label, "ack_recent": ack_recent, "last_ack": last_ack}
