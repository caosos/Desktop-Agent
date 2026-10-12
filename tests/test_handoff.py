"""Automatic DONE → next eligible handoff (owner directive da-17eaaa1109): selection, exactly-once delivery, restart safety,
fail-closed refusals, project isolation, one bounded item at a time."""
import asyncio
from pathlib import Path

import pytest

from desktop_agent.control import intake as intake_mod
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.handoff import Handoff, parse_queue, select_next
from desktop_agent.control.intake import Intake, IntakeSource, item_id_for
from desktop_agent.control.store import Store
from tests.test_intake import FakeGH, comment

HEAD = "| Outcome | Priority | Dependencies | Responsible worker | State | Concrete acceptance test | Latest evidence | Next action |\n|---|---|---|---|---|---|---|---|\n"


def row(title, prio="P0", deps="none", resp="existing coordinator `caoscare-1-8c`", state="QUEUED — ETA later", acc="acc", ev="ev", nxt="go"):
    return f"| {title} | {prio} | {deps} | {resp} | {state} | {acc} | {ev} | {nxt} |\n"


class Rig:
    def __init__(self, tmp_path: Path, queue: str, handoff=True, cfg=None):
        self.store = Store(tmp_path); self.gh = FakeGH(); self.delivered = []
        self.checklist = tmp_path / "BUILD_CHECKLIST.md"; self.checklist.write_text(queue)
        async def deliver(src, item):
            self.delivered.append(item["item_id"]); return {"ok": True, "status": "DELIVERED", "note": "peer message to session s", "evidence": ["session=s"]}
        intake_mod.gh_api = self.gh
        self.src = IntakeSource(project="desktop_agent", repo="x/y", issues=[117], coordinator={"kind": "claude_peer", "session_cwd": "/nowhere"}, handoff=handoff)
        self.cfg = cfg
        self.build(deliver)
        self.iid = item_id_for("x/y", 117, "issue", 9001)
        asyncio.run(self.intake.poll_once())                       # ingest the issue body as an instruction

    def build(self, deliver=None):
        self.deliver = deliver or self.deliver
        self.intake = Intake(self.store, [self.src], self.deliver, poll_sec=1)
        self.handoff = Handoff(self.store, self.intake, lambda project: self.checklist, self.cfg)
        self.intake.handoff = self.handoff

    def done(self, cid=7001, iid=None):
        self.gh.comments.append(comment(cid, "o", f"<!-- da:coordinator -->\nDONE {iid or self.iid}\nfinished", f"2026-10-12T01:{cid % 100:02d}:00Z"))
        return asyncio.run(self.intake.poll_once())

    def refusals(self): return [e.payload["reason"] for e in self.store.events() if e.payload.get("action") == "handoff_refused"]
    def handoffs(self): return [i for i in self.store.list_intake() if i["kind"] == "handoff"]
    def handoff_deliveries(self): return [d for d in self.delivered if d in {h["item_id"] for h in self.handoffs()}]


QUEUE = HEAD + row("Item one is running", state="WORKING — claimed") + row("Next eligible row", state="QUEUED — ETA 21:00") + row("Later row", prio="P1", state="QUEUED")


# ---- selection and exactly-once delivery ---------------------------------------------------------------------------------
def test_done_delivers_the_next_eligible_row_exactly_once_and_survives_a_restart(tmp_path):
    r = Rig(tmp_path, QUEUE)
    r.done()
    hs = r.handoffs()
    assert len(hs) == 1 and hs[0]["status"] == "DELIVERED" and r.handoff_deliveries() == [hs[0]["item_id"]] and hs[0]["author"] == "control"
    assert "Next eligible row" in hs[0]["body"] and f"ACK {hs[0]['item_id']}" in hs[0]["body"] and "Later row" not in hs[0]["body"]
    assert any(e.payload.get("action") == "intake_status" and e.payload.get("status") == "DELIVERED" for e in r.store.events())
    asyncio.run(r.intake.poll_once()); asyncio.run(r.intake.poll_once())                         # the same DONE comment is seen again
    assert len(r.handoffs()) == 1 and len(r.handoff_deliveries()) == 1
    r.build()                                                                                     # service restart: new objects over the same store
    asyncio.run(r.intake.poll_once())
    assert len(r.handoffs()) == 1 and len(r.handoff_deliveries()) == 1
    assert asyncio.run(r.handoff.on_done(r.src, r.store.get_intake(r.iid)))["duplicate"] is True    # a replayed completion hands off nothing more
    assert len(r.handoff_deliveries()) == 1


def test_priority_beats_table_order_and_a_refused_first_row_is_never_skipped():
    rows = parse_queue(HEAD + row("P1 listed first", prio="P1") + row("P0 listed second", prio="P0"))
    assert select_next(rows)[0]["title"] == "P0 listed second"
    rows = parse_queue(HEAD + row("Owner gated first", prio="P0", deps="Michael names the host") + row("Eligible but lower", prio="P1"))
    chosen, why = select_next(rows)
    assert chosen is None and why.startswith("owner-gated")                                      # fail closed: the next row is refused, not skipped


# ---- fail closed ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("rows,reason", [
    (row("Only running", state="WORKING"), "no READY or QUEUED row"),
    (row("Blocked one", state="BLOCKED — owner window"), "no READY or QUEUED row"),
    (row("Gated by owner", deps="owner decision d-1"), "owner-gated"),
    (row("Waiting", nxt="needs Michael to name the host"), "owner-gated"),
    (row("Other project's work", resp="CAOSCare coordinator"), "outside Desktop-Agent"),
    (row("Deal Sniffer follow-up"), "outside Desktop-Agent"),
    (row("Assigned to the owner", resp="Michael"), "not assigned to the coordinator"),
    (row("No priority", prio="soon"), "ambiguous: a READY/QUEUED row has no parseable priority"),
    (row("Twin") + row("Twin"), "ambiguous: two READY/QUEUED rows share the same title"),
    (row("Needs another row", deps="the audit row finishing first"), "dependency not verifiably met"),
])
def test_unsafe_rows_are_refused_with_a_recorded_reason_and_nothing_is_delivered(tmp_path, rows, reason):
    r = Rig(tmp_path, HEAD + rows)
    r.done()
    assert r.handoffs() == [] and r.handoff_deliveries() == [] and len(r.refusals()) == 1 and reason in r.refusals()[0], r.refusals()
    receipts = [x for x in r.store.receipts(subject_type="instruction", subject_id=r.iid) if "handoff refused" in x["claim"]]
    assert receipts and "fail closed" in receipts[-1]["claim"]


def test_a_met_dependency_is_accepted(tmp_path):
    r = Rig(tmp_path, HEAD + row("The audit row", state="DONE — loaded") + row("Follows the audit", deps="The audit row done"))
    r.done()
    assert len(r.handoffs()) == 1 and "Follows the audit" in r.handoffs()[0]["body"]


def test_subscription_usage_never_refuses_a_handoff(tmp_path):
    """The relay is a subscription call, not paid API spend: a large token-equivalent figure in the usage table is not a budget."""
    cfg = RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t")
    r = Rig(tmp_path, QUEUE, cfg=cfg)
    r.store.add_cost(None, None, "m", 500.0, 1, 1)
    r.done()
    assert len(r.handoffs()) == 1 and r.refusals() == []


def test_unreadable_queue_and_an_open_earlier_handoff_are_refused(tmp_path):
    r = Rig(tmp_path, QUEUE)
    r.checklist.unlink(); r.done()
    assert "not readable" in r.refusals()[0] and r.handoffs() == []
    r2 = Rig(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, QUEUE)
    r2.done(7001)                                                                                 # first handoff delivered and still open
    assert len(r2.handoffs()) == 1
    other = {"item_id": "da-cccccccccc", "project": "desktop_agent", "repo": "x/y", "issue": 117, "kind": "comment", "gh_id": 5, "author": "o", "title": "other", "body": "b",
             "url": "u", "posted_at": "2026-10-12T01:00:00Z", "status": "WORKING", "coordinator": "claude_peer"}
    r2.store.save_intake(other); r2.done(7002, iid="da-cccccccccc")
    assert len(r2.handoffs()) == 1 and "earlier handoff is still open" in r2.refusals()[-1]


# ---- isolation and chaining ---------------------------------------------------------------------------------------------------
def test_projects_that_do_not_opt_in_never_hand_off_and_each_project_reads_only_its_own_queue(tmp_path):
    r = Rig(tmp_path, QUEUE, handoff=False)
    r.done()
    assert r.handoffs() == [] and r.handoff_deliveries() == [] and r.refusals() == []                       # not enabled: nothing happens, nothing is read
    seen = []
    r.src.handoff = True; r.handoff.checklist_for = lambda project: (seen.append(project), r.checklist)[1]
    r.done(7002, iid=item_id_for("x/y", 117, "issue", 9001))
    assert set(seen) <= {"desktop_agent"}


def test_a_handoff_item_that_reaches_done_hands_off_the_following_row(tmp_path):
    r = Rig(tmp_path, QUEUE)
    r.done()
    first = r.handoffs()[0]
    r.checklist.write_text(HEAD + row("Item one is running", state="DONE") + row("Next eligible row", state="WORKING — claimed") + row("Later row", prio="P1", state="QUEUED"))
    r.done(7002, iid=first["item_id"])
    ids = [h["item_id"] for h in r.handoffs()]
    assert len(ids) == 2 and "Later row" in r.store.get_intake(ids[1])["body"] and len(r.handoff_deliveries()) == 2
