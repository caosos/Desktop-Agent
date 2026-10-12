#!/usr/bin/env python3
"""Unattended-day acceptance report (BUILD_CHECKLIST Phase 3: "a full day runs with Michael touching only the inbox").

Reads the control-plane store (SQLite, read-only; stdlib only; no model, no network) for a window and prints what
actually happened, from receipts and events — never from self-reports:

  tasks        created / verified (DONE) / FAILED / BLOCKED / ARCHIVED, retries (contract.supersedes), per project
  instructions ingested / delivered / acknowledged, ACK latency per item and the share within the 300 s target,
               undelivered or unacknowledged flags (BLOCKED+NEED events), misroutes (delivered to a session that is
               ANOTHER project's pinned coordinator), stand-ins (the pinned name was not live and exactly one eligible
               session in the project directory took delivery — the designed fallback, reported, not a defect)
  owner        decisions asked / answered, owner actions on the inbox (transfer/deny/defer), i.e. how often the owner
               was actually needed
  activity     longest gap with no event at all (the platform's idle time), restarts seen (recovery receipts)
  paid         actual paid API spend from the paid ledger (the only authority; subscription/local work is never spend)
  estimates    token-equivalent model usage by model, labelled "estimate; not billed" (technical evidence only)

Usage: unattended_report.py [--hours N] [--since ISO] [--json] [--db PATH]
Exit status is 0 when the report could be produced; it never judges pass/fail by itself — the checklist does.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA = Path.home() / ".local/share/desktop-agent"
TARGET_ACK_SEC = 300


def utc(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if ts else "-"


def load_events(db, since: float) -> list[dict]:
    out = []
    for (js,) in db.execute("SELECT json FROM events WHERE ts >= ? ORDER BY seq", (since,)):
        try:
            out.append(json.loads(js))
        except json.JSONDecodeError:
            continue
    return out


def report(db_path: Path, since: float, pins: dict[str, str]) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    now = time.time()
    ev = load_events(db, since)
    rep: dict = {"window": {"since": utc(since), "until": utc(now), "hours": round((now - since) / 3600, 1)}, "db": str(db_path)}

    # ---- tasks ------------------------------------------------------------------------------------------------
    tasks = []
    for row in db.execute("SELECT task_id, project, status, contract_json, created_at, updated_at FROM tasks WHERE updated_at >= ? OR created_at >= ?", (since, since)):
        c = json.loads(row[3] or "{}")
        tasks.append({"task_id": row[0], "project": row[1], "status": row[2], "supersedes": c.get("supersedes"), "created_at": row[4], "updated_at": row[5]})
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    rep["tasks"] = {"touched": len(tasks), "by_status": by_status, "retries": sum(1 for t in tasks if t["supersedes"]),
                    "by_project": {p: sum(1 for t in tasks if t["project"] == p) for p in sorted({t["project"] for t in tasks})},
                    "verified_ids": [t["task_id"] for t in tasks if t["status"] == "DONE"]}

    # ---- instructions ------------------------------------------------------------------------------------------
    status_ev = [e for e in ev if e.get("type") == "CONTROL" and (e.get("payload") or {}).get("action") == "intake_status"]
    per_item: dict[str, dict] = {}
    for e in status_ev:
        # the event payload carries status/note; the item id is in the payload or in the provenance evidence
        iid = (e.get("payload") or {}).get("item_id")
        if not iid:
            for x in (e.get("provenance") or {}).get("evidence") or []:
                if isinstance(x, str) and x.startswith("da-"):
                    iid = x; break
        if not iid:
            continue
        st = (e["payload"] or {}).get("status")
        d = per_item.setdefault(iid, {})
        d.setdefault(st, e["ts"])                      # first time each status was reached
        d.setdefault("_notes", {})[st] = (e["payload"] or {}).get("note", "")
    # fall back to the intake table for ids/status/times when events lack ids
    rows = db.execute("SELECT item_id, project, status, note, created_at, updated_at, flagged FROM intake_items WHERE created_at >= ? OR updated_at >= ?", (since, since)).fetchall()
    items = [{"item_id": r[0], "project": r[1], "status": r[2], "note": r[3] or "", "created_at": r[4], "updated_at": r[5], "flagged": r[6]} for r in rows]
    ack_lat = []
    misroutes, standins = [], []
    pinned_by = {v: k for k, v in pins.items()}                     # session name -> the project that pinned it
    for it in items:
        pi = per_item.get(it["item_id"], {})
        d, a = pi.get("DELIVERED"), pi.get("ACKNOWLEDGED") or pi.get("WORKING") or pi.get("DONE")
        if d and a and a >= d:
            ack_lat.append((it["item_id"], a - d))
        pin = pins.get(it["project"])
        note = (pi.get("_notes", {}).get("DELIVERED") or it["note"]) if it["status"] in ("DELIVERED", "ACKNOWLEDGED", "WORKING", "DONE", "BLOCKED") else ""
        if "peer message to session " in note:
            delivered_to = note.split("peer message to session ", 1)[1].split(" ", 1)[0]
            owner = pinned_by.get(delivered_to)
            if owner and owner != it["project"]:
                misroutes.append({"item_id": it["item_id"], "project": it["project"], "delivered_to": delivered_to, "pinned_by": owner, "note": note[:160]})
            elif pin and delivered_to != pin:
                standins.append({"item_id": it["item_id"], "project": it["project"], "delivered_to": delivered_to, "pinned": pin, "note": note[:160]})
    within = sum(1 for _, s in ack_lat if s <= TARGET_ACK_SEC)
    need_ev = [e for e in ev if e.get("type") == "BLOCKED" and "need" in (e.get("payload") or {})]
    rep["instructions"] = {
        "touched": len(items), "by_status": {s: sum(1 for i in items if i["status"] == s) for s in sorted({i["status"] for i in items})},
        "by_project": {p: sum(1 for i in items if i["project"] == p) for p in sorted({i["project"] for i in items})},
        "ack_latency": {"measured": len(ack_lat), "within_300s": within, "share_within_300s": round(within / len(ack_lat), 2) if ack_lat else None,
                        "median_sec": round(sorted(s for _, s in ack_lat)[len(ack_lat) // 2]) if ack_lat else None,
                        "max_sec": round(max(s for _, s in ack_lat)) if ack_lat else None},
        "flagged_unacknowledged": [i["item_id"] for i in items if i["flagged"] and i["status"] in ("RECEIVED", "DELIVERED")],
        "need_receipts": [{"url": (e.get("provenance") or {}).get("evidence", [None])[0], "project": e["payload"].get("project"),
                           "class": e["payload"]["need"].get("classification"), "at": utc(e["ts"])} for e in need_ev],
        "misroutes_to_other_pinned_session": misroutes,
        "standin_deliveries": standins,
    }

    # ---- owner involvement --------------------------------------------------------------------------------------
    dec = db.execute("SELECT decision_id, asked_at, answered_at, project FROM decisions WHERE asked_at >= ? OR answered_at >= ?", (since, since)).fetchall()
    owner_actions = [e for e in ev if e.get("type") == "CONTROL" and (e.get("payload") or {}).get("action") in ("owner_transfer", "owner_deny", "owner_defer") or
                     (e.get("type") == "CONTROL" and str((e.get("payload") or {}).get("action", "")).startswith("owner_"))]
    rep["owner"] = {"decisions_asked": sum(1 for d in dec if d[1] and d[1] >= since), "decisions_answered": sum(1 for d in dec if d[2] and d[2] >= since),
                    "inbox_actions": len(owner_actions),
                    "human_receipts": db.execute("SELECT COUNT(*) FROM receipts WHERE ts >= ? AND json LIKE '%\"actor\": \"human\"%'", (since,)).fetchone()[0]}

    # ---- activity ---------------------------------------------------------------------------------------------
    ts = sorted(e["ts"] for e in ev if e.get("ts"))
    gaps = [(ts[i + 1] - ts[i], ts[i]) for i in range(len(ts) - 1)]
    longest = max(gaps, default=(0, None))
    recov = [p for p in sorted(glob.glob(str(DATA / "recovery" / "*.json"))) if os.path.getmtime(p) >= since]
    rep["activity"] = {"events": len(ev), "first": utc(ts[0]) if ts else "-", "last": utc(ts[-1]) if ts else "-",
                       "longest_gap_min": round(longest[0] / 60, 1), "longest_gap_started": utc(longest[1]),
                       "recovery_receipts": len(recov), "restarts_seen": max(0, len(recov) // 2)}

    # ---- paid spend and usage estimates (owner directive da-8708eb5ec7: the ledger is the only authority) -------
    paid = db.execute("SELECT COUNT(*), COALESCE(SUM(usd), 0) FROM paid_ledger WHERE ts >= ? AND status IN ('reserved', 'settled')", (since,)).fetchone()
    rep["paid"] = {"calls": paid[0], "usd": round(paid[1], 4), "authority": "paid_ledger"}
    cost = db.execute("SELECT model, SUM(usd), COUNT(*), SUM(CASE WHEN usd IS NULL THEN 1 ELSE 0 END) FROM costs WHERE ts >= ? GROUP BY model", (since,)).fetchall()
    rep["estimates"] = {"label": "estimate; not billed",
                        "by_model": {m: {"usd": round(u or 0, 4), "calls": n, "unpriced_calls": k} for m, u, n, k in cost},
                        "usd_total": round(sum((u or 0) for _, u, _, _ in cost), 4)}
    return rep


def pins_from_config(repo: Path) -> dict[str, str]:
    out = {}
    for f in sorted((repo / "config" / "projects").glob("*.yaml")):
        name, pin, in_coord = None, None, False
        for line in f.read_text().splitlines():
            s = line.strip()
            if s.startswith("name:") and name is None:
                name = s.split(":", 1)[1].strip().strip('"')
            if s.startswith("coordinator:"):
                in_coord = True
            elif in_coord and s.startswith("session_name:"):
                pin = s.split(":", 1)[1].split("#")[0].strip(); in_coord = False
        if name and pin:
            out[name] = pin
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--since", help="ISO time (UTC) overriding --hours")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(DATA / "control.sqlite"))
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since.replace("Z", "+00:00")).timestamp() if a.since else time.time() - a.hours * 3600
    repo = Path(__file__).resolve().parents[1]
    rep = report(Path(a.db), since, pins_from_config(repo))
    if a.json:
        print(json.dumps(rep, indent=1, default=str)); return 0
    w, t, i, o, ac = rep["window"], rep["tasks"], rep["instructions"], rep["owner"], rep["activity"]
    print(f"UNATTENDED REPORT  {w['since']} → {w['until']}  ({w['hours']} h)")
    print(f"tasks: touched {t['touched']}  {t['by_status']}  retries {t['retries']}  by project {t['by_project']}")
    print(f"  verified: {', '.join(t['verified_ids']) or '-'}")
    print(f"instructions: touched {i['touched']}  {i['by_status']}  by project {i['by_project']}")
    al = i["ack_latency"]
    print(f"  ACK latency: measured {al['measured']}, within 300 s {al['within_300s']} (share {al['share_within_300s']}), median {al['median_sec']} s, max {al['max_sec']} s")
    print(f"  flagged unacknowledged now: {i['flagged_unacknowledged'] or '-'}")
    print(f"  NEED receipts: {len(i['need_receipts'])}" + (f"  e.g. {i['need_receipts'][-1]}" if i["need_receipts"] else ""))
    print(f"  misroutes to another project's pinned session: {len(i['misroutes_to_other_pinned_session'])}" + (f"  {i['misroutes_to_other_pinned_session'][0]}" if i["misroutes_to_other_pinned_session"] else ""))
    print(f"  stand-in deliveries (pinned name not live, one eligible session took it): {len(i['standin_deliveries'])}" + (f"  e.g. {i['standin_deliveries'][-1]['item_id']} -> {i['standin_deliveries'][-1]['delivered_to']}" if i["standin_deliveries"] else ""))
    print(f"owner: decisions asked {o['decisions_asked']} answered {o['decisions_answered']}; inbox actions {o['inbox_actions']}; human receipts {o['human_receipts']}")
    print(f"activity: {ac['events']} events {ac['first']} → {ac['last']}; longest silent gap {ac['longest_gap_min']} min from {ac['longest_gap_started']}; recovery receipts {ac['recovery_receipts']}")
    p, e = rep["paid"], rep["estimates"]
    print(f"paid API spend (ledger, the only authority): ${p['usd']} over {p['calls']} calls")
    print(f"model usage, {e['label']}: {e['by_model']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
