"""Worker visibility, separate from coordinator status, from grounded sources only.

A coordinator session at rest says nothing about its workers; a queue row, a "running" sentence in a
status file, an old AGENT_STATUS document or a shell-command count never counts as a live worker.
Sources, each named in the output:
  1. this control plane's own task records (tasks it launched, any project);
  2. live host processes matched by the project's declared `workers.process_patterns` (read from /proc,
     any account, read-only; the `id` group names the job; CPU ticks of the process tree give progress);
  3. the project's own read-only feed (`workers.feed_url`, fetched by the watchdog, never a model);
  4. open pull requests (free GitHub call) to tell FINISHED-but-not-merged from merged.
Labels: RUNNING (process alive now / task record live), STALE (alive but no CPU progress for
`stale_after_sec`), FINISHED (verified task whose PR is still open), WAITING (scheduler paused, e.g. on
quota), IDLE (sources checked, nothing running), UNKNOWN ("worker runtime not verified": no source
applies). Counts are never fabricated.
"""
from __future__ import annotations

import asyncio
import json
import os
import pwd
import re
import time
import urllib.request

STALE_AFTER_SEC = 900


def read_procs() -> list[dict]:
    """Every process on the host: pid, ppid, user, command line, CPU ticks, start time (epoch)."""
    out = []
    try:
        btime = int(next(l for l in open("/proc/stat") if l.startswith("btime")).split()[1])
    except Exception:
        btime = 0
    hz = os.sysconf("SC_CLK_TCK") or 100
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
            if not cmd:
                continue
            with open(f"/proc/{d}/stat") as fh:
                st = fh.read()
            rest = st[st.rindex(")") + 2:].split()            # fields from (3) state onward
            try:
                user = pwd.getpwuid(os.stat(f"/proc/{d}").st_uid).pw_name
            except Exception:
                user = "?"
            out.append({"pid": int(d), "ppid": int(rest[1]), "user": user, "cmd": cmd,
                        "ticks": int(rest[11]) + int(rest[12]), "started_at": btime + int(rest[19]) / hz})
        except Exception:
            continue
    return out


def _fetch_json(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read().decode())


def _tree_ticks(pid: int, procs: list[dict]) -> int:
    kids: dict[int, list[dict]] = {}
    for p in procs:
        kids.setdefault(p["ppid"], []).append(p)
    total, stack = 0, [pid]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        total += next((p["ticks"] for p in procs if p["pid"] == cur), 0)
        stack.extend(k["pid"] for k in kids.get(cur, []))
    return total


class WorkerMonitor:
    def __init__(self, store, projects: dict, procs_fn=read_procs, clock=time.time, fetch_json=_fetch_json,
                 gh_api=None, stale_after_sec: int = STALE_AFTER_SEC):
        self.store, self.projects = store, projects
        self.procs_fn, self.clock, self.fetch_json, self.gh_api = procs_fn, clock, fetch_json, gh_api
        self.stale_after_sec = stale_after_sec
        self._seen: dict[tuple[str, str], tuple[int, float]] = {}      # (project, id) → (ticks, last_progress_at)

    # ---- progress memory ------------------------------------------------------------------
    def _progress(self, key: tuple[str, str], ticks: int, now: float, default: float) -> tuple[float, bool]:
        prev = self._seen.get(key)
        if prev is None:
            self._seen[key] = (ticks, default)
            return default, False
        last = now if ticks > prev[0] else prev[1]
        self._seen[key] = (ticks, last)
        return last, True

    # ---- host processes ---------------------------------------------------------------------
    def _matched(self, project: str, patterns: list[str], procs: list[dict], now: float) -> list[dict]:
        found: dict[str, dict] = {}
        for pat in patterns:
            rx = re.compile(pat)
            for p in procs:
                m = rx.search(p["cmd"])
                if not m:
                    continue
                wid = (m.groupdict().get("id") or m.group(0))[:60]
                if wid in found and found[wid]["started_at"] <= p["started_at"]:
                    continue
                found[wid] = p
        out = []
        for wid, p in found.items():
            ticks = _tree_ticks(p["pid"], procs)
            last, observed = self._progress((project, wid), ticks, now, p["started_at"] if ticks == 0 else now)
            stale = observed and now - last > self.stale_after_sec
            out.append({"id": wid, "pid": p["pid"], "user": p["user"], "started_at": p["started_at"], "last_progress_at": last,
                        "status": "STALE" if stale else "RUNNING", "source": "host process",
                        "evidence": f"pid {p['pid']} ({p['user']}) alive now; CPU {ticks} ticks" + ("" if observed else "; first observation")})
        return sorted(out, key=lambda w: w["started_at"])

    # ---- the per-project picture ------------------------------------------------------------
    def snapshot(self, pkg, ptasks: list[dict], scheduler=None, last_confirmed: float | None = None) -> dict:
        now = self.clock()
        cfg = getattr(pkg, "workers", None) or {}
        patterns = list(cfg.get("process_patterns") or [])
        running_tasks = [t for t in ptasks if t["status"] == "RUNNING"]
        sources, workers, finished, notes = [], [], [], []
        procs = self.procs_fn() if (patterns or running_tasks) else []
        if ptasks:
            sources.append("control-plane task records")
        for t in running_tasks:
            proc = next((p for p in procs if f"Desktop-Agent-work/{t['task_id']}/" in p["cmd"]), None)
            stale = now - float(t["last_activity_at"] or now) > self.stale_after_sec
            workers.append({"id": t["task_id"], "pid": proc["pid"] if proc else None, "user": proc["user"] if proc else None,
                            "started_at": float(t["created_at"]), "last_progress_at": float(t["last_activity_at"] or t["created_at"]),
                            "status": "STALE" if stale else "RUNNING", "stage": t.get("stage"), "source": "control-plane task",
                            "evidence": f"task record, stage {t.get('stage')}, last event {t.get('last_activity', '')[:60]}"
                                        + (f"; pid {proc['pid']} alive" if proc else "; worker process not found on host")})
        if patterns:
            sources.append("host process list (/proc)")
            have = {w["id"] for w in workers}
            workers += [w for w in self._matched(pkg.name, patterns, procs, now) if w["id"] not in have]
        # finished but not merged (tasks this control plane verified, PR still open)
        repo = getattr(pkg, "github_repo", None)
        open_prs = (self.store.get_kv(f"open_prs:{repo}") or {}) if repo else {}
        for t in ptasks:
            pr = ((t.get("result") or {}).get("integration") or {}).get("pr_url")
            if t["status"] == "DONE" and pr and now - float(t["updated_at"]) < 86400:
                if not open_prs:
                    label = "FINISHED (verified; merge state not checked)"
                elif pr in open_prs.get("urls", []):
                    label = "FINISHED (verified; PR open, not merged)"
                else:
                    label = "DONE (verified; PR merged or closed)"
                finished.append({"id": t["task_id"], "pr_url": pr, "status": label, "at": float(t["updated_at"])})
        # read-only feed (fetched by the watchdog); operational alerts come only from what the feed states
        alerts: list[dict] = []
        feed = self.store.get_kv(f"feed:{pkg.name}") if cfg.get("feed_url") else None
        if feed:
            sources.append(f"project feed {cfg['feed_url']}")
            d = feed.get("data") or {}
            w = d.get("work") or {}
            if w:
                when = d.get("last_check") or feed.get("fetched_at")
                rows = w.get("approved_ready_rows_for_specialist_lanes")
                notes.append(f"feed: dispatcher {'running' if w.get('dispatcher_running') else 'not running'}, stalled {len(w.get('stalled_workers') or [])}, "
                             f"ready rows {rows if rows is not None else '?'}, quota allows a turn: {d.get('quota_allows_a_turn', '?')} ({when})")
                if not w.get("dispatcher_running"):
                    if rows:
                        alerts.append({"level": "amber", "text": f"dispatcher stopped — {rows} approved row(s) queued and not being dispatched (feed {when})"})
                    else:
                        alerts.append({"level": "info", "text": f"dispatcher stopped, nothing queued (feed {when})"})
                if w.get("stalled_workers"):
                    alerts.append({"level": "amber", "text": f"feed reports stalled workers: {', '.join(map(str, w['stalled_workers']))} (feed {when})"})
                for sid in w.get("stalled_workers") or []:
                    if not any(x["id"] == sid for x in workers):
                        workers.append({"id": str(sid), "pid": None, "user": None, "started_at": None, "last_progress_at": None, "status": "STALE",
                                        "source": "project feed", "evidence": "listed as stalled by the project's own feed"})
        elif cfg.get("feed_url"):
            notes.append(f"feed {cfg['feed_url']} not fetched yet or unreachable")
            alerts.append({"level": "info", "text": "project feed not reachable; worker picture from host processes only"})
        for x in workers:
            if x["status"] == "STALE" and x.get("source") != "project feed":
                alerts.append({"level": "amber", "text": f"{x['id']}: alive but no CPU progress for {int((now - (x['last_progress_at'] or now)) // 60)} min"})
        # waiting on this control plane's scheduler (quota or owner pause)
        paused = bool(scheduler is not None and getattr(scheduler, "paused", False))
        ready = [t for t in ptasks if t["status"] == "READY"]
        if paused and (ready or running_tasks):
            notes.append(f"scheduler paused: {getattr(scheduler, 'hold_reason', None) or 'by owner'}; {len(ready)} ready task(s) waiting")
            alerts.append({"level": "amber", "text": f"control-plane scheduler paused ({getattr(scheduler, 'hold_reason', None) or 'by owner'}); {len(ready)} ready task(s) waiting"})
        if cfg.get("quota_shared_with_control_plane"):
            q = self.store.get_kv("quota") or {}
            if q.get("windows"):
                notes.append("quota guard (same subscription as this control plane): " + ", ".join(
                    f"{k} {round((v.get('utilization') or 0) * 100)}%" for k, v in q["windows"].items()) + f" (seen {time.strftime('%H:%M', time.localtime(q.get('seen_at', 0)))})")
        if any(w["status"] == "RUNNING" for w in workers):
            status = "RUNNING"
        elif any(w["status"] == "STALE" for w in workers):
            status = "STALE"
        elif paused and ready:
            status = "WAITING"
        elif finished and any("open" in f["status"] for f in finished):
            status = "FINISHED"
        elif patterns or cfg.get("feed_url"):
            status = "IDLE"                      # only real telemetry may say "nobody is working"
        else:
            status = "UNKNOWN"                   # task records cover this control plane's own workers only
        active = sum(1 for w in workers if w["status"] in ("RUNNING", "STALE"))
        if status == "UNKNOWN":
            summary = ("no control-plane workers running; " if ptasks else "") + "worker runtime not verified: no worker telemetry configured for this project" + (
                f"; last confirmed coordinator update {time.strftime('%Y-%m-%d %H:%M', time.localtime(last_confirmed))}" if last_confirmed else "")
        else:
            summary = f"{active} active now (verified at {time.strftime('%H:%M:%S', time.localtime(now))} from {', '.join(sources)})"
        return {"status": status, "active": active, "summary": summary, "workers": workers, "finished": finished[-5:],
                "notes": notes, "alerts": alerts, "sources": sources, "verified_at": now if status != "UNKNOWN" else None,
                "rule": "coordinator at rest ≠ workers at rest; counts come only from the sources listed"}

    # ---- remote refresh (watchdog tick; free calls only) ---------------------------------------
    async def refresh_remote(self) -> dict:
        report = {}
        for name, pkg in self.projects.items():
            cfg = getattr(pkg, "workers", None) or {}
            if cfg.get("feed_url"):
                try:
                    data = await asyncio.to_thread(self.fetch_json, cfg["feed_url"])
                    sub = (data.get("projects") or {}).get(cfg.get("feed_project") or name) if isinstance(data, dict) else None
                    self.store.set_kv(f"feed:{name}", {"fetched_at": self.clock(), "url": cfg["feed_url"], "data": sub or data})
                    report[name] = "feed ok"
                except Exception as exc:
                    report[name] = f"feed error: {exc}"[:120]
            repo = getattr(pkg, "github_repo", None)
            if repo and self.gh_api and any(t["status"] == "DONE" and self.clock() - float(t["updated_at"]) < 86400 for t in self.store.list_tasks() if t["project"] == name):
                try:
                    prs = await self.gh_api(f"repos/{repo}/pulls?state=open&per_page=100")
                    self.store.set_kv(f"open_prs:{repo}", {"checked_at": self.clock(), "urls": [p.get("html_url") for p in prs or []]})
                    report[f"{name}:prs"] = len(prs or [])
                except Exception as exc:
                    report[f"{name}:prs"] = f"error: {exc}"[:120]
        return report
