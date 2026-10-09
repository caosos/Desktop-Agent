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
    # a delivery that failed: stays RECEIVED with the failure note, flagged as unacknowledged; never shown DELIVERED
    st.save_intake({"item_id": "da-bbbbbbbbbb", "project": "alpha", "repo": "x/alpha", "issue": 1, "kind": "comment", "gh_id": 2, "author": "caosos",
                    "title": "Owner note the coordinator never got", "body": "b", "url": "https://example.test/i/2/very/long/url/that/must/wrap/inside/the/row/" + "x" * 80,
                    "posted_at": "2026-10-09T00:00:00Z", "status": "RECEIVED", "coordinator": "claude_peer"})
    st.set_intake_status("da-bbbbbbbbbb", "RECEIVED", "delivery failed: no live session for alpha (cwd /nowhere)")
    st.flag_intake("da-bbbbbbbbbb")
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
        assert "Approve the pilot?" in page.inner_text("#g_next") and "approval packet" in page.inner_text("#g_next")
        assert page.locator("#g_next_ctl button").count() == 0 and page.locator("#g_next_ctl a[href='#packetcard']").count() == 1   # one place to answer
        assert "NaN" not in page.inner_text("body") and "UNKNOWN $" in page.inner_text("#costs")
        assert "Submit goal" in page.inner_text("#legend") and "Transfer / Send" in page.inner_text("#legend")
        assert "PR open, not merged" in page.inner_text("#g_done") or "merge not checked" in page.inner_text("#g_done")
        assert "control-plane stage" in page.inner_text("#projects") and "workers now: UNKNOWN" in page.inner_text("#projects")
        # coordinators: alpha disconnected (no session at /nowhere), beta no integration → truthful labels
        coords = page.inner_text("#coords")
        assert "Disconnected" in coords and "Data unavailable" in coords and "VERIFIED" not in coords
        # WORKERS shown separately from the coordinator: alpha's RUNNING record (process not found, said so);
        # beta has no telemetry and no records → "not verified", never a count
        alpha_row, beta_row = coords.split("beta")[0], coords.split("beta")[1]
        assert "WORKERS" in alpha_row and "RUNNING" in alpha_row and "t-run" in alpha_row and "worker process not found" in alpha_row
        assert "WORKERS" in beta_row and "UNKNOWN" in beta_row and "worker runtime not verified" in beta_row
        # shared inbox shows the delivered item with Defer; Defer records a receipt
        assert "Owner note about labels" in page.inner_text("#intake") and "DELIVERED" in page.inner_text("#intake")
        # a failed delivery is shown as RECEIVED with its failure note and UNACKNOWLEDGED, never as DELIVERED
        failed_row = page.locator("#intake .row", has_text="never got")
        assert failed_row.count() == 1 and "RECEIVED" in failed_row.inner_text() and "delivery failed" in failed_row.inner_text()
        assert "UNACKNOWLEDGED" in failed_row.inner_text() and "DELIVERED" not in failed_row.inner_text().replace("UNACKNOWLEDGED", "")
        page.once("dialog", lambda d: d.accept("later"))
        page.click("#intake .row:has-text('about labels') button[data-a='defer']")
        page.wait_for_function("document.querySelector('#intake').textContent.includes('DEFERRED')", timeout=15000)
        assert s.store.get_intake("da-aaaaaaaaaa")["status"] == "DEFERRED"
        assert any(r["result_label"] == "verified" and "DEFERRED" in r["claim"] for r in s.store.receipts("instruction", "da-aaaaaaaaaa"))
        # answer the one next action in the approval packet (the single place to answer); recorded with the panel as source
        page.wait_for_function("document.querySelectorAll('#packet fieldset').length === 1", timeout=15000)
        did = s.store.open_decisions()[0]["decision_id"]
        page.check(f"#packet input[name='{did}'][value='approve']"); page.click("#packet_submit")
        page.wait_for_function("document.querySelector('#g_next').textContent.includes('blocked') || document.querySelector('#g_next').textContent.includes('Nothing')", timeout=15000)
        assert s.store.open_decisions() == []
        ev = [e for e in s.store.events() if e.type == ET.OWNER_DECISION_RECORDED.value][-1]
        assert ev.payload["answer"] == "approve" and ev.provenance.source == "panel"
        # next action now points at the blocked task (no decisions left)
        assert "blocked task t-blk" in page.inner_text("#g_next")
        page.screenshot(path=str(Path(s.cfg.data_dir) / "panel-acceptance.png"), full_page=True)
        # reload keeps the same picture (state is server truth, not page memory)
        page.reload(); page.wait_for_function("document.querySelector('#g_next').textContent.length > 0", timeout=15000)
        assert page.inner_text("#g_done_n") == "1" and page.inner_text("#g_blocked_n") == "1" and "DEFERRED" in page.inner_text("#intake")
        # desktop 1648, laptop 1280, phone 390: no sideways scroll, no letter-stacked controls (every visible button
        # at least 48 px wide and at most 48 px tall: a stacked label is ~40 px wide and 100+ px tall), the packet spans the full row on wide screens
        for w, h in ((1648, 900), (1280, 900), (390, 844)):
            page.set_viewport_size({"width": w, "height": h}); page.wait_for_timeout(300)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), f"horizontal overflow at {w}px"
            bad = page.evaluate("[...document.querySelectorAll('button')].map(b => [b.textContent.trim(), b.getBoundingClientRect().width, b.getBoundingClientRect().height]).filter(x => x[1] > 0 && (x[1] < 48 || x[2] > 48))")
            assert bad == [], f"squeezed buttons at {w}px: {bad}"
            if w >= 1280:
                assert page.evaluate("document.querySelector('#packetcard').getBoundingClientRect().width > document.querySelector('.wrap').getBoundingClientRect().width * 0.95")
                assert page.evaluate("document.querySelector('.wrap').getBoundingClientRect().width") >= min(w - 32, 1500)
            page.screenshot(path=str(Path(s.cfg.data_dir) / f"panel-acceptance-{w}.png"), full_page=True)
        browser.close()
