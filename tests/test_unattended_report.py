"""The unattended-day report reads only the store and reports counts, ACK latency, NEEDs and owner involvement."""
import asyncio
import importlib.util
import time
from pathlib import Path

from tests.test_ask_aria import _service

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("unattended_report", ROOT / "scripts" / "unattended_report.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_report_counts_tasks_instructions_acks_and_needs(tmp_path):
    mod = _load()
    s = _service(tmp_path)
    # one verified task, one blocked retry
    s.store.save_task("t-1", "g", "mbos", "DONE", {"objective": "a"}, "h1")
    s.store.save_task("t-2", "g", "mbos", "BLOCKED", {"objective": "b", "supersedes": "t-0"}, "h2")
    # one instruction delivered then acknowledged 10 s later, one undelivered and flagged with a NEED body
    from desktop_agent.control.intake import Intake, IntakeSource
    src = IntakeSource(project="mbos", repo="x/y", issues=[1], coordinator={"kind": "claude_peer", "session_name": "s-1"})
    class Deliver:
        def session_for(self, src): return None
        async def __call__(self, src, item): return {"ok": False, "note": "no live Claude Code session", "evidence": ["registry"]}
    it = Intake(s.store, [src], Deliver(), poll_sec=1, unacked_after_sec=0)
    base = {"project": "mbos", "repo": "x/y", "issue": 1, "kind": "comment", "gh_id": 1, "author": "o", "title": "t", "body": "b", "url": "u",
            "posted_at": "2026-10-11T00:00:00Z", "status": "POSTED", "coordinator": "claude_peer"}
    a = {**base, "item_id": "da-aaaaaaaaaa"}; s.store.save_intake(a)
    it._set(a, "DELIVERED", "peer message to session s-1 (pid 1)", ["session=s-1"])
    time.sleep(0.02)
    it._set(a, "ACKNOWLEDGED", "coordinator comment", ["c"])
    b = {**base, "item_id": "da-bbbbbbbbbb", "gh_id": 2}; s.store.save_intake(b)
    it._set(b, "RECEIVED", "no live Claude Code session; queued", ["registry"])
    asyncio.run(it.poll_once())                      # flags b with a NEED body (threshold 0)
    rep = mod.report(tmp_path / "d" / "control.sqlite", time.time() - 3600, {"mbos": "s-1"})
    assert rep["tasks"]["by_status"] == {"DONE": 1, "BLOCKED": 1} and rep["tasks"]["retries"] == 1 and rep["tasks"]["verified_ids"] == ["t-1"]
    ins = rep["instructions"]
    assert ins["by_project"] == {"mbos": 2} and ins["ack_latency"]["measured"] == 1 and ins["ack_latency"]["within_300s"] == 1
    assert ins["flagged_unacknowledged"] == ["da-bbbbbbbbbb"] and ins["need_receipts"] and ins["need_receipts"][0]["class"] == "NEED_HOST_ACTION"
    assert ins["misroutes_to_other_pinned_session"] == [] and ins["standin_deliveries"] == []
    assert rep["owner"]["decisions_asked"] == 0 and rep["activity"]["events"] >= 4
    assert rep["paid"] == {"calls": 0, "usd": 0.0, "authority": "paid_ledger"} and "cost" not in rep


def test_standin_is_reported_misroute_is_another_projects_pin_and_paid_comes_from_the_ledger(tmp_path):
    """The designed fallback (pinned name gone, one eligible session in the project directory) is a stand-in, not a
    misroute; a misroute is delivery to a session some OTHER project pinned. Paid spend is the ledger; the costs
    table is an estimate and is labelled so."""
    mod = _load()
    s = _service(tmp_path)
    from desktop_agent.control.intake import Intake, IntakeSource
    src = IntakeSource(project="mbos", repo="x/y", issues=[1], coordinator={"kind": "claude_peer", "session_name": "s-1"})
    class Deliver:
        def session_for(self, src): return None
        async def __call__(self, src, item): return {"ok": False, "note": "x", "evidence": []}
    it = Intake(s.store, [src], Deliver(), poll_sec=1, unacked_after_sec=0)
    base = {"project": "mbos", "repo": "x/y", "issue": 1, "kind": "comment", "author": "o", "title": "t", "body": "b", "url": "u",
            "posted_at": "2026-10-11T00:00:00Z", "status": "POSTED", "coordinator": "claude_peer"}
    a = {**base, "item_id": "da-aaaaaaaaaa", "gh_id": 1}; s.store.save_intake(a)
    it._set(a, "DELIVERED", "peer message to session s-standin (pid 2, cwd /w/mbos; pinned name s-1 not live, resolved by cwd (prefix))", ["x"])
    b = {**base, "item_id": "da-bbbbbbbbbb", "gh_id": 2}; s.store.save_intake(b)
    it._set(b, "DELIVERED", "peer message to session s-other (pid 3, cwd /w/other; pinned name s-1 not live, resolved by cwd (prefix))", ["x"])
    c = {**base, "item_id": "da-cccccccccc", "gh_id": 3}; s.store.save_intake(c)
    it._set(c, "DELIVERED", "peer message to session s-1 (pid 1, cwd /w/mbos)", ["x"])
    s.store.add_cost("t1", "w", "claude-haiku-5-5", 0.25, 10, 5)                # subscription relay, priced as an estimate
    assert s.store.paid_reserve("call-1", "2026-10-12", "openai", "gpt-x", 0.5, 5.0, "test")[0]
    rep = mod.report(tmp_path / "d" / "control.sqlite", time.time() - 3600, {"mbos": "s-1", "other": "s-other"})
    ins = rep["instructions"]
    assert [m["item_id"] for m in ins["misroutes_to_other_pinned_session"]] == ["da-bbbbbbbbbb"] and ins["misroutes_to_other_pinned_session"][0]["pinned_by"] == "other"
    assert [m["item_id"] for m in ins["standin_deliveries"]] == ["da-aaaaaaaaaa"] and ins["standin_deliveries"][0]["delivered_to"] == "s-standin"
    assert rep["paid"] == {"calls": 1, "usd": 0.5, "authority": "paid_ledger"}
    assert rep["estimates"]["label"] == "estimate; not billed" and rep["estimates"]["usd_total"] == 0.25
