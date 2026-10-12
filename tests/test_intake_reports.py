"""DA-CHECKIN-HEADING-LOOP-001 (da-8d1730d062): a canonical check-in log (marker or exact heading) is recorded but never delivered or mirrored;
genuine owner directives are still delivered and genuine work receipts still cross the bridge exactly once."""
import asyncio
from pathlib import Path

from desktop_agent.control import intake as intake_mod
from desktop_agent.control.bridge import Bridge
from desktop_agent.control.intake import Intake, IntakeSource
from desktop_agent.control.intake_reports import is_checkin_report
from desktop_agent.control.store import Store
from tests.test_bridge import SCHEMA
from tests.test_intake import FakeGH, comment


class Rig:
    def __init__(self, tmp_path: Path):
        self.store, self.gh, self.delivered, self.posted = Store(tmp_path), FakeGH(), [], []
        intake_mod.gh_api = self.gh
        async def deliver(src, item): self.delivered.append(item["item_id"]); return {"ok": True, "status": "DELIVERED", "note": "n", "evidence": ["e"]}
        async def post(path, fields): self.posted.append(fields["body"]); return {}
        self.src = IntakeSource(project="desktop_agent", repo="x/y", issues=[117], coordinator={"kind": "claude_peer", "session_cwd": "/n"}, bridge_pr=19)
        self.intake = Intake(self.store, [self.src], deliver, poll_sec=1); self.intake.bridge = Bridge(self.store, post, "abc")
        asyncio.run(self.intake.poll_once()); self.delivered.clear()                       # ingest the issue body, then count only what follows

    def comment(self, cid, text):
        self.gh.comments.append(comment(cid, "caosos", text, f"2026-10-12T01:{cid % 100:02d}:00Z")); asyncio.run(self.intake.poll_once())


def test_recogniser_is_narrow():
    assert is_checkin_report("<!-- da:canonical-checkin source=x -->\n## log") and is_checkin_report("CANONICAL CHECK-IN LOG — Desktop Agent event supervision\n1. x")
    assert is_checkin_report("## Canonical check-in log — Monday") and is_checkin_report("**CANONICAL CHECK-IN LOG**")
    assert is_checkin_report("<!-- desktop-agent-canonical-checkin source=pr19-comment-1 -->\n\n## CANONICAL CHECK-IN — Monday, October 12, 2026 — 6:40 PM")
    assert is_checkin_report("## CANONICAL CHECK-IN — Monday, October 12, 2026") and not is_checkin_report("CANONICAL CHECK-IN REQUIRED: post a receipt now")
    for genuine in ("<!-- da:supervisor -->\nOWNER DIRECTIVE: do the thing", "SUPERVISOR DIRECTIVE — DA-X\nfix it", "please read the canonical check-in log later", ""):
        assert not is_checkin_report(genuine), genuine


def test_a_heading_only_log_is_recorded_not_delivered_and_owes_no_ack(tmp_path):
    r = Rig(tmp_path)
    r.comment(7101, "CANONICAL CHECK-IN LOG — Desktop Agent event supervision\n\n1. Full date and time: x")
    items = [i for i in r.store.list_intake() if i["gh_id"] == 7101]
    assert len(items) == 1 and items[0]["status"] == "DONE" and "not delivered" in items[0]["note"] and r.delivered == [] and r.posted == []
    r.comment(7101, "CANONICAL CHECK-IN LOG — Desktop Agent event supervision\n\n1. Full date and time: x")      # replay
    assert r.delivered == [] and r.posted == []


def test_a_genuine_directive_is_delivered_and_a_genuine_work_receipt_crosses_the_bridge_once(tmp_path):
    r = Rig(tmp_path)
    r.comment(7102, "<!-- da:supervisor -->\nSUPERVISOR DIRECTIVE — DA-X\nfix the thing")
    assert len(r.delivered) == 1 and r.posted == []
    iid = r.delivered[0]
    r.gh.comments.append(comment(7103, "caosos", f"<!-- da:coordinator -->\nWORKING {iid}\n{SCHEMA}", "2026-10-12T01:03:00Z")); asyncio.run(r.intake.poll_once()); asyncio.run(r.intake.poll_once())
    assert len(r.posted) == 1 and r.posted[0].startswith("DESKTOP AGENT RECEIPT: WORKING")
