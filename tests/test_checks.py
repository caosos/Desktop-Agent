"""ETA-driven checks (owner operating addendum 2026-10-11): receipt schema, exact timer, the three allowed outcomes, early
DONE, the missed-check exception wording and the notification filter, plus the path from a coordinator's GitHub receipt."""
import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from desktop_agent.control import intake as intake_mod
from desktop_agent.control.checks import RECHECK_SEC, STALE, Checks, next_eligible
from desktop_agent.control.checks_receipt import CHICAGO, REQUIRED, notify_decision, parse_receipt, validate
from desktop_agent.control.intake import IntakeSource, item_id_for
from desktop_agent.control.store import Store
from tests.test_intake import FakeGH, comment, make

CHECKLIST = """| Outcome | Priority | Dependencies | Responsible | State | Acceptance | Evidence | Next |
|---|---|---|---|---|---|---|---|
| Item one is running | P0 | none | me | WORKING — claimed | a | b | c |
| Item two comes next | P0 | one | me | QUEUED — ETA later | a | b | c |
| Item three | P1 | none | me | BLOCKED | a | b | c |
"""


def at(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def receipt(**over) -> str:
    f = {"project": "desktop_agent", "task": "ETA-driven checks", "responsible": "coordinator caoscare-1-8c", "stage": "implementing",
         "evidence": "queue row, watchdog tick, receipts", "eta": "2026-10-11T21:15:00-05:00", "eta_basis": "schema + timer + 6 test groups + gate + one restart",
         "next_check_at": "2026-10-11T20:15:00-05:00", "acceptance": "tests prove schema, timer, outcomes", "blocker": "NONE"}
    f.update(over)
    return "\n".join(f"- {k}: {v}" for k, v in f.items() if v is not None)


class Clock:
    def __init__(self, iso: str): self.t = at(iso)
    def __call__(self): return self.t


class Registry:
    """deliverer.resolve_for: a live session, an absent one, or an unreadable registry."""
    def __init__(self, mode="live"): self.mode = mode
    def resolve_for(self, src):
        if self.mode == "live": return {"name": "caoscare-1-8c", "status": "busy", "pid": 1}, "sole eligible session"
        if self.mode == "absent": return None, "no eligible session at name caoscare-1-8c"
        raise OSError("registry directory unreadable")


def build(tmp_path, mode="live", now="2026-10-11T19:30:00-05:00"):
    store = Store(tmp_path); gh = FakeGH(); clock = Clock(now)
    it = make(store, gh, lambda src, item: None)
    chk = Checks(store, it, Registry(mode), clock=clock, repo_dir=str(tmp_path), checklist_path=tmp_path / "BUILD_CHECKLIST.md")
    (tmp_path / "BUILD_CHECKLIST.md").write_text(CHECKLIST)
    it.checks = chk
    item = {"item_id": "da-aaaaaaaaaa", "project": "caoscare", "repo": "x/y", "issue": 117, "kind": "comment", "gh_id": 1, "author": "o", "title": "t", "body": "b",
            "url": "u", "posted_at": "2026-10-11T19:00:00Z", "status": "WORKING", "coordinator": "claude_peer"}
    store.save_intake(item)
    return store, gh, clock, chk, item


def arm(chk, item, text=None, created="2026-10-12T00:30:00Z", status="WORKING"):
    return chk.observe(item, status, text or receipt(), created, "https://github.com/x/y/issues/117#issuecomment-1")


# ---- 1. schema ---------------------------------------------------------------------------------------------------------
def test_schema_requires_every_field_and_renders_the_exact_chicago_time():
    f = parse_receipt(receipt())
    assert set(f) == set(REQUIRED)
    problems, norm = validate(f, at("2026-10-11T19:30:00-05:00"))
    assert problems == [] and norm["next_check_chicago"] == "2026-10-11 20:15:00 CDT" and norm["eta_chicago"] == "2026-10-11 21:15:00 CDT"
    for k in REQUIRED:                                           # each field is individually required
        missing = {a: b for a, b in f.items() if a != k}
        assert f"missing: {k}" in validate(missing, 0)[0]
    # offsets must be America/Chicago's at that instant: CDT in summer, CST in winter, never UTC or another zone
    assert validate({**f, "next_check_at": "2026-10-12T01:15:00Z"}, 0)[0] and validate({**f, "next_check_at": "2026-10-11T20:15:00-06:00"}, 0)[0]
    assert validate({**f, "next_check_at": "2026-12-15T20:15:00-06:00", "eta": "2026-12-15T21:15:00-06:00"}, 0)[0] == []          # CST is right in December
    assert validate({**f, "next_check_at": "2026-12-15T20:15:00-05:00"}, 0)[0]                                                  # CDT offset in December is wrong
    assert validate({**f, "next_check_at": "tomorrow"}, 0)[0] and validate({**f, "next_check_at": "2026-10-11T20:15:00"}, 0)[0]   # unparsable / naive
    assert validate(f, at("2026-10-11T20:15:00-05:00"))[0] == ["next_check_at: not after the time of this receipt"]
    for generic in ("soon", "TBD", "unknown", "asap", "a bit"):
        assert any("eta_basis" in p for p in validate({**f, "eta_basis": generic}, 0)[0])
    assert NONE_OK(f)


def NONE_OK(f): return validate({**f, "blocker": "NONE"}, 0)[0] == []


# ---- 2. exact timer ------------------------------------------------------------------------------------------------------
def test_timer_fires_exactly_at_next_check_at_and_only_once_per_due_time(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    rec = arm(chk, item)
    assert rec["state"] == "ARMED" and rec["fields"]["next_check_chicago"] == "2026-10-11 20:15:00 CDT"
    due = at("2026-10-11T20:15:00-05:00")
    clock.t = due - 1; assert asyncio.run(chk.tick()) == [] and gh.posted == []             # one second early: nothing
    clock.t = due; first = asyncio.run(chk.tick())
    assert len(first) == 1 and len(gh.posted) == 1 and first[0]["published"] is True
    assert asyncio.run(chk.tick()) == [] and len(gh.posted) == 1                               # the same due time never fires twice
    again = chk.get(item["item_id"])
    assert again["state"] == "ARMED" and again["fields"]["next_check_ts"] == due + RECHECK_SEC and len(again["fired"]) == 1
    clock.t = due + RECHECK_SEC; assert len(asyncio.run(chk.tick())) == 1 and len(gh.posted) == 2   # re-armed check is due again at the new exact time


def test_a_newer_receipt_replaces_the_armed_check_and_the_old_due_time_never_fires(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    arm(chk, item)
    arm(chk, item, receipt(next_check_at="2026-10-11T21:00:00-05:00", stage="testing"), created="2026-10-12T00:50:00Z")
    clock.t = at("2026-10-11T20:15:00-05:00"); assert asyncio.run(chk.tick()) == []          # the replaced time is gone
    assert chk.get(item["item_id"])["fields"]["next_check_chicago"] == "2026-10-11 21:00:00 CDT" and chk.get(item["item_id"])["fields"]["stage"] == "testing"
    arm(chk, item, receipt(next_check_at="2026-10-11T22:00:00-05:00"), created="2026-10-12T00:40:00Z")   # an OLDER receipt seen late does not win
    assert chk.get(item["item_id"])["fields"]["next_check_chicago"] == "2026-10-11 21:00:00 CDT"


def test_invalid_receipt_does_not_arm_and_is_recorded_with_its_problems(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    assert arm(chk, item, receipt(eta_basis="soon", blocker=None)) is None and chk.get(item["item_id"]) is None
    ev = [e for e in store.events() if e.payload.get("action") == "receipt_schema_invalid"]
    assert ev and "missing: blocker" in ev[-1].payload["problems"] and any("eta_basis" in p for p in ev[-1].payload["problems"])


# ---- 3. the three allowed outcomes, exactly one per firing -------------------------------------------------------------------
def heading(body): return next(l for l in body.splitlines() if l.startswith("WATCHDOG CHECK"))


def test_working_outcome_names_evidence_revised_eta_and_new_next_check(tmp_path):
    store, gh, clock, chk, item = build(tmp_path, "live")
    arm(chk, item); clock.t = at("2026-10-11T21:30:00-05:00")                                # ETA (21:15) has also passed
    out = asyncio.run(chk.tick())[0]
    body = gh.posted[0]
    assert out["outcome"] == "WORKING" and heading(body) == f"WATCHDOG CHECK — WORKING — {item['item_id']}"
    assert sum(w in heading(body) for w in ("WORKING", "DONE", "BLOCKED")) == 1
    assert "session caoscare-1-8c busy" in body and "service pid" in body and "repo HEAD" in body and "Revised ETA: UNKNOWN (stated ETA has passed" in body
    assert "new next_check_at: 2026-10-11 21:45:00 CDT" in body and "EXCEPTION (missed_check)" in body and "progress unverified" in body and STALE not in body
    assert out["notify"] is True                                                             # a missed check is notifiable


def test_done_outcome_closes_the_check_and_names_the_next_eligible_row(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    arm(chk, item); store.set_intake_status(item["item_id"], "DONE", "coordinator comment"); clock.t = at("2026-10-11T20:15:00-05:00")
    out = asyncio.run(chk.tick())[0]; body = gh.posted[0]
    assert out["outcome"] == "DONE" and heading(body).endswith(f"DONE — {item['item_id']}") and "next eligible queue row: Item two comes next" in body
    assert chk.get(item["item_id"])["state"] == "CLOSED" and out["notify"] is False and "new next_check_at" not in body
    clock.t += 3600; assert asyncio.run(chk.tick()) == []


def test_blocked_outcome_when_blocked_or_when_the_registry_proves_the_session_absent(tmp_path):
    store, gh, clock, chk, item = build(tmp_path, "live")
    arm(chk, item, receipt(blocker="waiting for the owner to name the SSH host")); store.set_intake_status(item["item_id"], "BLOCKED", "x"); clock.t = at("2026-10-11T20:15:00-05:00")
    out = asyncio.run(chk.tick())[0]
    assert out["outcome"] == "BLOCKED" and "Blocker / NEED: waiting for the owner to name the SSH host" in gh.posted[0] and out["notify"] is True
    store2, gh2, clock2, chk2, item2 = build(tmp_path / "b", "absent") if (tmp_path / "b").mkdir() is None else (None,) * 5
    arm(chk2, item2); clock2.t = at("2026-10-11T20:15:00-05:00")
    out2 = asyncio.run(chk2.tick())[0]
    assert out2["outcome"] == "BLOCKED" and "registry proves the coordinator session absent" in gh2.posted[0] and "NEED_HOST_ACTION" in gh2.posted[0]


# ---- 4. early DONE -------------------------------------------------------------------------------------------------------------
def test_early_done_closes_the_check_at_once_and_names_the_next_item(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    arm(chk, item); clock.t = at("2026-10-11T19:45:00-05:00")                                # well before 20:15
    rec = chk.observe({**item, "status": "DONE"}, "DONE", "DONE da-aaaaaaaaaa", "2026-10-12T00:45:00Z", "https://x/done")
    assert rec["state"] == "CLOSED" and rec["outcomes"][-1]["early"] is True and rec["outcomes"][-1]["next_eligible"].startswith("Item two comes next")
    ev = [e for e in store.events() if e.payload.get("action") == "check_closed"][-1]
    assert ev.payload["early"] is True and ev.payload["notify"] is False
    clock.t = at("2026-10-11T20:30:00-05:00"); assert asyncio.run(chk.tick()) == [] and gh.posted == []     # nothing left to fire
    assert next_eligible(CHECKLIST).startswith("Item two") and next_eligible("| a | b |") == "none READY or QUEUED in the queue table"


# ---- 5. missed-check exception wording ---------------------------------------------------------------------------------------------
def test_missed_check_is_an_exception_and_never_claims_stopped_without_host_proof(tmp_path):
    store, gh, clock, chk, item = build(tmp_path, "unreadable")
    arm(chk, item); clock.t = at("2026-10-11T20:15:00-05:00")
    out = asyncio.run(chk.tick())[0]
    assert out["outcome"] == "WORKING" and out["exception"]["kind"] == "missed_check" and STALE in out["exception"]["text"] and "stopped" not in gh.posted[0].lower()
    ev = [e for e in store.events() if e.payload.get("action") == "check_fired"][-1]
    assert ev.provenance.source == "watchdog:checks" and ev.payload["exception"]["kind"] == "missed_check" and ev.provenance.evidence    # recorded with provenance
    alert = store.get_kv("alert:" + item["item_id"]); assert alert and alert["kind"] == "missed_check"


# ---- 6. notification filter ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind,expected", [
    ("done_needs_owner_acceptance", True), ("need_blocked", True), ("missed_ack", True), ("missed_eta", True), ("missed_check", True),
    ("failed_test", True), ("failed_delivery", True), ("failed_deployment", True), ("failed_recovery", True), ("owner_decision", True), ("cost_or_authority", True),
    ("routine_progress", False), ("build_authority_already_granted", False), ("unchanged_state", False), ("done_no_owner_acceptance", False), ("something_else", False)])
def test_only_the_listed_exceptions_notify_michael(kind, expected):
    notify, reason = notify_decision(kind)
    assert notify is expected and reason


# ---- 7. from a coordinator's GitHub receipt through the intake read-back ---------------------------------------------------------------
def test_a_coordinator_receipt_on_github_arms_the_check_and_a_bare_ack_is_flagged(tmp_path):
    store = Store(tmp_path); gh = FakeGH(); clock = Clock("2026-10-11T19:30:00-05:00")
    async def deliver(src, item): return {"ok": True, "status": "DELIVERED", "note": "peer message", "evidence": ["session=s"]}
    it = make(store, gh, deliver)
    chk = Checks(store, it, Registry("live"), clock=clock, repo_dir=str(tmp_path), checklist_path=tmp_path / "BUILD_CHECKLIST.md"); it.checks = chk
    asyncio.run(it.poll_once())
    iid = item_id_for("x/y", 117, "issue", 9001)
    gh.comments.append(comment(5001, "o", f"<!-- caos:coordinator -->\nACK {iid}\nWORKING {iid}\n\n{receipt()}", "2026-10-12T00:30:00Z"))
    asyncio.run(it.poll_once())
    rec = chk.get(iid)
    assert rec and rec["state"] == "ARMED" and rec["fields"]["next_check_chicago"] == "2026-10-11 20:15:00 CDT" and rec["receipt_url"].endswith("issuecomment-5001")
    gh.comments.append(comment(5002, "o", f"<!-- caos:coordinator -->\nWORKING {iid}\nstill going", "2026-10-12T00:40:00Z"))        # a receipt without the schema
    asyncio.run(it.poll_once())
    assert chk.get(iid)["receipt_ts"] == rec["receipt_ts"]                                                                      # did not re-arm
    assert any(e.payload.get("action") == "receipt_schema_invalid" and "missing: project" in e.payload["problems"] for e in store.events())


def test_alert_reaches_the_owners_needs_rows_and_a_fresh_receipt_clears_it(tmp_path):
    from types import SimpleNamespace
    from desktop_agent.control.work_summary import work_summary
    store, gh, clock, chk, item = build(tmp_path, "unreadable")
    arm(chk, item); clock.t = at("2026-10-11T20:15:00-05:00"); asyncio.run(chk.tick())          # missed check → alert let through the filter
    rows = lambda: work_summary([{"name": "desktop_agent", "workers": {"status": "IDLE"}, "coordinator": {}}], [], [], {"items": []}, SimpleNamespace(paused=False, hold_reason=None), {"free": 1}, store)["needs_owner"]
    assert any("ALERT (missed_check)" in r["text"] and "APPEARS STALE" in r["text"] for r in rows())
    arm(chk, item, receipt(next_check_at="2026-10-11T22:00:00-05:00"), created="2026-10-12T01:20:00Z")   # the coordinator posts a new valid receipt
    assert not any("ALERT" in r["text"] for r in rows())
