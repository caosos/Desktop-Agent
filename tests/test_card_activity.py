"""Owner cards tell the truth in two lines (owner bug da-fe9a019247): COORDINATOR and SPECIALIST WORKERS are stated
separately; the badge is ACTIVE when either is provably active, NO CURRENT ACTIVITY only when both are verified idle,
and 'activity not verified' when the data cannot say. Six states, screenshots at 1648 and 390, no model calls."""
import threading
import time
from pathlib import Path

import pytest

from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control import intake_delivery
from desktop_agent.control.service import Service

playwright = pytest.importorskip("playwright.sync_api")
PORT = 8495

SESSIONS = {"busy0": {"name": "sess-busy0", "status": "busy", "pid": 1}, "idle2": {"name": "sess-idle2", "status": "idle", "pid": 2},
            "quiet": {"name": "sess-quiet", "status": "idle", "pid": 3}}


def _seed(tmp_path: Path, monkeypatch) -> Service:
    specs = {
        "busy0": "intake:\n  issues: [1]\n  coordinator: {kind: claude_peer, session_name: sess-busy0}\nworkers:\n  process_patterns: ['busy0-worker-(?P<id>\\w+)']\n",   # coordinator BUSY + 0 workers
        "idle2": "intake:\n  issues: [1]\n  coordinator: {kind: claude_peer, session_name: sess-idle2}\nworkers:\n  process_patterns: ['IDLE2/(?P<id>[\\w-]+)\\.prompt']\n",   # coordinator IDLE + 2 workers
        "ext": "workers:\n  process_patterns: ['tools/worker\\.py (?P<id>[A-Z]-\\d+)']\n  feed_url: http://127.0.0.1:1/\n  feed_project: ext\n"
               "intake:\n  issues: []\n  coordinator:\n    kind: liaison\n    liaison: {branch: l, inbox_dir: i, ack_branch: a, ack_dir: d}\n",                        # manual-only + F-45 running
        "quiet": "intake:\n  issues: [1]\n  coordinator: {kind: claude_peer, session_name: sess-quiet}\nworkers:\n  process_patterns: ['never-(?P<id>x)']\n",            # both idle
        "stale": "workers:\n  feed_url: http://127.0.0.1:1/\n  feed_project: stale\n",                                                                                  # feed says stalled
        "blind": "",                                                                                                                                                      # no telemetry at all
    }
    for name, extra in specs.items():
        repo = tmp_path / name; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
        (tmp_path / f"{name}.yaml").write_text(f"name: {name}\nrepo_path: {repo}\nremote_url: x\ngithub_repo: x/{name}\nintegration_branch: main\nstart_here: S\nagents_file: A\n{extra}")
    s = Service(RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t", project_files=[tmp_path / f"{n}.yaml" for n in specs]))
    s.scheduler.paused = True
    monkeypatch.setattr(intake_delivery.Deliverer, "session_for", staticmethod(lambda src: SESSIONS.get((src.coordinator or {}).get("session_name", "")[5:])))
    now = time.time()
    s.workers.procs_fn = lambda: [
        {"pid": 11, "ppid": 1, "user": "c", "cmd": 'bash -c claude "$(cat /x/IDLE2/rq-1.prompt)"', "ticks": 3, "started_at": now - 300},
        {"pid": 12, "ppid": 1, "user": "c", "cmd": 'bash -c claude "$(cat /x/IDLE2/rq-2.prompt)"', "ticks": 5, "started_at": now - 200},
        {"pid": 13, "ppid": 1, "user": "m", "cmd": "python -I tools/worker.py F-45 --lane 06", "ticks": 9, "started_at": now - 100}]
    s.store.set_kv("feed:ext", {"fetched_at": now, "url": "u", "data": {"work": {"dispatcher_running": True, "stalled_workers": [], "approved_ready_rows_for_specialist_lanes": 0}, "last_check": "t"}})
    s.store.set_kv("feed:stale", {"fetched_at": now, "url": "u", "data": {"work": {"dispatcher_running": True, "stalled_workers": ["C-9"], "approved_ready_rows_for_specialist_lanes": 0}, "last_check": "t"}})
    return s


@pytest.fixture
def served(tmp_path, monkeypatch):
    import uvicorn
    s = _seed(tmp_path, monkeypatch)
    server = uvicorn.Server(uvicorn.Config(build_app(s, "tok"), host="127.0.0.1", port=PORT, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    for _ in range(50):
        if server.started: break
        time.sleep(0.1)
    yield s
    server.should_exit = True; th.join(timeout=5)


def test_cards_state_coordinator_and_workers_separately(served):
    s = served
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome: {exc}")
        page = browser.new_page(viewport={"width": 1648, "height": 1000})
        page.goto(f"http://127.0.0.1:{PORT}/?access_token=tok")
        page.wait_for_function("document.querySelectorAll('#o_projects .ocard').length === 6", timeout=15000)
        card = lambda n: " ".join(page.inner_text(f"#o_projects .ocard[data-p='{n}']").split())      # spinner glyphs render as whitespace
        b = card("busy0"); assert "COORDINATOR: BUSY" in b and "sess-busy0, working" in b and "SPECIALIST WORKERS: 0 running" in b and "ACTIVE · coordinator" in b and "normal: bounded workers" in b
        i = card("idle2"); assert "COORDINATOR: IDLE" in i and "waiting for a message" in i and "2 running: rq-1, rq-2" in i and "ACTIVE · workers" in i and "host process list" in i
        e = card("ext"); assert "COORDINATOR: manual-only (other Linux account)" in e and "1 running: F-45" in e and "ACTIVE · workers" in e and "project feed" in e
        q = card("quiet"); assert "COORDINATOR: IDLE" in q and "0 running" in q and "NO CURRENT ACTIVITY · waiting for instructions" in q and "ACTIVE" not in q
        st = card("stale"); assert "alive but not progressing: C-9" in st and "attention" in st and "ACTIVE" not in st
        bl = card("blind"); assert "COORDINATOR: not verified" in bl and "SPECIALIST WORKERS: not verified" in bl and "activity not verified" in bl and "NO CURRENT ACTIVITY" not in bl
        assert "no Desktop-Agent decisions waiting" in page.inner_text("#o_decisions")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.screenshot(path=str(Path(s.cfg.data_dir) / "cards-1648.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844}); page.wait_for_timeout(250)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.screenshot(path=str(Path(s.cfg.data_dir) / "cards-390.png"), full_page=True)
        browser.close()
    assert s.store.cost_summary()["unknown_usage"]["calls"] == 0 and s.store.cost_summary()["total_usd"] == 0      # read-only, no model calls
