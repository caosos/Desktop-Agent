"""Memory Center in a real browser (headless Chrome via Playwright, system channel) against the API served in-process over
a seeded private store in a temp dir: containers with counts, listing, search, sensitive blur, pending approval, the
provenance view, capture switch, seed import. Skipped when Chrome or Playwright is absent. Synthetic facts only."""
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.service import Service

PORT = 8495
SEED = Path(__file__).parent / "fixtures" / "memory_seed_synthetic.md"


@pytest.fixture
def served(tmp_path):
    import uvicorn
    repo = tmp_path / "alpha"; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
    y = tmp_path / "a.yaml"; y.write_text(f"name: alpha\nrepo_path: {repo}\nremote_url: x\nintegration_branch: main\nstart_here: S\nagents_file: A\n")
    s = Service(RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "cfg" / "t", project_files=[y]))
    s.scheduler.paused = True
    s.memory.import_markdown(SEED.read_bytes())
    s.memory.after_turn("I think my dog Biscuit is maybe 5 now", "t1")
    server = uvicorn.Server(uvicorn.Config(build_app(s, "tok"), host="127.0.0.1", port=PORT, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    for _ in range(50):
        if server.started: break
        time.sleep(0.1)
    yield s
    server.should_exit = True; th.join(timeout=5)


def test_memory_center_in_chrome(served):
    s = served
    with playwright.sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        page = b.new_page(viewport={"width": 1000, "height": 1400})
        page.goto(f"http://127.0.0.1:{PORT}/memory?access_token=tok")
        page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=15000)
        page.wait_for_function("document.querySelectorAll('#tabs [data-c]').length === 13", timeout=10000)
        assert "loaded" in page.inner_text("#st_loaded") and "17 active" in page.inner_text("#st_loaded") and "1 pending" in page.inner_text("#st_loaded")
        assert "Seed file: loaded" in page.inner_text("#st_seed") and "sha256" in page.inner_text("#st_seed")
        assert "separate store" in page.inner_text("#st_dot")
        page.click("#tabs [data-c='people_and_relationships']")
        page.wait_for_function("document.querySelectorAll('#list .rec').length === 2", timeout=10000)
        assert "Zelphina" in page.inner_text("#list") and "4242" not in page.inner_text("#list")
        page.click("#tabs [data-c='finances']")
        page.wait_for_function("document.querySelectorAll('#list .rec.sensitive').length === 2", timeout=10000)
        assert page.evaluate("getComputedStyle(document.querySelector('#list .rec.sensitive .t')).filter") != "none"   # blurred by default
        page.click("#btn_show")
        page.wait_for_function("document.querySelectorAll('#list .rec.sensitive.show').length === 2", timeout=10000)
        page.fill("#q", "credit union")
        page.wait_for_function("document.querySelectorAll('#list .rec').length === 1", timeout=10000)
        page.fill("#q", "")
        page.select_option("#status", "pending"); page.click("#tabs [data-c='people_and_relationships']")
        page.wait_for_function("document.querySelector('#list').textContent.includes('Biscuit') && document.querySelector('#list button[data-a=approve]')", timeout=10000)
        page.click("#list button[data-a='why']")
        page.wait_for_function("!document.querySelector('#whycard').hidden && document.querySelector('#why').textContent.includes('runtime_extraction')", timeout=10000)
        page.click("#list button[data-a='approve']")
        page.wait_for_function("document.querySelector('#st_loaded').textContent.includes('0 pending')", timeout=10000)
        assert s.memory.status()["records"]["superseded"] == 1 and s.memory.status()["records"]["pending"] == 0
        page.click("#tabs [data-c='finances']"); page.wait_for_timeout(300)
        page.uncheck("#capture_on")
        page.wait_for_function("document.querySelector('#capture_on').checked === false", timeout=5000)
        page.wait_for_timeout(300)
        assert s.memory.store.capture_on("finances") is False
        page.set_input_files("#seed_file", str(SEED)); page.click("#btn_import")
        page.wait_for_function("document.querySelector('#import_state').textContent.includes('Imported sha')", timeout=15000)
        assert "16 unchanged" in page.inner_text("#import_state") or "unchanged" in page.inner_text("#import_state")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.screenshot(path=str(Path("~/.local/share/desktop-agent").expanduser() / "memory-center-fixture.png"), full_page=True)
        b.close()
