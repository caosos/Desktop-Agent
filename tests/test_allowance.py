"""Included Claude allowance: status-line drop → quota record → panel view. Absent/expired windows are
UNKNOWN, one receipt per meaningful change, a newer worker-run observation is not overwritten by an older
drop, the status-line script writes only from the designated session. No network, no model."""
import json
import subprocess
import sys
import time
from pathlib import Path

from desktop_agent.control import allowance
from desktop_agent.control.store import Store

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "claude_statusline.py"


def _drop(path: Path, observed_at: float, five=63.0, seven=86.0, resets=None):
    d = {"observed_at": observed_at, "source": "claude_code_statusline", "version": "2.1.293", "session_id": "s1", "session_name": "caoscare-1-05",
         "cwd": "/home/caoscare-1",
         "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": resets or 4102444800},
                         "seven_day": {"used_percentage": seven, "resets_at": 4102531200}}}
    path.write_text(json.dumps(d))
    return d


def test_ingest_stores_once_per_meaningful_change(tmp_path: Path):
    store = Store(tmp_path); f = tmp_path / "allowance.json"
    assert allowance.ingest(store, f)["found"] is False and store.get_kv("quota") is None
    t0 = time.time()
    _drop(f, t0)
    r = allowance.ingest(store, f, now=t0 + 1)
    assert r["stored"] and r["receipt"]
    q = store.get_kv("quota")
    assert q["source"] == "claude_code_statusline" and q["windows"]["five_hour"]["utilization"] == 0.63 and q["windows"]["seven_day"]["used_percentage"] == 86.0
    assert q["source_detail"]["session_name"] == "caoscare-1-05" and q["seen_at"] == t0
    # same numbers a minute later: stored (fresher), but no second receipt
    _drop(f, t0 + 60)
    r2 = allowance.ingest(store, f, now=t0 + 61)
    assert r2["stored"] and r2["receipt"] is False
    # one point more: a receipt
    _drop(f, t0 + 120, five=64.0)
    assert allowance.ingest(store, f, now=t0 + 121)["receipt"] is True
    evs = [e for e in store.events() if e.payload.get("action") == "allowance_observed"]
    assert len(evs) == 2 and evs[-1].payload["windows"]["five_hour"]["used_percentage"] == 64.0
    # an older drop never overwrites a newer held observation (e.g. a worker run reported in between)
    store.set_kv("quota", {"source": "worker_run", "seen_at": t0 + 500, "windows": {"five_hour": {"utilization": 0.7, "resets_at": t0 + 3600}}})
    _drop(f, t0 + 130, five=65.0)
    assert allowance.ingest(store, f, now=t0 + 600)["reason"].startswith("not newer")
    assert store.get_kv("quota")["source"] == "worker_run"


def test_view_unknown_when_absent_or_reset_and_freshness():
    now = time.time()
    assert allowance.view(None) is None
    q = {"source": "claude_code_statusline", "seen_at": now - 60,
         "windows": {"five_hour": {"utilization": 0.63, "used_percentage": 63.0, "resets_at": now + 100}}}
    v = allowance.view(q, now=now)["view"]
    assert v["windows"]["five_hour"] == {"state": "KNOWN", "used_percentage": 63.0, "resets_at": now + 100, "reason": None}
    assert v["windows"]["seven_day"]["state"] == "UNKNOWN" and "not reported" in v["windows"]["seven_day"]["reason"]
    assert v["freshness"] == "fresh" and v["source"] == "claude_code_statusline"
    # the 5 h window reset since the observation → UNKNOWN, never the old number; old observation → stale
    v2 = allowance.view(q, now=now + 200 + allowance.STALE_AFTER_SEC)["view"]
    assert v2["windows"]["five_hour"]["state"] == "UNKNOWN" and "reset since" in v2["windows"]["five_hour"]["reason"]
    assert v2["freshness"] == "stale"
    # worker-run record without used_percentage still renders a percentage
    w = allowance.view({"task_id": "t1", "seen_at": now, "windows": {"seven_day": {"utilization": 0.861, "resets_at": now + 10}}}, now=now)["view"]
    assert w["windows"]["seven_day"]["used_percentage"] == 86.1 and w["source"] == "worker_run"


def test_statusline_script_prints_and_writes_only_for_the_designated_session(tmp_path: Path):
    payload = {"model": {"display_name": "Fable"}, "context_window": {"used_percentage": 12.4}, "version": "2.1.293", "session_id": "abc",
               "cwd": str(tmp_path / "home"), "workspace": {"project_dir": str(tmp_path / "home")},
               "rate_limits": {"five_hour": {"used_percentage": 63, "resets_at": 1791579000}, "seven_day": {"used_percentage": 86, "resets_at": 1791799200},
                               "spend_limit": {"used_percentage": 1, "used_usd": 2.0}}}
    env = {"DA_DATA_DIR": str(tmp_path / "data"), "DA_ALLOWANCE_PROJECT_DIR": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "TZ": "UTC"}
    out = subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=20)
    assert out.returncode == 0 and out.stdout.startswith("[Fable] · ctx 12% · 5h 63% ↻") and "7d 86%" in out.stdout
    drop = json.loads((tmp_path / "data" / "allowance.json").read_text())
    assert set(drop["rate_limits"]) == {"five_hour", "seven_day"} and drop["session_id"] == "abc" and drop["observed_at"] > 0
    assert "spend_limit" not in drop["rate_limits"] and "transcript_path" not in drop
    # another session of the same account: the line still shows, nothing is written
    other = {**payload, "workspace": {"project_dir": str(tmp_path / "elsewhere")}, "session_id": "zzz"}
    (tmp_path / "data" / "allowance.json").unlink()
    out2 = subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(other), capture_output=True, text=True, env=env, timeout=20)
    assert out2.stdout.startswith("[Fable]") and not (tmp_path / "data" / "allowance.json").exists()
    # before the first response there are no rate limits: shows dashes, writes nothing
    out3 = subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps({"model": {"display_name": "Fable"}}), capture_output=True, text=True, env=env, timeout=20)
    assert out3.stdout.strip() == "[Fable] · ctx — · 5h — · 7d —" and not (tmp_path / "data" / "allowance.json").exists()
