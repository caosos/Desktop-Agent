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

from .intake import gh_api


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
        roster = []
        for r in roster_kv.get("rows", []):
            live = live_by_lane.get(r["lane"], [])
            if live:
                actual, proof = "RUNNING", f"{', '.join(w['id'] for w in live)} — pid {live[0]['pid']} alive since {_ago(now - live[0]['started_at'])}"
            elif r["lane"] in persistent and coord_proc:
                actual = "SESSION ALIVE" + (f" ({fsess.get('status')})" if fsess.get("status") else "")
                proof = (f"coordinator process pid {coord_proc['pid']} ({coord_proc['user']}) alive on this host since {_ago(now - coord_proc['started_at'])}; "
                         + (f"its own feed says session {fsess.get('name', '?')} {fsess.get('status', '?')}; " if fsess else "its feed carries no session entry; ")
                         + "not reachable from this account")
            elif r["lane"] in persistent and fsess:
                actual = "SESSION " + str(fsess.get("status", "?")).upper()
                proof = f"per the project's own feed: session {fsess.get('name', '?')} {fsess.get('status', '?')} (pid {fsess.get('pid', '?')}, feed {((feed.get('data') or {}).get('last_check')) or ''}); not reachable from this account"
            else:
                actual, proof = "NOT RUNNING", "no process for this lane on the host now (bounded worker per task; launched only by the project's own dispatcher)"
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
            elif i["status"] in ("WORKING", "ACKNOWLEDGED", "CLAIMED"):
                nxt = "in the coordinator's hands; DONE arrives by its comment or ack file"
            elif i["status"] in ("POSTED", "SENT"):
                nxt = "posted; delivery pending"
            else:
                nxt = ""
            instr.append({"item_id": i["item_id"], "title": (i.get("title") or "")[:90], "status": i["status"], "age": age, "flagged": bool(i.get("flagged")),
                          "url": i.get("url", ""), "next": nxt})
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
                "coordinator": {**coord, "feed_session": fsess or None,
                                "feed_note": ("feed read, no session entry" if feed and not fsess else "no feed" if not feed and (getattr(pkg, "workers", None) or {}).get("feed_url") else ""),
                                "process": ({"pid": coord_proc["pid"], "user": coord_proc["user"], "alive_since": coord_proc["started_at"]} if coord_proc else None)},
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


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m" if sec < 3600 else f"{sec // 3600}h" if sec < 86400 else f"{sec // 86400}d"


def _age(posted_at: str, updated_at: float | None) -> str:
    try:
        ts = time.mktime(time.strptime(posted_at[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if posted_at else float(updated_at or time.time())
    except Exception:
        ts = float(updated_at or time.time())
    return _ago(time.time() - ts) + " ago"
