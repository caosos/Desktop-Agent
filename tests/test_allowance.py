"""Included Claude allowance: registered-writer identity, status-line drop → quota record → panel view.
Fixtures: competing status-line writers (only the registered session id writes; the ingest rejects the
other), a newer sample with empty rate_limits → UNKNOWN with the old value kept only as historical,
receipt threshold measured against the last receipted value (cumulative drift is not suppressed), absent
or reset windows UNKNOWN, a fresher worker-run observation not overwritten. No network, no model."""
import json
import subprocess
import sys
import time
from pathlib import Path

from desktop_agent.control import allowance
from desktop_agent.control.store import Store

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "claude_statusline.py"
SID = "fc7b05e1-0000-0000-0000-000000000001"


def _drop(path: Path, observed_at: float, five=63.0, seven=86.0, sid=SID, empty=False, resets=None):
    rl = {} if empty else {"five_hour": {"used_percentage": five, "resets_at": resets or 4102444800},
                           "seven_day": {"used_percentage": seven, "resets_at": 4102531200}}
    d = {"observed_at": observed_at, "source": "claude_code_statusline", "version": "2.1.293", "session_id": sid,
         "session_name": None, "cwd": "/home/caoscare-1", "rate_limits": rl}
    path.write_text(json.dumps(d))
    return d


def _register(tmp_path: Path, sid=SID):
    w = tmp_path / "allowance_writer.json"
    allowance.register_writer(w, {"session_id": sid, "name": "caoscare-1-05", "pid": 1, "resolved_by": "name"}, "test")
    return w


def test_only_the_registered_writer_is_ingested(tmp_path: Path):
    store = Store(tmp_path); f = tmp_path / "allowance.json"; w = _register(tmp_path)
    t0 = time.time()
    _drop(f, t0, sid="other-session")
    r = allowance.ingest(store, f, now=t0 + 1, writer_path=w)
    assert r["stored"] is False and "not the registered writer" in r["reason"] and store.get_kv("quota") is None
    _drop(f, t0 + 1)
    assert allowance.ingest(store, f, now=t0 + 2, writer_path=w)["stored"]
    # registration cleared (no eligible coordinator) → nobody may write
    allowance.register_writer(w, None, "ambiguous")
    assert json.loads(w.read_text())["session_id"] is None
    _drop(f, t0 + 3)
    assert "not the registered writer" in allowance.ingest(store, f, now=t0 + 4, writer_path=w)["reason"]
    # register_writer rewrites only on change
    reg1 = allowance.register_writer(w, {"session_id": SID, "name": "n", "pid": 1}, "a")
    reg2 = allowance.register_writer(w, {"session_id": SID, "name": "n", "pid": 1}, "b")
    assert reg1["registered_at"] == reg2["registered_at"]
    assert allowance.register_writer(w, {"session_id": SID, "name": "n", "pid": 2}, "c")["registered_at"] >= reg1["registered_at"]


def test_receipts_measure_against_the_last_receipted_value(tmp_path: Path):
    store = Store(tmp_path); f = tmp_path / "allowance.json"; w = _register(tmp_path)
    t0 = time.time()
    _drop(f, t0, five=16.0)
    assert allowance.ingest(store, f, now=t0 + 1, writer_path=w)["receipt"] is True
    # +0.4 three times: each step is below the threshold, the cumulative change is not
    for i, pct in enumerate((16.4, 16.8, 17.2), start=1):
        _drop(f, t0 + 10 * i, five=pct)
        r = allowance.ingest(store, f, now=t0 + 10 * i + 1, writer_path=w)
        assert r["stored"]
        assert r["receipt"] is (pct >= 17.0), (pct, r)
    evs = [e for e in store.events() if e.payload.get("action") == "allowance_observed"]
    assert [e.payload["windows"]["five_hour"]["used_percentage"] for e in evs] == [16.0, 17.2]
    assert store.get_kv(allowance.RECEIPTED_KV)["windows"]["five_hour"]["used_percentage"] == 17.2
    # an older drop never overwrites a newer held observation (e.g. a worker run reported in between)
    store.set_kv("quota", {"source": "worker_run", "seen_at": t0 + 500, "windows": {"five_hour": {"utilization": 0.7, "resets_at": t0 + 3600}}})
    _drop(f, t0 + 130, five=65.0)
    assert allowance.ingest(store, f, now=t0 + 600, writer_path=w)["reason"].startswith("not newer")
    assert store.get_kv("quota")["source"] == "worker_run"


def test_newer_empty_sample_makes_current_unknown_and_keeps_history(tmp_path: Path):
    store = Store(tmp_path); f = tmp_path / "allowance.json"; w = _register(tmp_path)
    t0 = time.time()
    _drop(f, t0, five=17.0, seven=10.0)
    allowance.ingest(store, f, now=t0 + 1, writer_path=w)
    _drop(f, t0 + 60, empty=True)
    r = allowance.ingest(store, f, now=t0 + 61, writer_path=w)
    assert r["stored"] and r["receipt"] is True                      # known → unknown is a meaningful change
    q = store.get_kv("quota")
    assert q["windows"] == {} and q["historical"]["windows"]["five_hour"]["used_percentage"] == 17.0 and q["historical"]["seen_at"] == t0
    v = allowance.view(q, now=t0 + 62)["view"]
    assert v["windows"]["five_hour"]["state"] == "UNKNOWN" and "no rate-limit windows" in v["windows"]["five_hour"]["reason"]
    assert v["windows"]["five_hour"]["historical"] == {"used_percentage": 17.0, "seen_at": t0}
    assert v["windows"]["seven_day"]["historical"]["used_percentage"] == 10.0 and v["freshness"] == "fresh"
    ev = [e for e in store.events() if e.payload.get("action") == "allowance_observed"][-1]
    assert ev.payload["unknown"] is True and ev.payload["windows"] == {}
    # a second empty sample: stored (fresher), history carried, no second receipt
    _drop(f, t0 + 120, empty=True)
    r2 = allowance.ingest(store, f, now=t0 + 121, writer_path=w)
    assert r2["stored"] and r2["receipt"] is False and store.get_kv("quota")["historical"]["seen_at"] == t0
    # known again: historical dropped, receipt written
    _drop(f, t0 + 180, five=18.0, seven=10.0)
    assert allowance.ingest(store, f, now=t0 + 181, writer_path=w)["receipt"] is True
    assert "historical" not in store.get_kv("quota") and allowance.view(store.get_kv("quota"))["view"]["windows"]["five_hour"]["state"] == "KNOWN"


def test_view_unknown_when_absent_or_reset_and_freshness():
    now = time.time()
    assert allowance.view(None) is None
    q = {"source": "claude_code_statusline", "seen_at": now - 60,
         "windows": {"five_hour": {"utilization": 0.63, "used_percentage": 63.0, "resets_at": now + 100}}}
    v = allowance.view(q, now=now)["view"]
    assert v["windows"]["five_hour"]["state"] == "KNOWN" and v["windows"]["five_hour"]["used_percentage"] == 63.0
    assert v["windows"]["seven_day"]["state"] == "UNKNOWN" and "not reported" in v["windows"]["seven_day"]["reason"]
    assert v["freshness"] == "fresh" and v["source"] == "claude_code_statusline"
    v2 = allowance.view(q, now=now + 200 + allowance.STALE_AFTER_SEC)["view"]
    assert v2["windows"]["five_hour"]["state"] == "UNKNOWN" and "reset since" in v2["windows"]["five_hour"]["reason"]
    assert v2["freshness"] == "stale"
    w = allowance.view({"task_id": "t1", "seen_at": now, "windows": {"seven_day": {"utilization": 0.861, "resets_at": now + 10}}}, now=now)["view"]
    assert w["windows"]["seven_day"]["used_percentage"] == 86.1 and w["source"] == "worker_run"


def _run_script(payload: dict, data_dir: Path) -> str:
    env = {"DA_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin", "TZ": "UTC"}
    out = subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=20)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_statusline_script_competing_writers_and_empty_sample(tmp_path: Path):
    data = tmp_path / "data"; data.mkdir()
    base = {"model": {"display_name": "Fable"}, "context_window": {"used_percentage": 12.4}, "version": "2.1.293",
            "cwd": "/home/caoscare-1", "workspace": {"project_dir": "/home/caoscare-1"}, "transcript_path": "/secret",
            "rate_limits": {"five_hour": {"used_percentage": 63, "resets_at": 1791579000}, "seven_day": {"used_percentage": 86, "resets_at": 1791799200},
                            "spend_limit": {"used_percentage": 1, "used_usd": 2.0}}}
    a, b = {**base, "session_id": SID}, {**base, "session_id": "second-home-dir-session"}
    # no registration yet: both sessions show the line, neither writes (same home directory is not a credential)
    assert _run_script(a, data).startswith("[Fable] · ctx 12% · 5h 63% ↻") and _run_script(b, data).startswith("[Fable]")
    assert not (data / "allowance.json").exists()
    _register(data)
    _run_script(b, data); assert not (data / "allowance.json").exists()
    _run_script(a, data)
    drop = json.loads((data / "allowance.json").read_text())
    assert drop["session_id"] == SID and set(drop["rate_limits"]) == {"five_hour", "seven_day"} and "transcript_path" not in drop
    # the registered session before its first response: an empty sample is written (a fact), the line shows dashes
    out = _run_script({**a, "rate_limits": {}}, data)
    assert out.strip() == "[Fable] · ctx 12% · 5h — · 7d —" and json.loads((data / "allowance.json").read_text())["rate_limits"] == {}
    # ingest end to end: the competing session's drop is rejected even if it got written somehow
    store = Store(tmp_path)
    (data / "allowance.json").write_text(json.dumps({**drop, "session_id": "second-home-dir-session", "observed_at": time.time()}))
    assert "not the registered writer" in allowance.ingest(store, data / "allowance.json", writer_path=data / "allowance_writer.json")["reason"]
