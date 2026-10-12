"""The ETA-driven receipt schema and the notification filter (owner operating addendum, 2026-10-11).

Every ACK / WORKING / BLOCKED receipt a coordinator posts carries these fields as `key: value` lines:

    project / task / responsible / stage / evidence / eta / eta_basis / next_check_at / acceptance / blocker

`eta` and `next_check_at` are ISO-8601 times that carry the America/Chicago offset in force at that instant;
`eta_basis` names the scope, tests, browser verification or host condition the ETA comes from (a generic phrase is
rejected); `blocker` is `NONE` or the exact blocker. `validate` returns the problems, never a guess.
"""
from __future__ import annotations

import re
from datetime import datetime

from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
REQUIRED = ("project", "task", "responsible", "stage", "evidence", "eta", "eta_basis", "next_check_at", "acceptance", "blocker")
OUTCOMES = ("WORKING", "DONE", "BLOCKED")                      # the only three a check may publish
_LINE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?\**(" + "|".join(REQUIRED) + r")\**[ \t]*:[ \t]*\**[ \t]*(.+?)[ \t]*$", re.I | re.M)
_GENERIC = {"", "n/a", "na", "tbd", "unknown", "asap", "soon", "later", "none", "estimate", "a while"}


def parse_receipt(text: str) -> dict[str, str]:
    """The schema lines of a comment (last occurrence of a key wins); {} when it carries none."""
    return {m.group(1).lower(): m.group(2).strip().strip("*").strip() for m in _LINE.finditer(text or "")}


def parse_time(value: str) -> datetime | None:
    v = re.sub(r"\s*\(.*\)\s*$", "", (value or "").strip()).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def chicago(ts: float) -> str:
    return datetime.fromtimestamp(ts, CHICAGO).strftime("%Y-%m-%d %H:%M:%S %Z")


def validate(fields: dict[str, str], receipt_ts: float) -> tuple[list[str], dict | None]:
    """(problems, normalized). normalized carries `eta_ts`, `next_check_ts` (epoch) and their Chicago renderings."""
    fields = dict(fields)
    if str(fields.get("blocker", "")).upper().startswith("NEED_OWNER") and str(fields.get("eta", "")).strip().upper().startswith("UNKNOWN") and fields.get("next_check_at"):
        fields["eta_stated"] = fields["eta"]                  # what the coordinator actually said stays on the record (truth: stated vs substituted)
        fields["eta"] = fields["next_check_at"]               # waiting on the owner: no honest ETA exists; the check is parked, not timed (checks_reconcile.py)
    problems = [f"missing: {k}" for k in REQUIRED if not (fields.get(k) or "").strip()]
    times: dict[str, datetime] = {}
    for k in ("eta", "next_check_at"):
        if not fields.get(k):
            continue
        dt = parse_time(fields[k])
        if dt is None:
            problems.append(f"{k}: not an ISO-8601 time with a UTC offset ({fields[k][:40]!r})")
        elif dt.utcoffset() != dt.astimezone(CHICAGO).utcoffset():
            problems.append(f"{k}: the offset must be America/Chicago's at that instant ({dt.astimezone(CHICAGO).strftime('%z')[:3]}:{dt.astimezone(CHICAGO).strftime('%z')[3:]} expected)")
        else:
            times[k] = dt
    if "next_check_at" in times and times["next_check_at"].timestamp() <= receipt_ts:
        problems.append("next_check_at: not after the time of this receipt")
    basis = (fields.get("eta_basis") or "").strip().lower()
    if fields.get("eta_basis") is not None and (basis in _GENERIC or len(basis) < 15):
        problems.append("eta_basis: must state the scope, tests, browser verification or host condition the ETA comes from")
    if problems:
        return problems, None
    norm = dict(fields)
    norm.update(eta_ts=times["eta"].timestamp(), next_check_ts=times["next_check_at"].timestamp(),
                eta_chicago=chicago(times["eta"].timestamp()), next_check_chicago=chicago(times["next_check_at"].timestamp()))
    return [], norm


# ---- who is told: only the listed exceptions, never routine healthy progress ----------------------------------------------
NOTIFY_KINDS = {
    "done_needs_owner_acceptance": "a DONE that needs Michael's acceptance",
    "need_blocked": "a NEED or BLOCKED",
    "missed_ack": "a missed ACK",
    "missed_eta": "a missed ETA",
    "missed_check": "a missed next_check_at",
    "failed_test": "a failed test", "failed_delivery": "a failed delivery",
    "failed_deployment": "a failed deployment", "failed_recovery": "a failed recovery",
    "owner_decision": "a genuine owner decision",
    "cost_or_authority": "a material cost or authority boundary",
}
QUIET_KINDS = {
    "routine_progress": "routine healthy progress",
    "build_authority_already_granted": "ordinary build authority already granted",
    "unchanged_state": "unchanged state",
    "done_no_owner_acceptance": "a DONE that needs no owner acceptance",
}


def notify_decision(kind: str) -> tuple[bool, str]:
    if kind in NOTIFY_KINDS:
        return True, f"notify: {NOTIFY_KINDS[kind]}"
    if kind in QUIET_KINDS:
        return False, f"no notification: {QUIET_KINDS[kind]}"
    return False, f"no notification: {kind!r} is not one of the listed exceptions (recorded only)"
