"""Real browser acceptance of the owner-facing panel: headless Chrome (Playwright, system channel)
against the live API served in-process over a seeded store. Skipped when Chrome or Playwright is absent.
Covers: token sign-in, at-a-glance strip, coordinator statuses, Shared Inbox items, Defer (receipted),
answering the one next action (receipted), Disconnected shown truthfully. No network beyond localhost."""
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.events import EventType as ET
from desktop_agent.control.service import Service

PORT = 8499


def _seed(tmp_path: Path) -> Service:
    for name, extra in (("alpha", "github_repo: x/alpha\nintake:\n  issues: [1]\n  coordinator: {kind: claude_peer, session_cwd: /nowhere}\n"),
                        ("beta", "")):
        repo = tmp_path / name; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
        (tmp_path / f"{name}.yaml").write_text(f"name: {name}\nrepo_path: {repo}\nremote_url: x\nintegration_branch: main\nstart_here: S\nagents_file: A\n{extra}")
    cfg = RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t",
                        project_files=[tmp_path / "alpha.yaml", tmp_path / "beta.yaml"])
    s = Service(cfg); s.scheduler.paused = True
    st = s.store
    st.save_task("t-done", "g", "alpha", "DONE", {"objective": "Add the labels", "model_class": "cloud_cheap", "attempt": 1}, "h",
                 {"integration": {"pr_url": "https://example.test/pr/1"}})
    st.save_task("t-run", "g", "alpha", "RUNNING", {"objective": "Fix the parser", "model_class": "cloud_strong", "attempt": 1}, "h")
    st.save_task("t-blk", "g", "beta", "BLOCKED", {"objective": "Needs a key", "model_class": "cloud_cheap", "attempt": 1}, "h", {"reason": "credential missing"})
    s.ask_owner(question="Approve the pilot?", options=["approve", "not yet"], why="w", source="test", recommendation="approve")
    st.save_intake({"item_id": "da-aaaaaaaaaa", "project": "alpha", "repo": "x/alpha", "issue": 1, "kind": "comment", "gh_id": 1, "author": "caosos",
                    "title": "Owner note about labels", "body": "b", "url": "https://example.test/i/1", "posted_at": "2026-10-09T00:00:00Z", "status": "DELIVERED", "coordinator": "claude_peer"})
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


def test_owner_panel_acceptance(served):
    s = served
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 1100, "height": 1600})
        page.goto(f"http://127.0.0.1:{PORT}/?access_token=tok")
        page.wait_for_function("document.querySelector('#conn') && document.querySelector('#conn').textContent === 'live'", timeout=15000)
        page.wait_for_function("document.querySelector('#g_next').textContent.length > 0", timeout=15000)
        # at a glance: done with evidence, working, blocked, one next action
        assert page.inner_text("#g_done_n") == "1" and "Add the labels" in page.inner_text("#g_done") and page.locator("#g_done a").count() == 1
        assert page.inner_text("#g_working_n") == "1" and "Fix the parser" in page.inner_text("#g_working")
        assert page.inner_text("#g_blocked_n") == "1" and "credential missing" in page.inner_text("#g_blocked")
        assert "Approve the pilot?" in page.inner_text("#g_next")
        # coordinators: alpha disconnected (no session at /nowhere), beta no integration → truthful labels
        coords = page.inner_text("#coords")
        assert "Disconnected" in coords and "Data unavailable" in coords and "VERIFIED" not in coords
        # shared inbox shows the delivered item with Defer; Defer records a receipt
        assert "Owner note about labels" in page.inner_text("#intake") and "DELIVERED" in page.inner_text("#intake")
        page.once("dialog", lambda d: d.accept("later"))
        page.click("#intake button[data-a='defer']")
        page.wait_for_function("document.querySelector('#intake').textContent.includes('DEFERRED')", timeout=15000)
        assert s.store.get_intake("da-aaaaaaaaaa")["status"] == "DEFERRED"
        assert any(r["result_label"] == "verified" and "DEFERRED" in r["claim"] for r in s.store.receipts("instruction", "da-aaaaaaaaaa"))
        # answer the one next action from the glance strip; the decision is recorded with the panel as source
        page.click("#g_next_ctl button[data-a='approve']")
        page.wait_for_function("document.querySelector('#g_next').textContent.includes('blocked') || document.querySelector('#g_next').textContent.includes('Nothing')", timeout=15000)
        assert s.store.open_decisions() == []
        ev = [e for e in s.store.events() if e.type == ET.OWNER_DECISION_RECORDED.value][-1]
        assert ev.payload["answer"] == "approve" and ev.provenance.source == "panel"
        # next action now points at the blocked task (no decisions left)
        assert "blocked task t-blk" in page.inner_text("#g_next")
        page.screenshot(path=str(Path(s.cfg.data_dir) / "panel-acceptance.png"), full_page=True)
        browser.close()
