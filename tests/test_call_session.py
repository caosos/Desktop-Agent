"""SIMULATED session runner (docs/CALLING_BRIEF.md section 11.5, points 1-8). Scripted transports, zero delays."""
import asyncio

from desktop_agent.control.call_session import FakeTransport, fixture_script
from desktop_agent.control.calling import SimulatedAdapter
from desktop_agent.control.receipts import chain
from tests.test_ask_aria import _service

ALIAS = "fixture:front-desk"
END = {"type": "session.ended", "outcome": "completed"}


class Adapter(SimulatedAdapter):
    """SimulatedAdapter whose transports are the scripted ones (kept for inspection)."""

    def __init__(self, scripts, delay_sec=0.0):
        super().__init__(delay_sec)
        self.scripts, self.transports = list(scripts), []
        self.transport_factory = self._make

    def _make(self, call):
        t = FakeTransport(self.scripts.pop(0), self.delay_sec)
        self.transports.append(t)
        return t


def _svc(tmp_path, *scripts, delay=0.0):
    s = _service(tmp_path)
    s.calling.adapter = Adapter(scripts or [fixture_script()], delay)
    return s


def _go(s, *ids_args):
    async def go():
        recs = [s.calling.request(*a) for a in ids_args]
        for r in recs:
            await s.calling.wait(r["call_id"])
        return recs
    return asyncio.run(go())


def _tools(s, cid):
    return chain(s.store, "call_tool", cid)


def _kv(s, cid):
    return s.store.get_kv(f"call:{cid}")


def tc(turn, tid, name="lookup_fixture_status", args=None):
    return {"type": "tool.call", "turn": turn, "tool_call_id": tid, "name": name, "args": {"alias": ALIAS} if args is None else args}


def test_two_turn_conversation_default_adapter(tmp_path):
    s = _service(tmp_path)                                   # default adapter: SimulatedAdapter over the fixture script
    [rec] = _go(s, ("cmd-1", ALIAS, "confirm hours", 0, "test"))
    cid = rec["call_id"]
    call = _kv(s, cid)
    assert call["state"] == "COMPLETED" and "SIMULATED" in call["result"]["outcome"] and call["result"]["turns"] == 2 and call["result"]["tools"] == 1
    assert [h["state"] for h in call["history"]] == ["ACCEPTED", "STARTED", "IN_PROGRESS", "COMPLETED"]
    ts = call["transcript"]
    assert {e["turn"] for e in ts} == {1, 2} and all("SIMULATED" in str(e) for e in ts)
    tool = [e for e in ts if e.get("tool_call_id")]
    assert len(tool) == 1 and tool[0]["turn"] == 1 and tool[0]["output"]["transcript"].startswith("SIMULATED caller")
    [r] = _tools(s, cid)
    assert r["result_label"] == "simulated" and "SIMULATED" in r["claim"] and {"call_id=" + cid, "turn=1", "tool_call_id=tc-1", "name=lookup_fixture_status"} <= set(r["evidence"])
    assert [x["result_label"] for x in chain(s.store, "call", cid)] == ["verified"] + ["simulated"] * 4
    assert "SIMULATED" in s.aria.transcript()[-1]["text"] and cid in s.aria.transcript()[-1]["text"]
    for r in chain(s.store, "call", cid)[:3] + _tools(s, cid) + chain(s.store, "call", cid)[3:]:       # visible with -s: the end-to-end trace
        print(f"{r['result_label']:9} {r['subject']['type']:9} {r['claim']}")
    for e in ts:
        print(f"turn {e['turn']}: {e.get('role') or e.get('name')}: {str(e.get('text') or e.get('output'))[:100]}")


def test_default_adapter_sends_tool_result_before_close(tmp_path):
    s = _svc(tmp_path)
    _go(s, ("c", ALIAS, "p", 0, "test"))
    sent = [m["type"] for m in s.calling.adapter.transports[0].sent]
    assert sent == ["tool.result", "response.create", "session.close"]
    assert "SIMULATED" in s.calling.adapter.transports[0].sent[0]["output"]["status"]


def test_unknown_and_out_of_scope_tools_are_refused(tmp_path):
    bad = [tc(1, "a", "request_call"), tc(1, "b", "submit_goal"), tc(1, "c", "nonsense"), tc(1, "d", args={"alias": "+15555550100"}), tc(1, "e", args="x"), END]
    s = _svc(tmp_path, bad)
    [rec] = _go(s, ("c", ALIAS, "p", 0, "test"))
    cid = rec["call_id"]
    sent = [m for m in s.calling.adapter.transports[0].sent if m["type"] == "tool.result"]
    assert len(sent) == 5 and all(m["output"]["ok"] is False and "SIMULATED" in m["output"]["error"] for m in sent)
    rs = _tools(s, cid)
    assert len(rs) == 5 and all("refused" in r["claim"] and "SIMULATED" in r["claim"] for r in rs)
    assert len(s.calling.list()) == 1 and _kv(s, cid)["state"] == "COMPLETED" and _kv(s, cid)["result"]["tools"] == 0
    assert [r["subject"]["id"] for r in chain(s.store, "call", cid)] == [cid] * 5


def test_duplicate_and_replayed_tool_call_ids_execute_once(tmp_path):
    s = _svc(tmp_path, [tc(1, "x"), tc(1, "x"), tc(1, "x"), END])
    [rec] = _go(s, ("c", ALIAS, "p", 0, "test"))
    assert len(_tools(s, rec["call_id"])) == 1 and _kv(s, rec["call_id"])["result"]["tools"] == 1
    res = [m for m in s.calling.adapter.transports[0].sent if m["type"] == "tool.result"]
    assert len(res) == 3 and res[0]["output"] == res[1]["output"] == res[2]["output"]
    assert len([e for e in _kv(s, rec["call_id"])["transcript"] if e.get("tool_call_id")]) == 1


def test_interrupted_speech_is_recorded_and_the_session_continues(tmp_path):
    s = _svc(tmp_path, [{"type": "response.text", "turn": 1, "text": "long answer"}, {"type": "response.cancelled", "turn": 1},
                        {"type": "transcript.user", "turn": 2, "text": "again"}, END])
    [rec] = _go(s, ("c", ALIAS, "p", 0, "test"))
    call = _kv(s, rec["call_id"])
    assert any(e.get("event") == "response.cancelled" and "SIMULATED" in e["note"] for e in call["transcript"])
    assert call["state"] == "COMPLETED" and call["transcript"][-1]["text"] == "SIMULATED again"


def test_delayed_and_out_of_order_transcripts(tmp_path):
    s = _svc(tmp_path, [tc(2, "late"), {"type": "transcript.user", "turn": 2, "text": "second turn"},
                        {"type": "transcript.user", "turn": 1, "text": "first turn, late"}, END])
    [rec] = _go(s, ("c", ALIAS, "p", 0, "test"))
    call = _kv(s, rec["call_id"])
    out = [e for e in call["transcript"] if e.get("tool_call_id")][0]["output"]
    assert out["transcript"] == "second turn"                    # the tool waited for its own turn's transcript
    assert [e["text"] for e in call["transcript"] if e.get("role") == "user"] == ["SIMULATED second turn", "SIMULATED first turn, late"]
    assert call["state"] == "COMPLETED"


def test_tool_without_any_transcript_runs_after_the_bounded_wait(tmp_path, monkeypatch):
    from desktop_agent.control import call_session
    monkeypatch.setattr(call_session, "TRANSCRIPT_WAIT_SEC", 0.01)
    s = _svc(tmp_path, [tc(7, "n"), END])
    [rec] = _go(s, ("c", ALIAS, "p", 0, "test"))
    out = [e for e in _kv(s, rec["call_id"])["transcript"] if e.get("tool_call_id")][0]["output"]
    assert out["ok"] and out["transcript"] is None


def test_disconnect_without_session_ended_fails(tmp_path):
    s = _svc(tmp_path, [{"type": "transcript.user", "turn": 1, "text": "hi"}, {"type": "disconnect"}, END])
    [rec] = _go(s, ("c", ALIAS, "p", 0, "test"))
    call = _kv(s, rec["call_id"])
    assert call["state"] == "FAILED" and "transport disconnected" in call["history"][-1]["detail"] and "SIMULATED" in call["result"]["summary"]
    texts = " ".join(t["text"] for t in s.aria.transcript())
    assert "completed" not in texts and "failed" in texts


def test_iterator_end_without_any_event_fails(tmp_path):
    s = _svc(tmp_path, [])
    [rec] = _go(s, ("c", ALIAS, "p", 0, "test"))
    assert _kv(s, rec["call_id"])["state"] == "FAILED"


def test_cancel_mid_session_ignores_late_events(tmp_path):
    s = _svc(tmp_path, [{"type": "transcript.user", "turn": 1, "text": "hi"}, tc(1, "z"), END], delay=30)

    async def go():
        rec = s.calling.request("c", ALIAS, "p", 0, "test")
        for _ in range(20):
            await asyncio.sleep(0)
        s.calling.cancel(rec["call_id"], "test")
        await s.calling.wait(rec["call_id"])
        return rec
    rec = asyncio.run(go())
    cid = rec["call_id"]
    call = _kv(s, cid)
    assert call["state"] == "CANCELLED" and call["result"] is None
    assert s.calling.adapter.transports[0].sent == [] and _tools(s, cid) == []
    assert "completed" not in " ".join(t["text"] for t in s.aria.transcript())
    assert [r["result_label"] for r in chain(s.store, "call", cid)][-1] == "verified"          # the cancel receipt; no completion


def test_cancel_then_events_already_in_flight_do_nothing(tmp_path):
    s = _svc(tmp_path, [tc(1, "z"), END])

    async def go():
        rec = s.calling.request("c", ALIAS, "p", 0, "test")
        await asyncio.sleep(0)                                    # runner has started; events still pending
        s.calling.cancel(rec["call_id"], "test")
        await s.calling.wait(rec["call_id"])
        return rec
    rec = asyncio.run(go())
    call = _kv(s, rec["call_id"])
    assert call["state"] == "CANCELLED" and call["result"] is None and _tools(s, rec["call_id"]) == []
    assert not [m for m in s.calling.adapter.transports[0].sent if m["type"] == "tool.result"]


def test_two_overlapping_sessions_do_not_mix(tmp_path):
    a = [{"type": "transcript.user", "turn": 1, "text": "AAA"}, tc(1, "ta"), {"type": "transcript.user", "turn": 2, "text": "AAA2"}, END]
    b = [{"type": "transcript.user", "turn": 1, "text": "BBB"}, tc(1, "tb"), END]
    s = _svc(tmp_path, a, b)
    ra, rb = _go(s, ("ca", "fixture:alpha", "pa", 0, "test"), ("cb", "fixture:beta", "pb", 0, "test"))
    ca, cb = _kv(s, ra["call_id"]), _kv(s, rb["call_id"])
    assert ca["state"] == cb["state"] == "COMPLETED"
    assert all("BBB" not in str(e) for e in ca["transcript"]) and all("AAA" not in str(e) for e in cb["transcript"])
    ta, tb = s.calling.adapter.transports
    assert {m["tool_call_id"] for m in ta.sent if m["type"] == "tool.result"} == {"ta"} and {m["tool_call_id"] for m in tb.sent if m["type"] == "tool.result"} == {"tb"}
    for rec, own in ((ra, "ta"), (rb, "tb")):
        [r] = _tools(s, rec["call_id"])
        assert f"tool_call_id={own}" in r["evidence"] and r["call_id"] == rec["call_id"]
        assert all(r2["call_id"] == rec["call_id"] for r2 in chain(s.store, "call", rec["call_id"]))
