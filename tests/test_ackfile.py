"""Liaison ack files are read by content SHA, not by name (owner gap da-cb0120c765): an amended file is
read again and each reading is one receipt; an unchanged SHA is not fetched and writes nothing; BLOCKED
moves the item to BLOCKED; COMPLETED is 'coordination-completed' and NEVER product DONE; the row keeps
published / acknowledged / blocked / coordination-completed / implementation-result / live as separate
facts, unknown stays unknown. No network, no model."""
import asyncio
import base64
import time
from pathlib import Path

from desktop_agent.control import intake as intake_mod, watchdog as wd_mod
from desktop_agent.control.ackfile import next_action, parse_ack
from desktop_agent.control.intake import Intake, IntakeSource
from desktop_agent.control.store import Store
from desktop_agent.control.watchdog import Watchdog
from tests.test_watchdog import FakeDeliverer, FakeScheduler

F51_COMPLETED = """# ACK: ARYA-20261010-0433-f51-scope-handoff

- **Message:** `origin/liaison/aria-to-agent-01:docs/messages/inbox/ARYA-20261010-0433-f51-scope-handoff.md`
- **Stage:** COMPLETED for the coordinator-owned handoff (2026-10-10 ~04:50Z). The AMENDMENT ITSELF IS NOT IMPLEMENTED: the running F-51 worker did NOT read it.
- **Acked by:** automatic pickup at 2026-10-10T04:33:22Z (received only). **Resolved by the owning interactive Agent 01**, 2026-10-10T04:4xZ.
- **Done by the coordinator (evidence):**
  - F-51 row in `docs/status/READY_QUEUE.md` now carries the AMENDMENT; commit 3f9c2ab1
  - Handoff note `docs/handoff/F-51-amendment.md`; F-52 queued as the follow-on safety net.
- **NOT proven:** that the running F-51 worker has read the amendment.
- **Safety:** no live reload, restart of other services, spend, purchase, seller contact, database or worker change.
"""
BLOCKED_AMENDMENT = """# ACK: ARYA-20261010-0433-f51-scope-handoff

- **Stage:** BLOCKED — the F-51 worker exited without the amendment; needs Michael's choice between re-run and manual edit.

## Owner decision(s)
- re-run F-51 with the amendment, or accept the manual edit
"""
RESULT_LINKED = """# ACK: ARYA-20261010-0433-f51-scope-handoff

- **Stage:** COMPLETED (2026-10-10 06:10Z)
- **Done by the coordinator (evidence):**
  - implementation receipt `docs/receipts/2026-10-10-f51-amendment.md`, commit `9ab12cd4e`
  - [PR #61](https://github.com/x/mbos/pull/61) merged to research/agent-01-coordinator
- **Live:** deployment pending — :8766 still serves the previous build; not live.
"""


def test_parse_ack_keeps_facts_separate():
    d = parse_ack(F51_COMPLETED)
    assert d["stage"] == "COMPLETED" and d["category"] == "coordination-completed"
    assert d["not_implemented"] and d["implementation"] == "not implemented (per the ack)" and d["live"] == "not applicable (nothing implemented per the ack)"
    assert d["followups"] == ["F-52"] and d["not_proven"][0].startswith("that the running F-51 worker")
    assert "docs/status/READY_QUEUE.md" in d["links"] and "3f9c2ab1" in d["links"] and "docs/handoff/F-51-amendment.md" in d["links"]
    assert "NOT implemented" in next_action(d, "ACKNOWLEDGED") and "F-52" in next_action(d, "ACKNOWLEDGED") and "unknown until a worker receipt" in next_action(d, "ACKNOWLEDGED")
    b = parse_ack(BLOCKED_AMENDMENT)
    assert b["category"] == "blocked" and b["blocked_reason"].startswith("— the F-51 worker exited") and next_action(b, "BLOCKED").startswith("BLOCKED per its ack")
    r = parse_ack(RESULT_LINKED)
    assert r["category"] == "coordination-completed" and r["implementation"] == "result linked (per the ack)" and r["live"] == "not live (pending)"
    assert "https://github.com/x/mbos/pull/61" in r["links"] and "docs/receipts/2026-10-10-f51-amendment.md" in r["links"] and "9ab12cd4e" in r["links"]
    assert "deployment/live verification still pending" in next_action(r, "ACKNOWLEDGED")
    u = parse_ack("# ACK\nno stage line here\n")
    assert u["stage"] is None and u["category"] == "unknown" and u["implementation"] == "not stated" and u["live"] == "unknown"
    assert "not recognised" in next_action(u, "ACKNOWLEDGED")
    assert parse_ack("- **Stage:** COMPLETED\n- **Verified live:** :8766 serves build 9ab12cd4e\n")["live"] == "verified"


def _harness(tmp_path: Path):
    store = Store(tmp_path); deliv = FakeDeliverer()
    state = {"ack": ("s1", F51_COMPLETED), "content_fetches": 0}
    inbox = [{"name": "ARYA-20261010-0433-f51-scope-handoff.md", "path": "docs/messages/inbox/ARYA-20261010-0433-f51-scope-handoff.md", "sha": "i1", "html_url": "https://x/inbox/f51"}]
    async def gh(path, timeout=60, method="GET", fields=None):
        if "contents/docs/messages/inbox?" in path:
            return inbox
        if "contents/docs/messages/acks?" in path:
            return [{"name": inbox[0]["name"], "sha": state["ack"][0], "html_url": "https://x/acks/f51"}]
        if "contents/docs/messages/inbox/ARYA" in path:
            return {"encoding": "base64", "content": base64.b64encode(b"# F-51 scope handoff\n**Date:** 2026-10-10\nbody").decode()}
        if "contents/docs/messages/acks/ARYA" in path:
            state["content_fetches"] += 1
            return {"encoding": "base64", "sha": state["ack"][0], "html_url": "https://x/acks/f51", "content": base64.b64encode(state["ack"][1].encode()).decode()}
        if path == "repos/x/mbos/contents/docs/handoff/F-51-amendment.md?ref=research/c":
            return {"html_url": "https://github.com/x/mbos/blob/research/c/docs/handoff/F-51-amendment.md", "sha": "h1"}
        if path == "repos/x/mbos/commits/9ab12cd4e":
            return {"html_url": "https://github.com/x/mbos/commit/9ab12cd4e0000", "sha": "9ab12cd4e0000"}
        raise AssertionError(path)
    intake_mod.gh_api = gh; wd_mod.gh_api = gh
    src = IntakeSource(project="mbos", repo="x/mbos", issues=[], coordinator={"kind": "liaison", "liaison": {"branch": "liaison/x", "inbox_dir": "docs/messages/inbox", "ack_branch": "research/c", "ack_dir": "docs/messages/acks"}})
    it = Intake(store, [src], deliv, poll_sec=1)
    wd = Watchdog(store, it, deliv, FakeScheduler()); it.watchdog = wd
    return store, it, state


def _ackfile_receipts(store, item_id):
    return [r for r in store.receipts("instruction", item_id) if str(r.get("claim", "")).startswith("ACKFILE")]


def test_sha_aware_disposition_tracking(tmp_path: Path):
    store, it, state = _harness(tmp_path)
    asyncio.run(it.poll_once())
    item = store.list_intake()[0]; iid = item["item_id"]
    kv = store.get_kv("liaison_ack:" + iid)
    assert item["status"] == "ACKNOWLEDGED" and kv["sha"] == "s1" and kv["category"] == "coordination-completed" and kv["not_implemented"]
    assert state["content_fetches"] == 1 and len(_ackfile_receipts(store, iid)) == 1
    assert _ackfile_receipts(store, iid)[0]["claim"].startswith("ACKFILE COMPLETED: coordination-completed")
    # COMPLETED is never product DONE
    assert store.get_intake(iid)["status"] != "DONE"
    # unchanged SHA: no fetch, no duplicate receipt, nothing rewritten
    asyncio.run(it.poll_once()); asyncio.run(it.poll_once())
    assert state["content_fetches"] == 1 and len(_ackfile_receipts(store, iid)) == 1 and store.get_kv("liaison_ack:" + iid)["observed_at"] == kv["observed_at"]
    # same filename, changed SHA, BLOCKED amendment: read again, item BLOCKED with the reason, gate captured once
    state["ack"] = ("s2", BLOCKED_AMENDMENT)
    asyncio.run(it.poll_once())
    kv2 = store.get_kv("liaison_ack:" + iid); item = store.get_intake(iid)
    assert state["content_fetches"] == 2 and kv2["sha"] == "s2" and kv2["category"] == "blocked" and item["status"] == "BLOCKED"
    assert "F-51 worker exited" in item["note"] and len(_ackfile_receipts(store, iid)) == 2 and [h["sha"] for h in kv2["history"]] == ["s1", "s2"]
    gates = store.get_kv("gates:mbos")
    assert [g["gate"] for g in gates["gates"]] == ["- re-run F-51 with the amendment, or accept the manual edit"] and gates["scanned"][-1].endswith("@s2")
    asyncio.run(it.poll_once())
    assert len(store.get_kv("gates:mbos")["gates"]) == 1 and state["content_fetches"] == 2            # not re-scanned, not duplicated
    # third edit: COMPLETED with a linked implementation result whose deployment is still pending
    state["ack"] = ("s3", RESULT_LINKED)
    asyncio.run(it.poll_once())
    kv3 = store.get_kv("liaison_ack:" + iid); item = store.get_intake(iid)
    assert item["status"] == "ACKNOWLEDGED" and "not product DONE" in item["note"]
    assert kv3["implementation"] == "result linked (per the ack)" and kv3["live"] == "not live (pending)" and "https://github.com/x/mbos/pull/61" in kv3["links"]
    assert len(_ackfile_receipts(store, iid)) == 3 and kv3["first_seen_at"] == kv["first_seen_at"] and len(kv3["history"]) == 3
    assert store.coordinator_rows()["mbos"]["last_ack"] >= kv3["observed_at"] - 1


def test_instruction_row_shows_disposition_facts_and_truthful_next_action(tmp_path: Path):
    from tests.test_drilldown import _service
    s = _service(tmp_path)
    now = time.time()
    s.store.save_intake({"item_id": "da-f51", "project": "mbos", "repo": "x/mbos", "issue": 0, "kind": "liaison", "gh_id": 0, "author": "aria",
                         "title": "F-51 scope handoff", "body": "", "url": "https://x/inbox/f51", "posted_at": "2026-10-10", "status": "ACKNOWLEDGED", "coordinator": "liaison"})
    s.intake._set({"item_id": "da-f51", "url": "https://x/inbox/f51", "project": "mbos", "repo": "x/mbos", "issue": 0, "kind": "liaison"}, "RECEIVED", "liaison message", ["https://x/inbox/f51"])
    s.intake._set({"item_id": "da-f51", "url": "https://x/inbox/f51", "project": "mbos", "repo": "x/mbos", "issue": 0, "kind": "liaison"}, "ACKNOWLEDGED", "ack file", ["research/c:acks/f51.md"])
    d = {**parse_ack(F51_COMPLETED), "sha": "s1abcdef0000", "url": "https://x/acks/f51", "file": "acks/f51.md", "observed_at": now - 30, "first_seen_at": now - 300,
         "history": [{"sha": "s1abcdef0000", "at": now - 30, "stage": "COMPLETED", "category": "coordination-completed"}]}
    s.store.set_kv("liaison_ack:da-f51", d)
    row = next(i for i in s.drill.build("mbos")["instructions"] if i["item_id"] == "da-f51")
    assert row["status"] == "ACKNOWLEDGED" and row["done_at"] is None and row["blocked_at"] is None
    assert row["coordination_completed_at"] == now - 30 and row["acknowledged_at"] and row["delivered_at"]
    assert row["disposition"]["category"] == "coordination-completed" and row["disposition"]["implementation"] == "not implemented (per the ack)" and row["disposition"]["live"].startswith("not applicable")
    assert row["next"].startswith("coordination step completed per its ack; the product change is NOT implemented") and "F-52" in row["next"]
    assert "start it" not in row["next"] and "restart" not in row["next"]


F49_MIDLINE = """# ACK: ARYA-20261010-0328-f49-verification

- **Acked by:** Agent 01, 2026-10-10. **Stage:** COMPLETED (code complete and staging verified; live acceptance pending owner reload gate, see receipt)
- **Safety:** no live reload, restart, spend, purchase, seller contact, database or worker change.
- **Receipt:** docs/receipts/2026-10-10-f49-verification.md
"""
OLD_DISPOSITION = """# ACK: 2026-10-09-aria-owner-dispatcher-stopped-recover-existing-system

- **Acked by:** Agent 01, 2026-10-09. **Disposition:** INCORPORATED. Recovered through the existing dispatcher: stop cause fixed, not bypassed.
- **Receipt:** commit b2fbd95 (dispatcher --reset-task receipt in var/dispatcher.jsonl)
"""
OLD_NEEDS_OWNER = """# ACK: 2026-10-08-aria-owner-agent-watchdog

- **Classification:** OWNER_INPUT + TASK_REQUEST. **Disposition: INCORPORATED (what is safe) + NEEDS_OWNER_DECISION (the self-wake).** Acked by Agent 01, 2026-10-09.
"""


def test_parse_ack_real_formats():
    f49 = parse_ack(F49_MIDLINE)
    assert f49["stage"] == "COMPLETED" and f49["category"] == "coordination-completed"
    assert f49["implementation"] == "result linked (per the ack)" and "docs/receipts/2026-10-10-f49-verification.md" in f49["links"]
    assert f49["live"] == "not live (pending)" and "deployment/live verification still pending" in next_action(f49, "ACKNOWLEDGED")
    old = parse_ack(OLD_DISPOSITION)
    assert old["stage"] == "INCORPORATED" and old["category"] == "coordination-completed" and "b2fbd95" in old["links"]
    gate = parse_ack(OLD_NEEDS_OWNER)
    assert gate["stage"] == "INCORPORATED+NEEDS_OWNER_DECISION" and gate["category"] == "blocked" and gate["blocked_reason"] == "needs an owner decision (per the ack)"
    assert parse_ack("- **Stage:** SUPERSEDED by ARYA-0610\n")["category"] == "superseded"


def test_concurrent_polls_do_not_duplicate_ack_receipts(tmp_path: Path):
    store, it, state = _harness(tmp_path)
    async def both():
        return await asyncio.gather(it.poll_once(), it.poll_once())
    r1, r2 = asyncio.run(both())
    assert (r1["found"], r2["found"]) in ((1, 0), (0, 1))                                # serialized: the second pass saw nothing new
    iid = store.list_intake()[0]["item_id"]
    assert state["content_fetches"] == 1 and len(_ackfile_receipts(store, iid)) == 1


def test_parser_upgrade_reparses_same_sha_without_duplicate_receipts(tmp_path: Path):
    from desktop_agent.control import ackfile
    store, it, state = _harness(tmp_path)
    asyncio.run(it.poll_once())
    iid = store.list_intake()[0]["item_id"]
    kv = store.get_kv("liaison_ack:" + iid)
    assert kv["parser_version"] == ackfile.PARSER_VERSION and len(_ackfile_receipts(store, iid)) == 1
    # simulate an older stored reading (same sha, older parser): re-read once, in place, no receipt when the verdict is unchanged
    store.set_kv("liaison_ack:" + iid, {**kv, "parser_version": kv["parser_version"] - 1})
    asyncio.run(it.poll_once())
    kv2 = store.get_kv("liaison_ack:" + iid)
    assert state["content_fetches"] == 2 and kv2["parser_version"] == ackfile.PARSER_VERSION and kv2.get("reparsed_at") and len(kv2["history"]) == 1
    assert len(_ackfile_receipts(store, iid)) == 1
    asyncio.run(it.poll_once()); assert state["content_fetches"] == 2
    # an older reading whose verdict changes under the new parser gets one receipt
    store.set_kv("liaison_ack:" + iid, {**kv2, "parser_version": 0, "stage": None, "category": "unknown"})
    asyncio.run(it.poll_once())
    assert len(_ackfile_receipts(store, iid)) == 2 and "re-read with parser" in _ackfile_receipts(store, iid)[-1]["claim"]


GATE_LIVE_APPROVAL = """# ACK: ARYA-20261010-0433-f51-scope-handoff

- **Stage:** COMPLETED (code complete; staging verified)
- **Done by the coordinator (evidence):**
  - commit `9ab12cd4e`, handoff `docs/handoff/F-51-amendment.md`

## Owner decision(s)
- approve the live reload of :8766 with build 9ab12cd4e (nothing goes live until you say so)
"""
UNRECOGNISED_EDIT = """# ACK: ARYA-20261010-0433-f51-scope-handoff

- **Acked by:** Agent 01. Notes updated; see the receipt for details.
"""
SUPERSEDED = """# ACK: ARYA-20261010-0433-f51-scope-handoff

- **Stage:** SUPERSEDED by ARYA-20261010-0610 (the F-52 plan replaces this handoff)
"""


def test_blocked_is_retained_on_unrecognised_edit_and_lifted_only_by_explicit_disposition(tmp_path: Path):
    store, it, state = _harness(tmp_path)
    state["ack"] = ("b1", BLOCKED_AMENDMENT); asyncio.run(it.poll_once())
    iid = store.list_intake()[0]["item_id"]
    assert store.get_intake(iid)["status"] == "BLOCKED"
    # a later edit that is not a recognised disposition: the blocker stands, the observation is kept
    state["ack"] = ("u1", UNRECOGNISED_EDIT); asyncio.run(it.poll_once())
    kv = store.get_kv("liaison_ack:" + iid); item = store.get_intake(iid)
    assert item["status"] == "BLOCKED" and "F-51 worker exited" in item["note"]
    assert kv["category"] == "unknown" and kv["retained_block"]["since_sha"] == "u1" and [h["sha"] for h in kv["history"]] == ["b1", "u1"]
    assert len(_ackfile_receipts(store, iid)) == 2 and "UNRECOGNISED" in _ackfile_receipts(store, iid)[-1]["claim"]
    assert next_action(kv, "BLOCKED").startswith("BLOCKED (retained)") and "sha u1" in next_action(kv, "BLOCKED")
    # replayed / unchanged SHA: nothing happens
    asyncio.run(it.poll_once()); asyncio.run(it.poll_once())
    assert state["content_fetches"] == 2 and len(_ackfile_receipts(store, iid)) == 2 and store.get_intake(iid)["status"] == "BLOCKED"
    # an explicit recognised disposition lifts it
    state["ack"] = ("c1", RESULT_LINKED); asyncio.run(it.poll_once())
    item = store.get_intake(iid); kv = store.get_kv("liaison_ack:" + iid)
    assert item["status"] == "ACKNOWLEDGED" and "lifted by explicit COMPLETED" in item["note"] and "retained_block" not in kv
    # BLOCKED again, then an explicit SUPERSEDED lifts it too
    state["ack"] = ("b2", BLOCKED_AMENDMENT); asyncio.run(it.poll_once()); assert store.get_intake(iid)["status"] == "BLOCKED"
    state["ack"] = ("s9", SUPERSEDED); asyncio.run(it.poll_once())
    assert store.get_intake(iid)["status"] == "ACKNOWLEDGED" and "lifted by explicit SUPERSEDED" in store.get_intake(iid)["note"]
    assert "SUPERSEDED per its ack" in next_action(store.get_kv("liaison_ack:" + iid), "ACKNOWLEDGED")


def test_gates_current_versus_historical_and_disappearance_is_not_approval(tmp_path: Path):
    store, it, state = _harness(tmp_path)
    state["ack"] = ("g1", GATE_LIVE_APPROVAL); asyncio.run(it.poll_once())
    iid = store.list_intake()[0]["item_id"]
    g = store.get_kv("gates:mbos")
    assert [x["gate"] for x in g["gates"]] == ["- approve the live reload of :8766 with build 9ab12cd4e (nothing goes live until you say so)"]
    assert g["gates"][0]["sha"] == "g1" and g["gates"][0]["source"] == "docs/messages/acks/ARYA-20261010-0433-f51-scope-handoff.md" and g.get("history", []) == []
    # code is complete, yet the live-approval gate stays open and the row says so
    kv = store.get_kv("liaison_ack:" + iid)
    assert kv["category"] == "coordination-completed" and kv["implementation"] == "result linked (per the ack)"
    na = next_action(kv, "ACKNOWLEDGED", [x["gate"] for x in g["gates"]])
    assert "owner gate still open per its ack: - approve the live reload" in na and "done" not in na.lower()
    # replayed SHA: gates untouched, no duplicates
    asyncio.run(it.poll_once()); assert len(store.get_kv("gates:mbos")["gates"]) == 1 and state["content_fetches"] == 1
    # the gate disappears from the next reading: retired to history, explicitly not an approval
    state["ack"] = ("g2", RESULT_LINKED); asyncio.run(it.poll_once())
    g2 = store.get_kv("gates:mbos")
    assert g2["gates"] == [] and len(g2["history"]) == 1 and g2["history"][0]["retired_by"] == "no longer present in docs/messages/acks/ARYA-20261010-0433-f51-scope-handoff.md@g2"
    assert "not an answer" in g2["history"][0]["approval"] and g2["history"][0]["sha"] == "g1"
    # a gate retired by an explicit SUPERSEDED disposition
    state["ack"] = ("g3", GATE_LIVE_APPROVAL); asyncio.run(it.poll_once()); assert len(store.get_kv("gates:mbos")["gates"]) == 1
    state["ack"] = ("g4", SUPERSEDED); asyncio.run(it.poll_once())
    g4 = store.get_kv("gates:mbos")
    assert g4["gates"] == [] and g4["history"][-1]["retired_by"].startswith("explicit SUPERSEDED disposition") and "nothing was authorised" in g4["history"][-1]["approval"]


def test_links_resolved_only_against_the_verified_branch(tmp_path: Path):
    store, it, state = _harness(tmp_path)
    state["ack"] = ("l1", GATE_LIVE_APPROVAL); asyncio.run(it.poll_once())
    iid = store.list_intake()[0]["item_id"]
    lr = {l["text"]: l for l in store.get_kv("liaison_ack:" + iid)["links_resolved"]}
    assert lr["docs/handoff/F-51-amendment.md"]["verified"] and lr["docs/handoff/F-51-amendment.md"]["url"].endswith("/blob/research/c/docs/handoff/F-51-amendment.md")
    assert lr["9ab12cd4e"]["verified"] and lr["9ab12cd4e"]["url"].endswith("/commit/9ab12cd4e0000") and lr["9ab12cd4e"]["kind"] == "commit"
    state["ack"] = ("l2", RESULT_LINKED); asyncio.run(it.poll_once())
    lr = {l["text"]: l for l in store.get_kv("liaison_ack:" + iid)["links_resolved"]}
    assert lr["docs/receipts/2026-10-10-f51-amendment.md"]["verified"] is False and lr["docs/receipts/2026-10-10-f51-amendment.md"]["url"] is None   # not on the branch: no link invented
    assert lr["https://github.com/x/mbos/pull/61"]["kind"] == "external" and lr["https://github.com/x/mbos/pull/61"]["url"] == "https://github.com/x/mbos/pull/61"
    assert store.get_kv("linkres:x/mbos:research/c:9ab12cd4e")["verified"]                                                 # verified resolutions are cached


def test_packet_separates_current_and_historical_gates(tmp_path: Path):
    from tests.test_drilldown import _service
    s = _service(tmp_path)
    s.store.set_kv("gates:michael_business_os", {"gates": [{"gate": "approve the live reload", "source": "acks/f51.md", "url": "u", "sha": "g1"}],
                                                 "history": [{"gate": "old question", "source": "acks/f40.md", "sha": "a1", "retired_at": time.time(), "retired_by": "no longer present in acks/f40.md@a2",
                                                              "approval": "none — disappearance is not an answer; nothing was authorised"}], "scanned": []})
    pk = s.approval_packet()
    assert [g["gate"] for g in pk["external_gates"] if g["project"] == "michael_business_os"] == ["approve the live reload"]
    assert pk["historical_gates"][0]["gate"] == "old question" and "not an answer" in pk["historical_gates"][0]["approval"]


def test_gates_recorded_before_sha_tracking_are_reconciled_on_reread(tmp_path: Path):
    from desktop_agent.control import ackfile
    store, it, state = _harness(tmp_path)
    state["ack"] = ("g1", GATE_LIVE_APPROVAL); asyncio.run(it.poll_once())
    iid = store.list_intake()[0]["item_id"]
    g = store.get_kv("gates:mbos"); src = g["gates"][0]["source"]
    # simulate the pre-SHA store: a gate without sha for this source, plus one that the file no longer contains
    store.set_kv("gates:mbos", {"gates": [{"gate": g["gates"][0]["gate"], "source": src, "url": "u"}, {"gate": "- an older question", "source": src, "url": "u"}],
                                "history": [], "scanned": [f"ARYA-20261010-0433-f51-scope-handoff.md@g1"]})
    store.set_kv("liaison_ack:" + iid, {**store.get_kv("liaison_ack:" + iid), "parser_version": ackfile.PARSER_VERSION - 1})
    asyncio.run(it.poll_once())
    g2 = store.get_kv("gates:mbos")
    assert [x.get("sha") for x in g2["gates"]] == ["g1"] and g2["history"][0]["gate"] == "- an older question" and "not an answer" in g2["history"][0]["approval"]
