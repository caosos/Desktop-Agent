"""SIMULATED conversational calling (docs/CALLING_BRIEF.md §7): the loop, idempotency, replay, cancel vs late
result, fixture-only identities, the Ask Aria path, the API, and out-of-order rejection. No network, no provider."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from desktop_agent.control.api import build_app
from desktop_agent.control.calling import SimulatedAdapter
from desktop_agent.control.receipts import chain
from tests.test_ask_aria import FakeLLM, _service

ALIAS = "fixture:front-desk"


def _svc(tmp_path):
    s = _service(tmp_path)
    s.calling.adapter = SimulatedAdapter(delay_sec=0)
    return s


def _trace(s, call_id):
    return [f"{r['result_label']:9} {r['actor']:14} {r['claim']}" for r in chain(s.store, "call", call_id)]


def test_happy_loop_receipts_and_transcript(tmp_path):
    s = _svc(tmp_path)

    async def go():
        rec = s.calling.request("cmd-1", ALIAS, "confirm the fixture appointment", 0, "test")
        await s.calling.wait(rec["call_id"])
        return rec
    rec = asyncio.run(go())
    call = s.calling.get(rec["call_id"])
    assert [h["state"] for h in call["history"]] == ["ACCEPTED", "STARTED", "IN_PROGRESS", "COMPLETED"]
    assert [h["seq"] for h in call["history"]] == [1, 2, 3, 4]
    assert call["state"] == "COMPLETED" and "SIMULATED" in call["result"]["outcome"] and call["simulated"] and call["label"] == "SIMULATED"
    rs = chain(s.store, "call", rec["call_id"])
    assert [r["result_label"] for r in rs] == ["verified", "simulated", "simulated", "simulated", "simulated"] and call["receipts"] == 5
    for i, r in enumerate(rs):
        assert "SIMULATED" in r["claim"]
        assert r["call_id"] == rec["call_id"] and r["command_id"] == "cmd-1" and r["calling_task_id"] == rec["task_id"]
        assert f"call_id={rec['call_id']}" in r["evidence"] and f"command_id=cmd-1" in r["evidence"]
        assert r["parent_receipt_id"] == (rs[i - 1]["receipt_id"] if i else None)
    notes = [t for t in s.aria.transcript() if t.get("kind") == "call (SIMULATED)"]
    assert len(notes) == 2 and all(rec["call_id"] in n["text"] and "SIMULATED" in n["text"] for n in notes)
    print("\nTRACE\n" + "\n".join(_trace(s, rec["call_id"])) + "\n" + "\n".join(n["text"] for n in notes))


def test_duplicate_command_is_one_call(tmp_path):
    s = _svc(tmp_path)

    async def go():
        a = s.calling.request("cmd-dup", ALIAS, "p", None, "test")
        b = s.calling.request("cmd-dup", ALIAS, "p", None, "test")
        await s.calling.wait(a["call_id"])
        return a, b
    a, b = asyncio.run(go())
    assert (a["duplicate"], b["duplicate"]) == (False, True) and a["call_id"] == b["call_id"] and a["task_id"] == b["task_id"]
    assert len(s.calling.list()) == 1 and s.calling.adapter_starts == 1
    assert len(chain(s.store, "call", a["call_id"])) == 5          # no receipt from the duplicate


def test_replayed_seq_is_ignored(tmp_path):
    s = _svc(tmp_path)
    rec = s.calling.request("cmd-r", ALIAS, "p", None, "test")     # no running loop: this test drives apply itself
    assert s.calling.apply(rec["call_id"], 1, "ACCEPTED")["applied"]
    n = len(chain(s.store, "call", rec["call_id"]))
    r = s.calling.apply(rec["call_id"], 1, "ACCEPTED")
    assert r == {"applied": False, "reason": "replayed seq ignored", "state": "ACCEPTED"}
    assert len(chain(s.store, "call", rec["call_id"])) == n and s.calling.get(rec["call_id"])["state"] == "ACCEPTED"


def test_cancel_then_late_result_is_ignored(tmp_path):
    s = _svc(tmp_path)
    rec = s.calling.request("cmd-c", ALIAS, "p", None, "test")
    cid = rec["call_id"]
    s.calling.apply(cid, 1, "ACCEPTED"); s.calling.apply(cid, 2, "STARTED")
    assert s.calling.cancel(cid, "test")["state"] == "CANCELLED"
    n = len(chain(s.store, "call", cid))
    r = s.calling.apply(cid, 4, "COMPLETED", "late", {"outcome": "SIMULATED: completed", "summary": "SIMULATED late"})
    assert r["applied"] is False and r["reason"] == "late result ignored" and r["state"] == "CANCELLED"
    rs = chain(s.store, "call", cid)
    assert len(rs) == n + 1 and "late result ignored" in rs[-1]["claim"]
    call = s.calling.get(cid)
    assert call["state"] == "CANCELLED" and call["result"] is None and len(s.calling.list()) == 1
    texts = " ".join(t["text"] for t in s.aria.transcript())
    assert "cancelled" in texts and "completed" not in texts


def test_non_fixture_destination_is_rejected(tmp_path):
    s = _svc(tmp_path)
    for bad in ("+15555550100", "front-desk", "fixture:", ""):
        with pytest.raises(ValueError):
            s.calling.request(None, bad, "p", None, "test")
    assert s.calling.list() == []
    with TestClient(build_app(s, "tok")) as c:
        r = c.post("/v0/calls", json={"destination_alias": "+15555550100", "purpose": "p"}, headers={"Authorization": "Bearer tok"})
        assert r.status_code == 400
    assert s.calling.list() == []


def test_ask_aria_request_call_path(tmp_path):
    s = _svc(tmp_path)
    s.aria.llm = FakeLLM([{"reply": "Starting a SIMULATED call.", "action": {"name": "request_call", "args": {"destination_alias": ALIAS, "purpose": "check hours"}}},
                          {"reply": "The SIMULATED call is under way; its result will appear here.", "action": None}])

    async def go():
        r = await s.aria.chat("Call the front desk and check their hours")
        cid = r["actions"][0]["evidence"]["call_id"]
        await s.calling.wait(cid)
        return r, cid
    r, cid = asyncio.run(go())
    assert r["kind"] == "call (SIMULATED)" and r["actions"][0]["name"] == "request_call" and r["actions"][0]["ok"] and cid.startswith("call-")
    assert s.calling.get(cid)["state"] == "COMPLETED"
    assert "SIMULATED" in s.aria.transcript()[-1]["text"] and cid in s.aria.transcript()[-1]["text"]


def test_api_create_get_cancel(tmp_path):
    s = _svc(tmp_path)
    s.calling.adapter = SimulatedAdapter(delay_sec=30)               # still in flight when the cancel arrives
    h = {"Authorization": "Bearer tok"}
    with TestClient(build_app(s, "tok")) as c:
        assert c.post("/v0/calls", json={"destination_alias": ALIAS}).status_code == 401
        r = c.post("/v0/calls", json={"destination_alias": ALIAS, "purpose": "api", "command_id": "cmd-api"}, headers=h)
        assert r.status_code == 201
        rec = r.json()
        assert rec["simulated"] and rec["duplicate"] is False and rec["call_id"].startswith("call-")
        assert c.post("/v0/calls", json={"destination_alias": ALIAS, "command_id": "cmd-api"}, headers=h).json()["duplicate"] is True
        assert c.get(f"/v0/calls/{rec['call_id']}", headers=h).json()["call_id"] == rec["call_id"]
        assert [x["call_id"] for x in c.get("/v0/calls", headers=h).json()["calls"]] == [rec["call_id"]]
        out = c.post(f"/v0/calls/{rec['call_id']}/cancel", headers=h).json()
        assert out["state"] == "CANCELLED" and out["cancelled"] is True
        assert c.post("/v0/calls/call-nope/cancel", headers=h).status_code == 404
        assert c.get("/v0/calls/call-nope", headers=h).status_code == 404


def test_out_of_order_events_are_rejected(tmp_path):
    s = _svc(tmp_path)
    rec = s.calling.request("cmd-o", ALIAS, "p", None, "test")
    cid = rec["call_id"]
    r = s.calling.apply(cid, 4, "COMPLETED", "too early", {"outcome": "SIMULATED: completed"})
    assert r["applied"] is False and "not allowed" in r["reason"] and r["state"] == "REQUESTED"
    assert s.calling.get(cid)["result"] is None
    assert s.calling.apply(cid, 1, "STARTED")["applied"] is False       # skipping ACCEPTED
    assert s.calling.apply(cid, 1, "ACCEPTED")["applied"] is True


# --- correction da-586eaa76af: command identity per owner turn; rejected events remembered ---------------------

def test_same_action_twice_in_one_owner_turn_is_one_call(tmp_path):
    s = _svc(tmp_path)
    act = {"name": "request_call", "args": {"destination_alias": ALIAS, "purpose": "check hours"}}
    s.aria.llm = FakeLLM([{"reply": "Calling.", "action": act}, {"reply": "Calling again.", "action": act}, {"reply": "Done.", "action": None}])

    async def go():
        r = await s.aria.chat("Call the front desk and check their hours")
        await s.calling.wait(r["actions"][0]["evidence"]["call_id"])
        return r
    r = asyncio.run(go())
    assert [a["name"] for a in r["actions"]] == ["request_call", "request_call"]
    assert r["actions"][0]["evidence"]["call_id"] == r["actions"][1]["evidence"]["call_id"]
    assert len(s.calling.list()) == 1 and s.calling.adapter_starts == 1
    second = [h for h in s.aria.history if h["role"] == "action"][1]["text"]
    assert '"duplicate": true' in second
    owner = [h for h in s.aria.history if h["role"] == "owner"][-1]
    assert owner["turn_id"].startswith("turn-") and s.calling.get(r["actions"][0]["evidence"]["call_id"])["conversation_ref"] == owner["turn_id"]


def test_two_identical_owner_turns_at_history_cap_are_two_calls(tmp_path):
    from desktop_agent.control.aria import HISTORY_KEEP
    s = _svc(tmp_path)
    s.aria.history = [{"role": "owner" if i % 2 == 0 else "aria", "text": f"filler {i}", "ts": float(i)} for i in range(HISTORY_KEEP + 5)]
    act = {"name": "request_call", "args": {"destination_alias": ALIAS, "purpose": "check hours"}}
    ids = []

    async def go():
        for _ in range(2):
            s.aria.llm = FakeLLM([{"reply": "Calling.", "action": act}, {"reply": "Done.", "action": None}])
            r = await s.aria.chat("Call the front desk and check their hours")
            ids.append(r["actions"][0]["evidence"]["call_id"])
            await s.calling.wait(ids[-1])
    asyncio.run(go())
    assert len(set(ids)) == 2 and s.calling.adapter_starts == 2 and len(s.calling.list()) == 2
    assert len(s.aria.history) <= HISTORY_KEEP and {c["conversation_ref"] for c in s.calling.list()} == {h["turn_id"] for h in s.aria.history if h["role"] == "owner" and h.get("turn_id")}
    # a request with neither command_id nor a turn is a new command every time (API callers must pass command_id to replay)
    a = s.calling.request(None, ALIAS, "check hours", None, "test"); b = s.calling.request(None, ALIAS, "check hours", None, "test")
    assert a["call_id"] != b["call_id"] and a["command_id"] != b["command_id"]


def test_identical_rejected_late_completion_twice_is_one_receipt(tmp_path):
    s = _svc(tmp_path)
    cid = s.calling.request("cmd-late", ALIAS, "p", None, "test")["call_id"]
    s.calling.apply(cid, 1, "ACCEPTED"); s.calling.apply(cid, 2, "STARTED")
    assert s.calling.cancel(cid, "test")["state"] == "CANCELLED"
    n = len(chain(s.store, "call", cid))
    late = dict(detail="late", result={"outcome": "SIMULATED: completed", "summary": "SIMULATED late"})
    r1 = s.calling.apply(cid, 4, "COMPLETED", **late)
    r2 = s.calling.apply(cid, 4, "COMPLETED", **late)
    assert r1 == {"applied": False, "reason": "late result ignored", "state": "CANCELLED"}
    assert r2 == {"applied": False, "reason": "replayed rejection ignored", "state": "CANCELLED"}
    assert len(chain(s.store, "call", cid)) == n + 1
    call = s.calling.get(cid)
    assert call["state"] == "CANCELLED" and call["result"] is None
    # out-of-order rejections are remembered the same way; a different rejected event is a new receipt
    cid2 = s.calling.request("cmd-ooo", ALIAS, "p", None, "test")["call_id"]
    s.calling.apply(cid2, 3, "IN_PROGRESS"); s.calling.apply(cid2, 3, "IN_PROGRESS"); s.calling.apply(cid2, 4, "COMPLETED")
    assert [r["claim"][:40] for r in chain(s.store, "call", cid2)][1:] == ["SIMULATED out-of-order event rejected: R", "SIMULATED out-of-order event rejected: R"]


def test_overlapping_owner_turns_keep_their_own_identity(tmp_path):
    """da-37dac6c840: the first chat is held inside llm.complete while a second identical chat starts and finishes;
    each turn must get its own call with its own provenance, and the repeated action inside each turn stays one call."""
    from desktop_agent.control.llm import Completion
    s = _svc(tmp_path)
    act = {"name": "request_call", "args": {"destination_alias": ALIAS, "purpose": "check hours"}}
    gate = asyncio.Event()

    class GatedLLM(FakeLLM):
        calls = 0
        async def complete(self, prompt, schema, model_class="cloud_cheap", system=None, timeout=0, purpose=""):
            GatedLLM.calls += 1
            if GatedLLM.calls == 1:
                await gate.wait()                          # first chat parks here; the second runs start to finish meanwhile
            return await super().complete(prompt, schema, model_class, system, timeout)
    s.aria.llm = GatedLLM([{"reply": "a", "action": act}, {"reply": "a", "action": act}, {"reply": "done", "action": None}] * 2)

    async def go():
        first = asyncio.create_task(s.aria.chat("Call the front desk and check their hours"))
        await asyncio.sleep(0.05)
        second = await s.aria.chat("Call the front desk and check their hours")
        gate.set()
        first = await first
        for r in (first, second):
            await s.calling.wait(r["actions"][0]["evidence"]["call_id"])
        return first, second
    first, second = asyncio.run(go())
    ids = {first["actions"][0]["evidence"]["call_id"], second["actions"][0]["evidence"]["call_id"]}
    assert len(ids) == 2 and s.calling.adapter_starts == 2 and len(s.calling.list()) == 2
    for r in (first, second):                               # repeated identical action inside one turn: one call
        assert r["actions"][0]["evidence"]["call_id"] == r["actions"][1]["evidence"]["call_id"]
    owners = [h for h in s.aria.history if h["role"] == "owner"]
    assert len(owners) == 2 and len({o["turn_id"] for o in owners}) == 2
    assert {c["conversation_ref"] for c in s.calling.list()} == {o["turn_id"] for o in owners}
