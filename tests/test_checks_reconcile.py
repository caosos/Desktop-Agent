"""DA-STALE-CHECK-CLOSURE-001 (da-213f9988f5): obsolete timers are retired once, evidence stays, restart/replay cannot revive them,
and a genuinely open check still produces the missed-check exception."""
import asyncio

from desktop_agent.control.checks import Checks
from desktop_agent.control.checks_reconcile import reconcile
from tests.test_checks import Registry, at, build, receipt


def other(store, item, iid):
    store.save_intake({**item, "item_id": iid, "gh_id": hash(iid) % 100000})
    return {**item, "item_id": iid}


def arm(chk, item, text, when="2026-10-11T19:31:00Z"):
    return chk.observe(item, "WORKING", text, when, "https://x/c/" + item["item_id"])


def test_acks_in_the_same_comment_are_superseded_by_the_tasks_own_single_timer(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    parent = other(store, item, "da-1111111111"); a = other(store, item, "da-2222222222"); b = other(store, item, "da-3333333333")
    text = receipt(task="da-1111111111 passive screen and voice", next_check_at="2026-10-11T20:00:00-05:00", eta="2026-10-11T21:00:00-05:00")
    for it in (parent, a, b): arm(chk, it, text)
    assert sorted(reconcile(chk)) == ["da-2222222222", "da-3333333333"]
    assert [chk.get(i)["state"] for i in ("da-1111111111", "da-2222222222", "da-3333333333")] == ["ARMED", "SUPERSEDED", "SUPERSEDED"]
    assert reconcile(chk) == []                                                                              # idempotent: a second pass retires nothing


def test_owner_wait_is_parked_not_alarmed_and_a_newer_receipt_rearms_it(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    arm(chk, item, receipt(blocker="NEED_OWNER for the microphone observation", next_check_at="2026-10-11T19:40:00-05:00", eta="2026-10-11T19:50:00-05:00"))
    assert reconcile(chk) == [item["item_id"]] and chk.get(item["item_id"])["state"] == "WAITING_OWNER"
    clock.t = at("2026-10-11T19:45:00-05:00"); assert asyncio.run(chk.tick()) == []
    arm(chk, item, receipt(blocker="NONE", next_check_at="2026-10-11T20:30:00-05:00", eta="2026-10-11T21:00:00-05:00"), when="2026-10-11T19:46:00Z")
    assert chk.get(item["item_id"])["state"] == "ARMED"                                                      # work resumed: the timer is live again


def test_restart_and_replay_do_not_revive_or_repeat_retirements(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    arm(chk, item, receipt(blocker="NEED_OWNER x", next_check_at="2026-10-11T19:40:00-05:00", eta="2026-10-11T19:50:00-05:00"))
    reconcile(chk); n = len(store.receipts("check", item["item_id"]))
    arm(chk, item, receipt(blocker="NEED_OWNER x", next_check_at="2026-10-11T19:40:00-05:00", eta="2026-10-11T19:50:00-05:00"))     # the same comment read again
    chk2 = Checks(store, chk.intake, Registry(), clock=clock, repo_dir=str(tmp_path), checklist_path=chk.checklist_path)               # service restart
    assert reconcile(chk2) == [] and chk2.get(item["item_id"])["state"] == "WAITING_OWNER" and len(store.receipts("check", item["item_id"])) == n


def test_a_genuinely_open_check_still_raises_the_missed_check_exception(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    arm(chk, item, receipt(next_check_at="2026-10-11T19:40:00-05:00", eta="2026-10-11T19:50:00-05:00"))
    assert reconcile(chk) == []
    clock.t = at("2026-10-11T19:41:00-05:00")
    out = asyncio.run(chk.tick())
    assert len(out) == 1 and chk.get(item["item_id"])["fired"] and "EXCEPTION (missed_check)" in gh.posted[0]


def test_an_owner_wait_receipt_with_eta_unknown_is_accepted_and_parked_instead_of_rejected(tmp_path):
    store, gh, clock, chk, item = build(tmp_path)
    arm(chk, item, receipt(blocker="NEED_OWNER — Michael's microphone", eta="UNKNOWN until Michael tests", eta_basis="M1 needs his real microphone",
                           next_check_at="2026-10-11T20:30:00-05:00"))
    assert chk.get(item["item_id"])["state"] == "ARMED"                    # not rejected for the ETA schema
    assert reconcile(chk) == [item["item_id"]] and chk.get(item["item_id"])["state"] == "WAITING_OWNER"
    clock.t = at("2026-10-11T21:00:00-05:00"); assert asyncio.run(chk.tick()) == []
    # without NEED_OWNER an UNKNOWN eta is still invalid
    store2, gh2, clock2, chk2, item2 = build(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    assert arm(chk2, item2, receipt(eta="UNKNOWN", blocker="NONE")) is None
