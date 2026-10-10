"""Worker visibility from grounded sources only: idle coordinator with two live workers, a stale worker,
finished-without-merge, paused on quota, a genuinely idle shop, and a project with no telemetry (UNKNOWN)."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from desktop_agent.control.store import Store
from desktop_agent.control.workers import WorkerMonitor


def pkg(name, **workers):
    return SimpleNamespace(name=name, github_repo="x/y" if name == "da" else None, workers=workers or None)


def procs_two_workers(ticks=(5, 7)):
    return [{"pid": 10, "ppid": 1, "user": "c", "cmd": "claude --model sonnet You are the coordinator", "ticks": 1, "started_at": 1000.0},
            {"pid": 20, "ppid": 10, "user": "c", "cmd": 'bash -c claude "$(cat /home/x/CAOSCARE-BOUNDED/rq-050-claim.prompt)"', "ticks": 0, "started_at": 2000.0},
            {"pid": 21, "ppid": 20, "user": "c", "cmd": "claude --model sonnet You are a fresh bounded worker", "ticks": ticks[0], "started_at": 2001.0},
            {"pid": 30, "ppid": 10, "user": "c", "cmd": 'bash -c claude "$(cat /home/x/CAOSCARE-BOUNDED/rq-051-audit.prompt)"', "ticks": 0, "started_at": 2100.0},
            {"pid": 31, "ppid": 30, "user": "c", "cmd": "claude --model sonnet You are a fresh bounded worker", "ticks": ticks[1], "started_at": 2101.0}]


def test_idle_coordinator_with_two_active_workers(tmp_path: Path):
    store = Store(tmp_path); now = [3000.0]
    procs = [procs_two_workers()]
    mon = WorkerMonitor(store, {}, procs_fn=lambda: procs[0], clock=lambda: now[0])
    p = pkg("caoscare", process_patterns=[r"CAOSCARE-BOUNDED/(?P<id>[\w-]+)\.prompt"], quota_shared_with_control_plane=True)
    store.set_kv("quota", {"windows": {"five_hour": {"utilization": 0.63}}, "seen_at": now[0]})
    s = mon.snapshot(p, [], scheduler=SimpleNamespace(paused=False, hold_reason=None))
    assert s["status"] == "RUNNING" and s["active"] == 2 and [w["id"] for w in s["workers"]] == ["rq-050-claim", "rq-051-audit"]
    assert all(w["status"] == "RUNNING" and w["pid"] in (20, 30) and "alive now" in w["evidence"] for w in s["workers"])
    assert "host process list" in s["summary"] and any("quota guard" in n and "63%" in n for n in s["notes"])
    # the process tree's CPU grows → progress time advances; no growth for long → STALE
    now[0] = 3100.0; procs[0] = procs_two_workers(ticks=(9, 7))
    s2 = mon.snapshot(p, [], None)
    by = {w["id"]: w for w in s2["workers"]}
    assert by["rq-050-claim"]["last_progress_at"] == 3100.0 and by["rq-050-claim"]["status"] == "RUNNING"
    now[0] = 3100.0 + 1000; procs[0] = procs_two_workers(ticks=(9, 7))
    s3 = mon.snapshot(p, [], None)
    by = {w["id"]: w for w in s3["workers"]}
    assert by["rq-050-claim"]["status"] == "STALE" and by["rq-051-audit"]["status"] == "STALE" and s3["status"] == "STALE"


def test_finished_without_merge_waiting_on_quota_and_idle_shop(tmp_path: Path):
    store = Store(tmp_path); now = 5000.0
    mon = WorkerMonitor(store, {}, procs_fn=lambda: [], clock=lambda: now)
    p = pkg("da", process_patterns=[r"Desktop-Agent-work/(?P<id>[\w-]+)/repo"])
    done = {"task_id": "t-done", "status": "DONE", "created_at": 4000.0, "updated_at": 4500.0, "last_activity_at": 4500.0,
            "result": {"integration": {"pr_url": "https://x/pr/11"}}}
    ready = {"task_id": "t-ready", "status": "READY", "created_at": 4900.0, "updated_at": 4900.0, "last_activity_at": 4900.0, "result": {}}
    # merge state unknown until the watchdog has checked open PRs
    s = mon.snapshot(p, [done], scheduler=SimpleNamespace(paused=False, hold_reason=None))
    assert s["finished"][0]["status"].startswith("FINISHED (verified; merge state not checked)") and s["status"] == "IDLE"
    store.set_kv("open_prs:x/y", {"checked_at": now, "urls": ["https://x/pr/11"]})
    s = mon.snapshot(p, [done], None)
    assert s["finished"][0]["status"] == "FINISHED (verified; PR open, not merged)" and s["status"] == "FINISHED"
    store.set_kv("open_prs:x/y", {"checked_at": now, "urls": []})
    assert mon.snapshot(p, [done], None)["finished"][0]["status"].startswith("DONE (verified; PR merged")
    # paused on a provider limit with a ready task → WAITING, reason shown
    s = mon.snapshot(p, [ready], scheduler=SimpleNamespace(paused=True, hold_reason="paused: provider limit (resets 14:20)"))
    assert s["status"] == "WAITING" and any("provider limit" in n and "1 ready" in n for n in s["notes"])
    # genuinely idle shop: sources checked, nothing running, never UNKNOWN
    s = mon.snapshot(p, [], None)
    assert s["status"] == "IDLE" and s["active"] == 0 and s["verified_at"] == now and "0 active now" in s["summary"]


def test_running_record_grounded_in_process_and_unknown_project(tmp_path: Path):
    store = Store(tmp_path)
    procs = [{"pid": 50, "ppid": 1, "user": "c", "cmd": "bwrap --bind /home/c/Desktop-Agent-work/t-run/repo /work claude -p", "ticks": 3, "started_at": 100.0}]
    mon = WorkerMonitor(store, {}, procs_fn=lambda: procs, clock=lambda: 200.0)
    run = {"task_id": "t-run", "status": "RUNNING", "stage": "BUILDING", "created_at": 100.0, "updated_at": 150.0, "last_activity_at": 150.0,
           "last_activity": "file written", "result": {}}
    s = mon.snapshot(pkg("da", process_patterns=[r"Desktop-Agent-work/(?P<id>[\w-]+)/repo"]), [run], None)
    assert s["status"] == "RUNNING" and s["workers"][0]["pid"] == 50 and s["workers"][0]["stage"] == "BUILDING" and "pid 50 alive" in s["workers"][0]["evidence"]
    # the same record with no process on the host says so instead of pretending
    mon2 = WorkerMonitor(store, {}, procs_fn=lambda: [], clock=lambda: 200.0)
    assert "worker process not found" in mon2.snapshot(pkg("da"), [run], None)["workers"][0]["evidence"]
    # no telemetry, no records → UNKNOWN with the last confirmed coordinator update, never a count
    u = mon2.snapshot(pkg("other"), [], None, last_confirmed=1791500000.0)
    assert u["status"] == "UNKNOWN" and u["active"] == 0 and "not verified" in u["summary"] and "last confirmed coordinator update" in u["summary"] and u["verified_at"] is None


def test_feed_and_open_pr_refresh(tmp_path: Path):
    store = Store(tmp_path)
    feed = {"projects": {"mbos": {"work": {"dispatcher_running": True, "stalled_workers": ["C-9"], "approved_ready_rows_for_specialist_lanes": 2},
                                  "quota_allows_a_turn": True, "last_check": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}}
    store.save_task("t-done", "g", "da", "DONE", {"objective": "o"}, "h", {"integration": {"pr_url": "https://x/pr/11"}})
    calls = []
    async def gh(path): calls.append(path); return [{"html_url": "https://x/pr/11"}]
    projects = {"mbos": pkg("mbos", feed_url="http://127.0.0.1:1/", feed_project="mbos"), "da": pkg("da")}
    mon = WorkerMonitor(store, projects, procs_fn=lambda: [], fetch_json=lambda url: feed, gh_api=gh)
    r = asyncio.run(mon.refresh_remote())
    assert r["mbos"] == "feed ok" and r["da:prs"] == 1 and calls == ["repos/x/y/pulls?state=open&per_page=100"]
    s = mon.snapshot(projects["mbos"], [], None)
    assert s["status"] == "STALE" and s["workers"][0]["id"] == "C-9" and any("dispatcher running" in n and "stalled 1" in n for n in s["notes"])
    assert "project feed" in s["summary"] and [a["level"] for a in s["alerts"]] == ["amber"] and "stalled workers: C-9" in s["alerts"][0]["text"]
    # dispatcher stopped with approved rows queued → an amber operational alert, not a quiet IDLE
    feed["projects"]["mbos"]["work"] = {"dispatcher_running": False, "stalled_workers": [], "approved_ready_rows_for_specialist_lanes": 1}
    asyncio.run(mon.refresh_remote())
    s = mon.snapshot(projects["mbos"], [], None)
    assert s["status"] == "IDLE" and [a["level"] for a in s["alerts"]] == ["amber"]
    assert s["alerts"][0]["text"].startswith("dispatcher stopped — 1 approved row(s) queued and not being dispatched (observed 2026-") and "read 0s ago" in s["alerts"][0]["text"]
    feed["projects"]["mbos"]["work"]["approved_ready_rows_for_specialist_lanes"] = 0
    asyncio.run(mon.refresh_remote())
    assert mon.snapshot(projects["mbos"], [], None)["alerts"][0]["level"] == "info"
