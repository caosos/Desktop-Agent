"""Project drilldown: declared roster rows are quoted from the project's own ACTIVE_WORK table and merged with
LIVE evidence only (a host process for the lane now, the feed's session entry for the persistent lane, otherwise
NOT RUNNING); instructions carry age and an honest next action; the primary action follows real alerts."""
import asyncio
import time
from pathlib import Path

from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.drilldown import parse_active_work_table
from desktop_agent.control.service import Service
from desktop_agent.control import drilldown as dd

TABLE = """# ACTIVE WORK
- **Last synced:** 2026-10-08 against lane heads 02 `55a7e19`

| Lane | State | Last bounded worker result | READY work | Blocker |
|---|---|---|---|---|
| **01** Coordinator (persistent) | WORKING | A-35, gate green (437) | none open | none |
| **02** Discovery | CLOSED (worker per task) | P-02-15 `349f21c` | none | B-12 live smoke: credentials |
| **06** Operator UI | CLOSED | P-06-18 partial | re-run P-06-18 | none |
| **07** QA | CLOSED (fresh worker per release window) | G-14 `2e1e5f8` | next release window | none |
"""


def test_parse_active_work_table():
    rows = parse_active_work_table(TABLE)
    assert [r["lane"] for r in rows] == ["01", "02", "06", "07"] and rows[0]["role"] == "Coordinator (persistent)"
    assert rows[1]["declared_state"].startswith("CLOSED") and rows[1]["last_result"] == "P-02-15 `349f21c`" and rows[1]["blocker"].startswith("B-12")
    assert rows[0]["declared_as_of"].startswith("2026-10-08")


def _service(tmp_path: Path) -> Service:
    repo = tmp_path / "mbos"; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
    (tmp_path / "m.yaml").write_text(f"name: mbos\nrepo_path: {repo}\nremote_url: x\ngithub_repo: x/mbos\nintegration_branch: main\nstart_here: S\nagents_file: A\n"
                                     "mission: Deal Sniffer test mission\nintake:\n  issues: []\n  coordinator:\n    kind: liaison\n    liaison: {branch: l, inbox_dir: i, ack_branch: a, ack_dir: d}\n"
                                     "workers:\n  process_patterns: ['tools/worker\\.py (?P<id>[A-Z]-\\d+)(?: --lane (?P<lane>\\d+))?']\n  feed_url: http://127.0.0.1:1/\n  feed_project: mbos\n"
                                     "roster:\n  branch: b\n  path: docs/status/ACTIVE_WORK.md\n  kind: active_work_table\n  persistent_lanes: ['01']\n")
    s = Service(RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t", project_files=[tmp_path / "m.yaml"]))
    s.scheduler.paused = True
    return s


def test_drilldown_roster_live_vs_declared_and_instructions(tmp_path: Path, monkeypatch):
    s = _service(tmp_path)
    async def fake_gh(path, **kw):
        import base64
        assert path.startswith("repos/x/mbos/contents/docs/status/ACTIVE_WORK.md")
        return {"content": base64.b64encode(TABLE.encode()).decode(), "encoding": "base64", "html_url": "https://x/ACTIVE_WORK.md", "sha": "abc123def456"}
    monkeypatch.setattr(dd, "gh_api", fake_gh)
    assert asyncio.run(s.drill.refresh_remote()) == {"mbos": "4 declared rows"}
    s.store.set_kv("feed:mbos", {"fetched_at": time.time(), "url": "u", "data": {"work": {"dispatcher_running": True, "stalled_workers": [], "approved_ready_rows_for_specialist_lanes": 1},
                                                                       "session": {"name": "agent-01-coordinator-17", "status": "idle", "pid": 14005}, "last_check": "2026-10-09T22:00:00Z"}})
    s.workers.procs_fn = lambda: [{"pid": 77, "ppid": 1, "user": "michaelos", "cmd": "python -I tools/worker.py F-39 --lane 06 --kind implement", "ticks": 3, "started_at": time.time() - 120}]
    s.store.save_intake({"item_id": "da-old0000000", "project": "mbos", "repo": "x/mbos", "issue": 0, "kind": "liaison", "gh_id": 0, "author": "aria", "title": "Owner direction one",
                         "body": "b", "url": "https://x/m1", "posted_at": "2026-10-09T18:00:00Z", "status": "RECEIVED", "coordinator": "liaison"})
    s.store.flag_intake("da-old0000000")
    s.store.save_intake({"item_id": "da-ack0000000", "project": "mbos", "repo": "x/mbos", "issue": 0, "kind": "liaison", "gh_id": 0, "author": "aria", "title": "Acked one",
                         "body": "b", "url": "https://x/m2", "posted_at": "2026-10-09T19:00:00Z", "status": "ACKNOWLEDGED", "coordinator": "liaison"})
    d = s.drill.build("mbos")
    assert d["mission"] == "Deal Sniffer test mission" and d["coordinator"]["feed_session"]["name"] == "agent-01-coordinator-17"
    by = {r["lane"]: r for r in d["roster"]}
    assert by["01"]["actual"] == "SESSION IDLE" and "not reachable from this account" in by["01"]["proof"]
    assert by["06"]["actual"] == "RUNNING" and "F-39" in by["06"]["proof"] and by["06"]["declared_state"] == "CLOSED"       # declared ≠ live, both shown
    assert by["02"]["actual"] == "NOT RUNNING" and by["07"]["actual"] == "NOT RUNNING" and "no process for this lane" in by["02"]["proof"]
    assert all(r["launched_by"] == "project dispatcher" for r in d["roster"]) and d["roster_source"]["sha"] == "abc123def456"
    assert d["unacknowledged"] == 1 and d["instructions"][0]["item_id"] == "da-ack0000000" and d["instructions"][1]["flagged"]
    assert "no cross-account wake" in d["instructions"][1]["next"] and d["instructions"][1]["age"].endswith("ago")
    assert d["primary_action"].startswith("1 instruction(s) not acknowledged") and d["freshness"]["roster_declared_as_of"].startswith("2026-10-08")
    # the persistent coordinator's own process (other account) is shown alive when declared and visible; feed without session says so
    s.projects["mbos"].roster["coordinator_process_pattern"] = "ROUND TWO AGENT 01 — COORDINATOR"
    s.workers.procs_fn = lambda: [{"pid": 14005, "ppid": 1, "user": "michaelos", "cmd": "claude --permission-mode acceptEdits MICHAEL BUSINESS OS — ROUND TWO AGENT 01 — COORDINATOR", "ticks": 1, "started_at": time.time() - 5000}]
    s.store.set_kv("feed:mbos", {"fetched_at": time.time(), "url": "u", "data": {"work": {"dispatcher_running": True, "stalled_workers": [], "approved_ready_rows_for_specialist_lanes": 0}, "session": None, "last_check": "t"}})
    d = s.drill.build("mbos"); by = {r["lane"]: r for r in d["roster"]}
    assert by["01"]["actual"] == "SESSION ALIVE" and "pid 14005 (michaelos)" in by["01"]["proof"] and "no session entry" in by["01"]["proof"]
    assert d["coordinator"]["process"]["pid"] == 14005 and d["coordinator"]["feed_note"] == "feed read, no session entry"
    assert by["06"]["actual"] == "NOT RUNNING"
    # a stopped dispatcher with queued rows outranks the unacknowledged instruction
    s.store.set_kv("feed:mbos", {"fetched_at": time.time(), "url": "u", "data": {"work": {"dispatcher_running": False, "stalled_workers": [], "approved_ready_rows_for_specialist_lanes": 2}, "last_check": "t"}})
    assert s.drill.build("mbos")["primary_action"].startswith("dispatcher stopped")
    assert s.drill.build("nope") is None


def test_work_summary_three_lists(tmp_path: Path):
    from types import SimpleNamespace
    from desktop_agent.control.drilldown import work_summary
    from desktop_agent.control.store import Store
    store = Store(tmp_path)
    store.set_kv("feed:mbos", {"fetched_at": time.time(), "data": {"work": {"dispatcher_running": True, "approved_ready_rows_for_specialist_lanes": 2}, "quota_allows_a_turn": True}})
    store.set_kv("roster:mbos", {"rows": [{"lane": "02", "role": "Discovery", "blocker": "B-12 live smoke: credentials + MICHAEL_DECISIONS #8"}, {"lane": "07", "role": "QA", "blocker": "none"}]})
    projects = [{"name": "caoscare", "workers": {"status": "IDLE", "finished": [{"id": "c2e594", "status": "FINISHED (verified; PR open, not merged)", "pr_url": "https://x/110"}]},
                 "coordinator": {"external": {"ready_unblocked": ["RQ-052 docs"], "open_items": ["da-1"], "waiting_owner": ["drivers", "telephony"]}}},
                {"name": "mbos", "workers": {"status": "RUNNING", "active": 1, "workers": [{"id": "F-39"}]}, "coordinator": {}}]
    tasks = [{"task_id": "t-ready", "status": "READY"}]
    intake = {"items": [{"project": "mbos", "kind": "liaison", "flagged": 1, "status": "RECEIVED"}, {"project": "mbos", "kind": "liaison", "flagged": 1, "status": "RECEIVED"}]}
    w = work_summary(projects, tasks, [{"question": "Optional: key?", "project": "desktop_agent"}], intake, SimpleNamespace(paused=False, hold_reason=None), {"free": 1}, store)
    can, need, wait = [x["text"] for x in w["can_run_now"]], [x["text"] for x in w["needs_owner"]], [x["text"] for x in w["waiting"]]
    assert any("1 queued control-plane task(s) start" in t for t in can) and any("RQ-052 docs" in t for t in can)
    assert any("2 approved row(s) ready; its own dispatcher launches them" in t for t in can) and any("1 worker(s) running now: F-39" in t for t in can)
    assert need[0] == "optional decision: Optional: key?" and any("merge the verified PR https://x/110" in t for t in need)
    assert any("2 gate(s) on its own list" in t and "drivers; telephony" in t for t in need) and any("lane 02 Discovery: B-12 live smoke" in t for t in need)
    assert not any("lane 07" in t for t in need)
    assert any("instruction da-1 in its coordinator's hands" in t for t in wait) and any("2 instruction(s) delivered, not acknowledged: Agent 01 reads the liaison branch" in t for t in wait)
    w2 = work_summary(projects, tasks, [], intake, SimpleNamespace(paused=True, hold_reason="paused: provider limit"), {"free": 0}, store)
    assert any("held: scheduler paused (paused: provider limit)" in t for t in [x["text"] for x in w2["waiting"]])
