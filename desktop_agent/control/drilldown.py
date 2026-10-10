"""Project drilldown: one in-place view per project built only from sources that already exist.

  mission        the project's own descriptor (`mission:`), pointing at its start_here
  coordinator    `Service.coordinator_state` (live session registry, state script, liaison) plus, for a project
                 with a feed, the feed's own session entry (quoted as the project's self-report)
  workers        `WorkerMonitor.snapshot` (live processes, feed, task records)
  roster         declared roles from the project's own status file on its coordinator branch (free GitHub read,
                 cached by the watchdog) merged with LIVE evidence per lane: a host process for that lane now,
                 the feed's session entry for the persistent lane, or "not running". Declared ≠ live, always labelled.
  tasks          this control plane's READY / RUNNING / DONE (with merge state) / BLOCKED tasks for the project
  instructions   Shared Inbox items for the project with age and an honest next action (no cross-user wake claimed)
  freshness      when each source was last read
  primary_action one plain sentence derived from alerts, unacknowledged items, finished PRs
"""
from __future__ import annotations

import base64
import re
import time

from .ackfile import next_action as ack_next_action
from .feedtime import feed_freshness
from .intake import gh_api

from .feedtime import FEED_TTL_SEC   # a feed observation older than this proves nothing about "now" (one rule, feedtime.py)


def parse_active_work_table(text: str) -> list[dict]:
    """Rows of Agent 01's ACTIVE_WORK table: | **01** Coordinator (persistent) | WORKING | last | READY | blocker |"""
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\|\s*\*\*(\d{2})\*\*\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|", line)
        if m:
            lane, role, state, last, ready, blocker = m.groups()
            rows.append({"lane": lane, "role": role.strip(), "declared_state": state.strip(), "last_result": last.strip(),
                         "ready": ready.strip(), "blocker": blocker.strip()})
    synced = re.search(r"\*\*Last synced:\*\*\s*([^\n]+)", text)
    if synced and rows:
        for r in rows:
            r["declared_as_of"] = synced.group(1).strip()[:160]
    return rows


class Drilldown:
    def __init__(self, service):
        self.svc = service

    # ---- watchdog tick: read declared rosters (free) --------------------------------------
    async def refresh_remote(self) -> dict:
        report = {}
        for name, pkg in self.svc.projects.items():
            ro = getattr(pkg, "roster", None) or {}
            if not ro or not pkg.github_repo:
                continue
            try:
                content = await gh_api(f"repos/{pkg.github_repo}/contents/{ro['path']}?ref={ro['branch']}")
                body = base64.b64decode(content.get("content") or "").decode(errors="replace") if content.get("encoding") == "base64" else ""
                rows = parse_active_work_table(body) if ro.get("kind") == "active_work_table" else []
                self.svc.store.set_kv(f"roster:{name}", {"fetched_at": time.time(), "source": f"{ro['branch']}:{ro['path']}", "url": content.get("html_url", ""),
                                                        "sha": (content.get("sha") or "")[:12], "rows": rows})
                report[name] = f"{len(rows)} declared rows"
            except Exception as exc:
                report[name] = f"roster error: {exc}"[:120]
        return report

    # ---- the view ---------------------------------------------------------------------------
    def build(self, name: str) -> dict | None:
        pkg = self.svc.projects.get(name)
        if not pkg:
            return None
        now = time.time()
        st = self.svc.state()
        proj = next(p for p in st["projects"] if p["name"] == name)
        coord, workers = proj["coordinator"], proj["workers"]
        tasks = [t for t in st["tasks"] if t["project"] == name]
        feed = self.svc.store.get_kv(f"feed:{name}") or {}
        fsess = ((feed.get("data") or {}).get("session") or {}) if feed else {}
        fstate = str(((feed.get("data") or {}).get("state") or "")).upper() if feed else ""
        roster_kv = self.svc.store.get_kv(f"roster:{name}") or {}
        live_by_lane: dict[str, list] = {}
        for w in workers.get("workers", []):
            live_by_lane.setdefault(str(w.get("lane") or ""), []).append(w)
        ro = getattr(pkg, "roster", None) or {}
        persistent = set(ro.get("persistent_lanes") or [])
        # the persistent coordinator's own process, if the project declares how to recognise it (another account is fine: read-only)
        coord_proc = None
        if ro.get("coordinator_process_pattern"):
            rx = re.compile(ro["coordinator_process_pattern"])
            coord_proc = next((p for p in sorted(self.svc.workers.procs_fn(), key=lambda p: p["started_at"]) if rx.search(p["cmd"])), None)
        # Evidence rules (owner finding da-bd13834bc7): absence of a match is never proof of "not running".
        ff = feed_freshness(feed, now, last_ack=(self.svc.store.coordinator_rows().get(name) or {}).get("last_ack"))
        feed_age, feed_fresh = ff["fetch_age"], ff["fresh"]
        other_account = coord.get("kind") == "liaison"
        roster = []
        for r in roster_kv.get("rows", []):
            live = live_by_lane.get(r["lane"], [])
            if live:
                actual, proof = "RUNNING", f"{', '.join(w['id'] for w in live)} — pid {live[0]['pid']} alive since {_ago(now - live[0]['started_at'])}"
            elif r["lane"] in persistent:
                st = str(fsess.get("status") or "").lower() if fsess else ""
                if coord_proc:
                    actual = "SESSION ALIVE" + (f" ({st})" if st else "")
                    proof = (f"coordinator process pid {coord_proc['pid']} ({coord_proc['user']}) alive on this host since {_ago(now - coord_proc['started_at'])}"
                             + (f"; its own feed says session {fsess.get('name', '?')} {st}" if st else "; its feed carries no session entry")
                             + "; a live process proves the session exists, not that it is working this minute")
                elif fsess and feed_fresh and st in ("closed", "exited", "stopped") and not ff["ack_recent"]:
                    actual, proof = "NOT RUNNING", f"its own feed reports session {fsess.get('name', '?')} {st} ({ff['label']})"
                elif not fsess and feed_fresh and fstate == "STOPPED" and not ff["ack_recent"]:
                    actual = "NOT RUNNING"
                    proof = (f"its own watchdog (same account) reports coordinator state STOPPED with no session ({ff['label']}); "
                             f"{len((feed.get('data') or {}).get('unacknowledged_messages') or [])} liaison message(s) it lists as unacknowledged — nobody is reading them until the session is started in its own account")
                elif feed_fresh and ff["ack_recent"] and (fstate == "STOPPED" or st in ("closed", "exited", "stopped")):
                    actual = "UNKNOWN (contradicted)"
                    proof = (f"its own watchdog says {fstate or st.upper()} ({ff['label']}), but an ack file from its account was observed {_ago(now - float(ff['last_ack']))} ago — "
                             "something there is processing the inbox; the STOPPED claim is not current evidence and no restart is implied")
                elif fsess and feed_fresh and st:
                    actual = "SESSION " + st.upper()
                    proof = f"per the project's own feed: session {fsess.get('name', '?')} {st} (pid {fsess.get('pid', '?')}; {ff['label']})"
                elif feed and ff["status"] == "unknown_time" and (st or fstate):
                    actual, proof = "UNKNOWN (undated feed)", f"its feed says {fstate or ('session ' + st)} but {ff['label']}"
                elif feed and ff["status"] == "stale" and (st or fstate):
                    actual, proof = "UNKNOWN (stale feed)", f"its feed last said {fstate or ('session ' + st)}; {ff['label']}"
                else:
                    actual = "NOT VERIFIED"
                    proof = ("no verified process match and no session record in its feed; " + ("the coordinator runs under a separate Linux account, so " if other_account else "")
                             + "this control plane cannot see whether it is working — not evidence that it is stopped")
            else:
                actual, proof = "NOT OBSERVED", f"no worker process for this lane observed on the host {('(' + _ago(now - (workers.get('verified_at') or now)) + ' ago) ') if workers.get('verified_at') else ''}— bounded workers run only while a task is in flight"
            roster.append({**r, "actual": actual, "proof": proof, "launched_by": "project dispatcher"})
        for w in workers.get("workers", []):
            if not any(w["id"] in x["proof"] for x in roster):
                roster.append({"lane": w.get("lane") or "", "role": "live worker (not in the declared table)" if roster_kv else "bounded worker", "declared_state": "",
                               "last_result": "", "ready": "", "blocker": "", "actual": w["status"], "proof": w.get("evidence", ""),
                               "launched_by": "this control plane" if w.get("source") == "control-plane task" else "project coordinator", "id": w["id"]})
        if coord.get("kind") == "claude_peer":
            roster.insert(0, {"lane": "coordinator", "role": "coordinator session", "declared_state": "", "last_result": "", "ready": "", "blocker": "",
                              "actual": ("SESSION " + str(coord.get("session_status", "")).upper()) if coord.get("connected") else "NOT RUNNING",
                              "proof": coord.get("detail", ""), "launched_by": "owner / coordinator"})
        # instructions for this project, newest first, with an honest next action
        items = [i for i in self.svc.store.list_intake() if i["project"] == name][-10:][::-1]
        instr = []
        for i in items:
            age = _age(i.get("posted_at") or "", i.get("updated_at"))
            if i["status"] in ("RECEIVED", "DELIVERED") and i.get("flagged"):
                nxt = ("awaiting Agent 01's ack file; it reads the liaison branch at its own sync — no cross-account wake exists" if i.get("kind") == "liaison"
                       else "delivered but not acknowledged; resend from the Shared inbox or check the coordinator session")
            elif i["status"] in ("WORKING", "ACKNOWLEDGED", "CLAIMED", "BLOCKED") and i.get("kind") == "liaison" and self.svc.store.get_kv("liaison_ack:" + i["item_id"]):
                nxt = ack_next_action(self.svc.store.get_kv("liaison_ack:" + i["item_id"]), i["status"])
            elif i["status"] in ("WORKING", "ACKNOWLEDGED", "CLAIMED"):
                nxt = "in the coordinator's hands; DONE arrives by its comment or ack file"
            elif i["status"] in ("POSTED", "SENT"):
                nxt = "posted; delivery pending"
            else:
                nxt = ""
            # separate facts from the item's own receipts: delivered / acknowledged / blocked / coordination-completed /
            # implementation result / verified live / DONE (issue route only). Never collapsed into one.
            facts = {"delivered_at": None, "acknowledged_at": None, "done_at": None, "evidence": [], "blocked_at": None, "coordination_completed_at": None}
            for rc in self.svc.store.receipts("instruction", i["item_id"]):
                claim, ts = str(rc.get("claim") or ""), rc.get("ts")
                head = claim.split(":")[0].strip().upper()
                if head in ("DELIVERED", "RECEIVED", "SENT") and not facts["delivered_at"]:
                    facts["delivered_at"] = ts
                if head in ("ACKNOWLEDGED", "WORKING", "CLAIMED") and not facts["acknowledged_at"]:
                    facts["acknowledged_at"] = ts
                if head == "DONE":
                    facts["done_at"] = ts
                if head == "BLOCKED" or head.startswith("ACKFILE BLOCKED"):
                    facts["blocked_at"] = ts
                if head.startswith("ACKFILE") and "coordination-completed" in claim and not facts["coordination_completed_at"]:
                    facts["coordination_completed_at"] = ts
                for ev in rc.get("evidence") or []:
                    if isinstance(ev, str) and ev.startswith("http") and ev not in facts["evidence"]:
                        facts["evidence"].append(ev)
            disp = self.svc.store.get_kv("liaison_ack:" + i["item_id"]) if i.get("kind") == "liaison" else None
            if disp and facts["coordination_completed_at"] is None and disp.get("category") == "coordination-completed":
                facts["coordination_completed_at"] = disp.get("observed_at")
            instr.append({"item_id": i["item_id"], "title": (i.get("title") or "")[:90], "status": i["status"], "age": age, "flagged": bool(i.get("flagged")),
                          "url": i.get("url", ""), "next": nxt, **facts, "evidence": facts["evidence"][-3:],
                          "disposition": ({k: disp.get(k) for k in ("stage", "category", "implementation", "live", "links", "not_proven", "followups", "sha", "url", "observed_at", "first_seen_at", "history", "stage_line")}
                                          if disp else None)})
        unacked = [x for x in instr if x["flagged"] and x["status"] in ("RECEIVED", "DELIVERED")]
        finished_open = [f for f in workers.get("finished", []) if "open" in f["status"]]
        amber = [a["text"] for a in workers.get("alerts", []) if a["level"] == "amber"]
        if amber:
            primary = amber[0]
        elif unacked:
            primary = f"{len(unacked)} instruction(s) not acknowledged (oldest {unacked[-1]['age']}): " + unacked[-1]["next"]
        elif finished_open:
            primary = f"merge {finished_open[0]['pr_url']} (verified)"
        elif workers.get("status") == "RUNNING":
            primary = "nothing needed; work is running"
        else:
            primary = "nothing needed right now"
        return {"name": name, "mission": getattr(pkg, "mission", None) or "", "start_here": pkg.start_here, "integration_branch": pkg.integration_branch,
                "coordinator": {**coord, "feed_session": fsess or None, "feed_fresh": feed_fresh, "feed_age_sec": feed_age,
                                "feed_note": ("feed read, no session entry" if feed and not fsess else "no feed" if not feed and (getattr(pkg, "workers", None) or {}).get("feed_url") else ""),
                                "process": ({"pid": coord_proc["pid"], "user": coord_proc["user"], "alive_since": coord_proc["started_at"]} if coord_proc else None),
                                "verdict": next((r["actual"] for r in roster if r["lane"] in persistent), None)},
                "workers": workers, "roster": roster,
                "roster_source": {k: roster_kv.get(k) for k in ("source", "url", "sha", "fetched_at")} if roster_kv else None,
                "tasks": {"ready": [t["task_id"] for t in tasks if t["status"] == "READY"],
                          "running": [{"task_id": t["task_id"], "stage": t["stage"], "last_activity": t["last_activity"]} for t in tasks if t["status"] == "RUNNING"],
                          "done": [{"task_id": f["id"], "status": f["status"], "pr_url": f["pr_url"]} for f in workers.get("finished", [])],
                          "blocked": [{"task_id": t["task_id"], "reason": (t.get("result") or {}).get("reason", "")[:140]} for t in tasks if t["status"] == "BLOCKED"]},
                "instructions": instr, "unacknowledged": len(unacked),
                "freshness": {"coordinator_checked": coord.get("last_check_at"), "workers_verified": workers.get("verified_at"),
                              "feed_read": feed.get("fetched_at"), "feed_self_time": (feed.get("data") or {}).get("last_check"),
                              "roster_read": roster_kv.get("fetched_at"), "roster_declared_as_of": (roster_kv.get("rows") or [{}])[0].get("declared_as_of")},
                "primary_action": primary, "generated_at": now}


def work_summary(projects: list[dict], tasks: list[dict], inbox: list[dict], intake: dict, scheduler, slots: dict, store) -> dict:
    """Every row names who it waits on (`who`). Three short lists for the owner, from live sources only: what can run now, what needs the owner,
    what is waiting on someone else. Never 'nothing needs you' when a project's own data says otherwise."""
    can, need, wait = [], [], []
    now = time.time()
    ready = [t for t in tasks if t["status"] == "READY"]
    paused = bool(getattr(scheduler, "paused", False))
    if ready:
        if paused:
            wait.append({"project": "desktop_agent", "who": "scheduler (you can press Resume)", "text": f"{len(ready)} queued control-plane task(s) held: scheduler paused ({getattr(scheduler, 'hold_reason', None) or 'by owner'})"})
        elif slots.get("free", 0) > 0:
            can.append({"project": "desktop_agent", "who": "scheduler", "text": f"{len(ready)} queued control-plane task(s) start on the next scheduler tick ({slots.get('free')} free slot(s))"})
        else:
            wait.append({"project": "desktop_agent", "who": "scheduler", "text": f"{len(ready)} queued control-plane task(s) wait for a free slot ({slots.get('ceiling_reason') or 'budget or capacity'})"})
    for d in inbox:
        need.append({"project": d.get("project") or "desktop_agent", "who": "you", "text": f"optional decision: {d['question'][:90]}…" if len(d["question"]) > 90 else f"optional decision: {d['question']}"})
    for p in projects:
        name, w, c = p["name"], p.get("workers") or {}, p.get("coordinator") or {}
        ex = c.get("external") or {}
        for f in w.get("finished") or []:
            if "open" in f["status"]:
                need.append({"project": name, "who": "you", "text": f"merge the verified PR {f['pr_url']} ({f['id'][:30]})"})
        for item in ex.get("ready_unblocked") or []:
            can.append({"project": name, "who": "its coordinator", "text": f"its coordinator has unblocked work ready: {str(item)[:90]}"})
        for item in ex.get("open_items") or []:
            wait.append({"project": name, "who": "its coordinator", "text": f"instruction {item} in its coordinator's hands (WORKING until its DONE comment)"})
        if ex.get("waiting_owner"):
            need.append({"project": name, "who": "you", "text": f"{len(ex['waiting_owner'])} gate(s) on its own list need you (see the approval packet's read-only section): " + "; ".join(map(str, ex["waiting_owner"][:3])) + ("…" if len(ex["waiting_owner"]) > 3 else "")})
        feed = store.get_kv(f"feed:{name}") or {}
        fw = ((feed.get("data") or {}).get("work") or {}) if feed else {}
        fstate = str(((feed.get("data") or {}).get("state") or "")).upper() if feed else ""
        if feed and fstate == "STOPPED" and not (feed.get("data") or {}).get("session"):
            ff = feed_freshness(feed, now, last_ack=(store.coordinator_rows().get(name) or {}).get("last_ack"))
            pend = (feed.get("data") or {}).get("unacknowledged_messages") or []
            acked_here = {i["title"]: i for i in store.list_intake(name) if i.get("status") == "ACKNOWLEDGED"}
            with_receipt = [m for m in pend if any(m in t for t in acked_here)]
            receipts = (f"{len(pend)} message(s) its feed lists as unread, {len(with_receipt)} of them with an ack receipt here" if pend
                        else "no message its feed lists as unread")
            if ff["fresh"] and not ff["ack_recent"]:
                need.insert(0, {"project": name, "who": "you (other account)",
                                "text": f"its coordinator session is STOPPED per its own watchdog ({ff['label']}); start it in your own account (tmux mbos-agent-01) — {receipts}" + (": " + ", ".join(pend[:2]) if pend else "")})
            elif ff["ack_recent"]:
                wait.append({"project": name, "who": "its coordinator (other account)",
                             "text": f"its watchdog says STOPPED ({ff['label']}) but an ack file from its account appeared {_ago(now - float(ff['last_ack']))} ago: the inbox is being processed, state UNKNOWN; no restart implied — {receipts}"})
            else:
                wait.append({"project": name, "who": "its watchdog (other account)",
                             "text": f"its watchdog last said STOPPED, but {ff['label']}; not a current state and not a restart instruction — {receipts}"})
        if fw and not feed_freshness(feed, now)["fresh"]:
            wait.append({"project": name, "who": "its watchdog (other account)",
                         "text": f"its feed's dispatcher/ready-row figures are not current ({feed_freshness(feed, now)['label']}); nothing is claimed from them"})
        elif fw:
            rows = fw.get("approved_ready_rows_for_specialist_lanes") or 0
            if rows and fw.get("dispatcher_running"):
                can.append({"project": name, "who": "its dispatcher", "text": f"{rows} approved row(s) ready; its own dispatcher launches them (quota allows a turn: {(feed.get('data') or {}).get('quota_allows_a_turn', '?')})"})
            elif rows:
                need.append({"project": name, "who": "you (other account)", "text": f"{rows} approved row(s) ready but its dispatcher is stopped: restart it in your own account (tmux mbos-agent-01)"})
            elif fw.get("dispatcher_running"):
                wait.append({"project": name, "who": "its coordinator", "text": "dispatcher running, 0 approved rows: nothing to launch until a gate clears or a new gap is queued"})
        for r in (store.get_kv(f"roster:{name}") or {}).get("rows") or []:
            b = r.get("blocker") or ""
            if b and b.lower() != "none" and any(k in b for k in ("MICHAEL_DECISIONS", "credentials", "Michael", "host software", "host")):
                need.append({"project": name, "who": "you", "text": f"lane {r['lane']} {r['role'][:20]}: {b[:110]}"})
    unacked = [i for i in (intake.get("items") or []) if i.get("flagged") and i.get("status") in ("RECEIVED", "DELIVERED")]
    by_proj: dict[str, int] = {}
    for i in unacked:
        by_proj[i["project"]] = by_proj.get(i["project"], 0) + 1
    for name, n in by_proj.items():
        liaison = any(i.get("kind") == "liaison" for i in unacked if i["project"] == name)
        wait.append({"project": name, "who": ("Agent 01" if liaison else "its coordinator"), "text": f"{n} instruction(s) delivered, not acknowledged" + (": Agent 01 reads the liaison branch at its own sync; you can nudge it in your own account (tmux mbos-agent-01)" if liaison else ": resend from the Shared inbox or check the coordinator session")})
    for p in projects:
        if (p.get("workers") or {}).get("status") == "RUNNING":
            can.append({"project": p["name"], "who": "workers", "text": f"{(p['workers'] or {}).get('active', 0)} worker(s) running now: " + ", ".join(x["id"] for x in (p["workers"] or {}).get("workers", [])[:4])})
    return {"can_run_now": can, "needs_owner": need, "waiting": wait}


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec // 3600}h" if sec < 86400 else f"{sec // 86400}d"


def _age(posted_at: str, updated_at: float | None) -> str:
    try:
        ts = time.mktime(time.strptime(posted_at[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if posted_at else float(updated_at or time.time())
    except Exception:
        ts = float(updated_at or time.time())
    return _ago(time.time() - ts) + " ago"
