"""One rule for feed currency (owner correction da-76a26e95fa / da-8fbee0af64): the feed's OWN observation
time decides, never the fetch time. Fixtures: fresh fetch + stale source; fresh source; missing, malformed
and future source time; old STOPPED vs a newer ack receipt; dispatcher/ready claims from a re-fetched old
feed. Rendered Work rows distinguish the current observation from receipt availability. No network."""
import time
from pathlib import Path

from desktop_agent.control import feedtime
from desktop_agent.control.feedtime import feed_freshness, parse_iso
from tests.test_drilldown import _service

T0 = 1791614194.0     # 2026-10-10T06:36:34Z


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def test_parse_iso_variants():
    assert parse_iso("2026-10-10T06:36:34Z") == T0 and parse_iso("2026-10-10T06:36:34+00:00") == T0
    assert parse_iso("2026-10-10T01:36:34-05:00") == T0 and parse_iso("2026-10-10T06:36:34.250Z") == T0
    assert parse_iso("2026-10-10") == T0 - 6 * 3600 - 36 * 60 - 34
    for bad in (None, "", "t", "yesterday", 12345, "2026-13-45T99:00:00Z"):
        assert parse_iso(bad) is None, bad


def test_freshness_rule_fetch_vs_source():
    now = T0 + 120
    fresh = feed_freshness({"fetched_at": now - 31, "data": {"last_check": _iso(T0)}}, now)
    assert fresh["status"] == "fresh" and fresh["fresh"] and "observed 2026-10-10T06:36:34Z (2m ago), read 31s ago" == fresh["label"]
    # fetched 31 s ago, but the document itself is 3 h old: STALE, whatever the fetch time says
    stale = feed_freshness({"fetched_at": now - 31, "data": {"last_check": _iso(T0 - 3 * 3600)}}, now)
    assert stale["status"] == "stale" and not stale["fresh"] and "3h old" in stale["label"] and "not current" in stale["label"]
    # fresh source but the fetch is old (watchdog down): also not current
    assert feed_freshness({"fetched_at": now - 3600, "data": {"last_check": _iso(now - 60)}}, now)["status"] == "stale"
    # missing / malformed / future observation time: UNKNOWN, never fresh
    for lc in (None, "", "t", "yesterday"):
        r = feed_freshness({"fetched_at": now - 5, "data": {"last_check": lc}}, now)
        assert r["status"] == "unknown_time" and not r["fresh"] and "cannot be dated" in r["label"], lc
    fut = feed_freshness({"fetched_at": now - 5, "data": {"last_check": _iso(now + 2 * 3600)}}, now)
    assert fut["status"] == "unknown_time" and "in the future" in fut["label"]
    assert feed_freshness({"fetched_at": now - 5, "data": {"last_check": _iso(now + 60)}}, now)["fresh"]   # a minute of skew is fine
    assert feed_freshness(None, now)["status"] == "missing"
    # ack within TTL = evidence that the inbox is processed, independent of the feed's state
    assert feed_freshness({"fetched_at": now, "data": {"last_check": _iso(T0)}}, now, last_ack=now - 100)["ack_recent"]
    assert not feed_freshness({"fetched_at": now, "data": {"last_check": _iso(T0)}}, now, last_ack=now - 3600)["ack_recent"]


def _mbos(tmp_path: Path, last_check, state="STOPPED", session=None, fetched_ago=31.0, work=None, unacked=None):
    s = _service(tmp_path)
    s.store.set_kv("roster:mbos", {"fetched_at": time.time(), "source": "b:x", "url": "u", "sha": "abc",
                                   "rows": [{"lane": "01", "role": "Coordinator (persistent)", "declared_state": "WORKING", "last_result": "", "ready": "", "blocker": "", "declared_as_of": "2026-10-08"}]})
    s.workers.procs_fn = lambda: []
    s.store.set_kv("feed:mbos", {"fetched_at": time.time() - fetched_ago, "url": "u",
                                 "data": {"state": state, "session": session, "last_check": last_check, "unacknowledged_messages": unacked or [],
                                          "work": work if work is not None else {"dispatcher_running": False, "stalled_workers": [], "approved_ready_rows_for_specialist_lanes": 0}}})
    return s


def _lane01(s):
    return next(x for x in s.drill.build("mbos")["roster"] if x["lane"] == "01")


def test_refetched_old_stopped_is_not_a_restart_instruction(tmp_path: Path):
    """Fresh fetch, 3-hour-old source saying STOPPED: UNKNOWN (stale feed) on the lane, a Waiting row that
    names the observation time, and NO 'start it in your own account' row."""
    s = _mbos(tmp_path, _iso(time.time() - 3 * 3600), unacked=["ARYA-20261010-0328-f49-verification"])
    r = _lane01(s)
    assert r["actual"] == "UNKNOWN (stale feed)" and "3h old" in r["proof"] and "STOPPED" in r["proof"]
    w = s.state()["work"]
    assert not any("start it in your own account" in x["text"] for x in w["needs_owner"])
    row = next(x for x in w["waiting"] if "last said STOPPED" in x["text"])
    assert "not a current state and not a restart instruction" in row["text"] and "3h old" in row["text"] and row["who"] == "its watchdog (other account)"
    assert "1 message(s) its feed lists as unread, 0 of them with an ack receipt here" in row["text"]
    # card-level: the coordinator line is not verified, so the card never says NO CURRENT ACTIVITY
    fd = next(p for p in s.state()["projects"] if p["name"] == "mbos")["workers"]["feed"]
    assert fd["fresh"] is False and fd["freshness"] == "stale" and fd["source_age_sec"] > 3 * 3600 - 5
    assert any("not current" in a["text"] for a in next(p for p in s.state()["projects"] if p["name"] == "mbos")["workers"]["alerts"])


def test_fresh_source_stopped_without_acks_is_the_owner_action(tmp_path: Path):
    s = _mbos(tmp_path, _iso(time.time() - 90), unacked=["ARYA-20261010-0328-f49-verification"])
    r = _lane01(s)
    assert r["actual"] == "NOT RUNNING" and "observed 2026-10-10" in r["proof"] and "read 31s ago" in r["proof"]
    w = s.state()["work"]
    assert w["needs_owner"][0]["who"] == "you (other account)" and "start it in your own account" in w["needs_owner"][0]["text"]
    assert "1 message(s) its feed lists as unread, 0 of them with an ack receipt here" in w["needs_owner"][0]["text"]


def test_missing_malformed_or_future_source_time_is_unknown(tmp_path: Path):
    for lc in (None, "t", _iso(time.time() + 7200)):
        sub = tmp_path / str(abs(hash(str(lc))) % 10_000); sub.mkdir()
        s = _mbos(sub, lc)
        r = _lane01(s)
        assert r["actual"] == "UNKNOWN (undated feed)" and "cannot be dated" in r["proof"], lc
        w = s.state()["work"]
        assert not any("start it in your own account" in x["text"] for x in w["needs_owner"])
        assert any("not a current state" in x["text"] for x in w["waiting"])


def test_old_stopped_versus_newer_ack_receipt_is_contradicted_not_a_restart(tmp_path: Path):
    """The feed says STOPPED/no session (fresh), but an ack file from that account was observed minutes ago:
    something there processes the inbox. The lane shows UNKNOWN (contradicted) with both facts; the Work row
    waits on its coordinator, not on Michael; receipt availability is stated separately."""
    s = _mbos(tmp_path, _iso(time.time() - 90), unacked=["ARYA-20261010-0328-f49-verification"])
    s.store.save_coordinator_ack("mbos", time.time() - 70)
    s.store.save_intake({"item_id": "da-684ad0b380", "project": "mbos", "repo": "x/mbos", "issue": 0, "kind": "liaison", "gh_id": 0, "author": "aria",
                         "title": "F-49 verification and remaining live gate — ARYA-20261010-0328-f49-verification", "body": "", "url": "u", "posted_at": "",
                         "status": "ACKNOWLEDGED", "coordinator": "liaison"})
    r = _lane01(s)
    assert r["actual"] == "UNKNOWN (contradicted)" and "ack file from its account was observed 1m ago" in r["proof"] and "no restart is implied" in r["proof"]
    w = s.state()["work"]
    assert not any("start it in your own account" in x["text"] for x in w["needs_owner"])
    row = next(x for x in w["waiting"] if "ack file from its account appeared" in x["text"])
    assert row["who"] == "its coordinator (other account)" and "1 of them with an ack receipt here" in row["text"]
    fd = next(p for p in s.state()["projects"] if p["name"] == "mbos")["workers"]["feed"]
    assert fd["fresh"] and fd["ack_recent"] and fd["last_ack"]
    # a live coordinator process is still the stronger evidence and is never suppressed by the feed
    s.projects["mbos"].roster["coordinator_process_pattern"] = "AGENT 01 — COORDINATOR"
    s.workers.procs_fn = lambda: [{"pid": 14005, "ppid": 1, "user": "michaelos", "cmd": "claude MICHAEL BUSINESS OS — ROUND TWO AGENT 01 — COORDINATOR", "ticks": 1, "started_at": time.time() - 100}]
    assert _lane01(s)["actual"].startswith("SESSION ALIVE")


def test_refetched_old_dispatcher_claims_are_not_current(tmp_path: Path):
    """A 2-hour-old feed saying 'dispatcher running, 2 approved rows' fetched 30 s ago must not produce a
    'can run now' row or a stopped-dispatcher amber alert; it is its last statement."""
    s = _mbos(tmp_path, _iso(time.time() - 7200), state="HEALTHY", work={"dispatcher_running": False, "stalled_workers": ["F-9"], "approved_ready_rows_for_specialist_lanes": 2})
    st = s.state(); w = st["work"]; p = next(x for x in st["projects"] if x["name"] == "mbos")
    assert not any("approved row" in x["text"] for x in w["can_run_now"] + w["needs_owner"])
    assert any("dispatcher/ready-row figures are not current" in x["text"] for x in w["waiting"])
    assert not any(a["level"] == "amber" for a in p["workers"]["alerts"]) and any("not current" in a["text"] for a in p["workers"]["alerts"])
    assert p["workers"]["feed"]["fresh"] is False
    # the same figures with a current observation: the amber alert and the owner row come back
    (tmp_path / "b").mkdir()
    s2 = _mbos(tmp_path / "b", _iso(time.time() - 60), state="HEALTHY", work={"dispatcher_running": False, "stalled_workers": [], "approved_ready_rows_for_specialist_lanes": 2})
    st2 = s2.state(); p2 = next(x for x in st2["projects"] if x["name"] == "mbos")
    assert any(a["level"] == "amber" and "2 approved row(s)" in a["text"] for a in p2["workers"]["alerts"])
    assert any("dispatcher is stopped" in x["text"] for x in st2["work"]["needs_owner"])
