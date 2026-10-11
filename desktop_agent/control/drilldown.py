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

from .work_summary import _age, _ago, _open_item_text, work_summary  # noqa: F401  (re-exported; state_view and tests import work_summary from here)


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

    # ---- three lines the owner reads first: Now / Next / Needs Michael, from current sources only --------------
    def _summary(self, name: str, st: dict, coord: dict, workers: dict, feed: dict, ff: dict, fsess: dict, tasks: list, roster_kv: dict, now: float) -> dict:
        """Owner finding 2026-10-11: the Oct-8 declared table next to live rows read like current work and fresh demands.
        These three lines use only fresh evidence (live processes, the project's own feed within its TTL, open decisions,
        current owner gates). The declared roster never feeds them; it is shown separately as historical."""
        data = (feed.get("data") or {}) if feed else {}
        work = data.get("work") or {}
        live = workers.get("workers") or []
        parts = []
        for w in live[:4]:
            parts.append(f"{w['id']} running" + (f" on lane {w['lane']}" if w.get("lane") else "") + (f" (pid {w['pid']}, {_ago(now - w['started_at'])})" if w.get("pid") and w.get("started_at") else ""))
        if coord.get("kind") == "liaison":
            if fsess and ff["fresh"]:
                parts.append(f"coordinator session {fsess.get('name', '?')} {str(fsess.get('status') or '?').lower()} per its own feed ({ff['label']})")
            elif feed and not ff["fresh"]:
                parts.append(f"coordinator: UNKNOWN — its feed is {ff['status']} ({ff['label']})")
            else:
                parts.append("coordinator: NOT VERIFIED (separate account, no fresh feed record)")
            if ff["fresh"] and "dispatcher_running" in work:
                parts.append("dispatcher running" if work.get("dispatcher_running") else "dispatcher STOPPED")
        elif coord.get("kind") == "claude_peer":
            parts.append(f"coordinator session {coord.get('session')} {coord.get('session_status')}" if coord.get("connected") else "coordinator session DOWN (registry)")
        else:
            parts.append("coordinator: this control plane")
        if not live and not any("running" in x for x in parts[1:2]):
            parts.append("no bounded worker process observed on the host")
        now_line = "; ".join(parts)
        ready_here = [t["task_id"] for t in tasks if t["status"] == "READY"]
        if ff["fresh"] and work.get("approved_ready_rows_for_specialist_lanes"):
            n = work["approved_ready_rows_for_specialist_lanes"]
            nxt = f"{n} approved row(s) ready in its own queue; its dispatcher launches them (feed {ff['label']})"
        elif ff["fresh"] and str(work.get("dispatcher_action") or "NONE").upper() != "NONE":
            nxt = f"dispatcher action {work['dispatcher_action']} (feed {ff['label']})"
        elif ready_here:
            nxt = f"queued here: {', '.join(ready_here[:3])}"
        elif feed and not ff["fresh"]:
            nxt = f"UNKNOWN — the project's feed is {ff['status']} ({ff['label']}); nothing current names a next item"
        elif ff["fresh"]:
            nxt = "nothing queued per its own feed right now"
        else:
            nxt = "UNKNOWN — no project feed to read a next item from"
        needs = []
        dec = data.get("decision")
        if ff["fresh"] and dec and str(dec).strip().lower() not in ("nothing needed", "none", ""):
            needs.append(f"its feed asks: {str(dec)[:200]}")
        for g in (self.svc.store.get_kv("gates:" + name) or {}).get("gates") or []:
            if g.get("gate"):
                needs.append(f"owner gate (from its coordinator's ack file): {g['gate'][:160]}")
        for d in st.get("inbox") or []:
            if (d.get("project") or "") == name:
                needs.append(f"decision {d.get('decision_id', '')}: {str(d.get('question', ''))[:160]}")
        synced = re.search(r"\d{4}-\d{2}-\d{2}", str((roster_kv.get("rows") or [{}])[0].get("declared_as_of") or ""))
        return {"now": now_line, "next": nxt,
                "needs_michael": needs[:3] or ["nothing needed now"],
                "roster_synced": synced.group(0) if synced else None,
                "roster_note": ("Declared table from the project's own status file. CLOSED means that lane's last bounded worker ended, not that the agent "
                                "was removed or the project finished. Codes such as B-12, D-09b, licenses_held or 'off-box destination' are historical tracking "
                                "references from that table, not current owner questions; a current question appears under Needs Michael.")}

    # ---- the view ---------------------------------------------------------------------------
    def build(self, name: str) -> dict | None:
        pkg = self.svc.projects.get(name)
        if not pkg:
            return None
        now = time.time()
        st = self.svc.state()
        state = st                                      # `st` is reused below for a session status string; keep the dict
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
                stored_need = self.svc.store.get_kv("need:" + i["item_id"])
                nxt = (f"{stored_need['classification']}: {stored_need['action']}" if stored_need and stored_need.get("action")
                       else "awaiting Agent 01's ack file; it reads the liaison branch at its own sync — no cross-account wake exists" if i.get("kind") == "liaison"
                       else "delivered but not acknowledged; resend from the Shared inbox or check the coordinator session")
            elif i["status"] in ("WORKING", "ACKNOWLEDGED", "CLAIMED", "BLOCKED") and i.get("kind") == "liaison" and self.svc.store.get_kv("liaison_ack:" + i["item_id"]):
                _d = self.svc.store.get_kv("liaison_ack:" + i["item_id"])
                _open = [g["gate"] for g in (self.svc.store.get_kv("gates:" + name) or {}).get("gates") or [] if g.get("source") == _d.get("file")]
                nxt = ack_next_action(_d, i["status"], _open)
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
                          "disposition": ({**{k: disp.get(k) for k in ("stage", "category", "implementation", "live", "links", "links_resolved", "not_proven", "followups", "sha", "url", "observed_at", "first_seen_at", "history", "stage_line", "retained_block")},
                                           "gates_open": [g["gate"] for g in (self.svc.store.get_kv("gates:" + name) or {}).get("gates") or [] if g.get("source") == disp.get("file")]}
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
        summary = self._summary(name, state, coord, workers, feed, ff, fsess, tasks, roster_kv, now)
        return {"name": name, "mission": getattr(pkg, "mission", None) or "", "start_here": pkg.start_here, "integration_branch": pkg.integration_branch,
                "summary": summary,
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
