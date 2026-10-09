"""Live-like states on the Owner view, at desktop, laptop and phone widths (owner order da-623f447d9b):
coordinator DOWN/idle with two live workers · all genuinely idle · dispatcher stopped with READY work while quota
permits · quota pause · PR open vs merged vs verified · bounced delivery vs ACK · outstanding owner decisions;
evidence (stage timeline, cost, events, receipts) only on demand."""
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.events import Actor, Event, EventType as ET, Provenance
from desktop_agent.control.service import Service

playwright = pytest.importorskip("playwright.sync_api")
PORT = 8498


def _seed(tmp_path: Path) -> Service:
    specs = {"alpha": "github_repo: x/alpha\nintake:\n  issues: [1]\n  coordinator: {kind: claude_peer, session_cwd: /nowhere}\nworkers:\n  process_patterns: ['BOUNDED/(?P<id>[\\w-]+)\\.prompt', 'Desktop-Agent-work/(?P<id>[\\w-]+)/repo']\n",
             "beta": "workers:\n  feed_url: http://127.0.0.1:1/\n  feed_project: beta\n",
             "gamma": "workers:\n  process_patterns: ['never-matches-(?P<id>x)']\n"}
    for name, extra in specs.items():
        repo = tmp_path / name; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
        (tmp_path / f"{name}.yaml").write_text(f"name: {name}\nrepo_path: {repo}\nremote_url: x\nintegration_branch: main\nstart_here: S\nagents_file: A\n{extra}")
    s = Service(RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t",
                              project_files=[tmp_path / f"{n}.yaml" for n in specs]))
    s.scheduler.paused = True; s.scheduler.hold_reason = "paused: provider limit (resets 14:20)"
    st = s.store
    # alpha: two live workers (one control-plane task, one coordinator-launched), DOWN coordinator, PR open + PR merged
    st.save_task("t-run", "g", "alpha", "RUNNING", {"objective": "Fix the parser", "model_class": "cloud_strong", "attempt": 1}, "h")
    st.save_task("t-done", "g", "alpha", "DONE", {"objective": "Add the labels", "model_class": "cloud_cheap", "attempt": 1}, "h", {"integration": {"pr_url": "https://x/pr/11"}})
    st.save_task("t-merged", "g", "alpha", "DONE", {"objective": "Write the blueprint", "model_class": "cloud_strong", "attempt": 1}, "h", {"integration": {"pr_url": "https://x/pr/10"}})
    st.set_kv("open_prs:x/alpha", {"checked_at": time.time(), "urls": ["https://x/pr/11"]})
    for i, (typ, text) in enumerate([(ET.WORKER_STARTED.value, "started"), (ET.FILE_READ.value, "read S"), (ET.FILE_CHANGED.value, "wrote A"),
                                     (ET.TEST_PASSED.value, "tests ok"), (ET.COMMIT_CREATED.value, "commit"), (ET.CLAIM_WRITTEN.value, "DONE"),
                                     (ET.VERIFY_PASSED.value, "verified"), (ET.TASK_DONE.value, "done")]):
        st.append_event(Event(type=typ, task_id="t-done", payload={"base_sha": "b", "text": text, "status": "DONE"} if typ == ET.CLAIM_WRITTEN.value else {"base_sha": "b", "text": text},
                              provenance=Provenance(actor=Actor.WORKER.value if i < 6 else Actor.CONTROL.value, source="test", evidence=[f"ev{i}"])))
    st.add_cost("t-done", "w-1", "claude-haiku-5-5", 0.0172, 1000, 200)
    s.workers.procs_fn = lambda: [
        {"pid": 20, "ppid": 1, "user": "c", "cmd": 'bash -c claude "$(cat /home/x/BOUNDED/rq-050-claim.prompt)"', "ticks": 5, "started_at": time.time() - 600},
        {"pid": 30, "ppid": 1, "user": "c", "cmd": "bwrap --bind /home/c/Desktop-Agent-work/t-run/repo /work claude -p", "ticks": 9, "started_at": time.time() - 300}]
    # beta: dispatcher stopped with READY work while quota permits (its own feed says so)
    st.set_kv("feed:beta", {"fetched_at": time.time(), "url": "http://127.0.0.1:1/", "data": {"work": {"dispatcher_running": False, "stalled_workers": [], "approved_ready_rows_for_specialist_lanes": 2},
                                                                                           "quota_allows_a_turn": True, "last_check": "2026-10-09T20:24:10Z"}})
    # gamma: genuinely idle, but a READY control-plane task waits behind the quota pause
    st.save_task("t-ready", "g", "gamma", "READY", {"objective": "Docs touch-up", "model_class": "cloud_cheap", "attempt": 1}, "h")
    # inbox: a bounced delivery vs an acknowledged one; one outstanding decision
    st.save_intake({"item_id": "da-bounced000", "project": "alpha", "repo": "x/alpha", "issue": 1, "kind": "comment", "gh_id": 1, "author": "caosos",
                    "title": "Bounced note", "body": "b", "url": "https://x/i/1", "posted_at": "2026-10-09T00:00:00Z", "status": "RECEIVED", "coordinator": "claude_peer"})
    st.set_intake_status("da-bounced000", "RECEIVED", "delivery failed: no live session"); st.flag_intake("da-bounced000")
    st.save_intake({"item_id": "da-acked00000", "project": "alpha", "repo": "x/alpha", "issue": 1, "kind": "comment", "gh_id": 2, "author": "caosos",
                    "title": "Acked note", "body": "b", "url": "https://x/i/2", "posted_at": "2026-10-09T00:00:00Z", "status": "ACKNOWLEDGED", "coordinator": "claude_peer"})
    s.ask_owner(question="Optional: approve the pilot?", options=["yes", "not now"], why="w", source="test")
    return s


@pytest.fixture
def served(tmp_path):
    import uvicorn
    s = _seed(tmp_path)
    server = uvicorn.Server(uvicorn.Config(build_app(s, "tok"), host="127.0.0.1", port=PORT, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    for _ in range(50):
        if server.started: break
        time.sleep(0.1)
    yield s
    server.should_exit = True; th.join(timeout=5)


def test_owner_view_states_and_evidence_on_demand(served):
    s = served
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v0/tasks/t-done", headers={"Authorization": "Bearer tok"})
    d = json.loads(urllib.request.urlopen(req).read())
    assert [x["stage"] for x in d["stage_timeline"]][0] == "READING" and d["stage_timeline"][-1]["stage"] == "DONE"
    assert d["files_changed_events"] == 1 and all("evidence" in e and "source" in e for e in d["events"]) and d["cost_usd"] == 0.0172
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 1648, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/?access_token=tok")
        page.wait_for_function("document.querySelectorAll('#o_projects .ocard').length === 3 && document.querySelector('#o_decisions').textContent.includes('pilot')", timeout=15000)
        for w, h in ((1648, 900), (1280, 900), (390, 844)):
            page.set_viewport_size({"width": w, "height": h}); page.wait_for_timeout(250)
            cards = page.inner_text("#o_projects")
            alpha, beta, gamma = cards.split("beta")[0], cards.split("beta")[1].split("gamma")[0], cards.split("gamma")[1]
            assert "DOWN" in alpha and "2 running now" in alpha and "rq-050-claim" in alpha and "t-run" in alpha           # idle/absent coordinator ≠ idle workers
            assert "awaiting your merge" in alpha and "evidence" in alpha and "delivered but not acknowledged" in alpha
            assert "dispatcher stopped — 2 approved row(s) queued" in beta and "attention" in beta
            assert "waiting — scheduler paused" in gamma and "provider limit" in gamma
            assert "Add the labels" in page.inner_text("#o_done") and "PR open, not merged" in page.inner_text("#o_done") and "merged" in page.inner_text("#o_done")
            assert "Optional: approve the pilot?" in page.inner_text("#o_decisions") and "amber alert" in page.inner_text("#o_next")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), f"overflow at {w}"
            assert page.evaluate("[...document.querySelectorAll('button')].filter(b => b.getBoundingClientRect().width > 0 && (b.getBoundingClientRect().width < 48 || b.getBoundingClientRect().height > 48)).length") == 0
            if w >= 1280:
                assert page.evaluate("document.querySelector('#owner').getBoundingClientRect().bottom") <= h + 160
            page.screenshot(path=str(Path(s.cfg.data_dir) / f"owner-states-{w}.png"), full_page=True)
        # evidence only on demand: the ✓ Today row's "evidence" link opens the Details view on the task's trace
        page.set_viewport_size({"width": 1648, "height": 900})
        page.click("#o_done a.evidence[data-t='t-done']")
        page.wait_for_function("document.body.dataset.mode === 'details' && !document.querySelector('#detail').hidden && document.querySelector('#d_timeline').textContent.includes('DONE')", timeout=15000)
        assert "READING" in page.inner_text("#d_timeline") and "cost $0.02" in page.inner_text("#d_summary")
        page.click("#detail summary")
        assert "worker/test" in page.inner_text("#d_trace") and "ev0" in page.inner_text("#d_trace")
        assert "UNACKNOWLEDGED" in page.inner_text("#intake") and "ACKNOWLEDGED" in page.inner_text("#intake")
        page.screenshot(path=str(Path(s.cfg.data_dir) / "owner-states-evidence.png"), full_page=True)
        browser.close()
