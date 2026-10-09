"""Ask Aria in the panel: deterministic no-model mode, project-grounded answers through project_brief,
persisted transcript, and dictation driven by a fake browser speech engine (permission denied, no engine,
long dictation with pauses and engine restarts, correction then explicit Send, reload) at three widths."""
import asyncio
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.llm import Completion
from desktop_agent.control.service import Service

PORT = 8496


class FakeLLM:
    def __init__(self, answers, ok=True): self.answers = list(answers); self.prompts = []; self.ok = ok; self.backend = "fake"
    def model_for(self, mc): return "fake-model"
    def available(self, backend): return self.ok
    def refresh_backend(self): return self.backend
    async def complete(self, prompt, schema, model_class="cloud_cheap", system=None, timeout=0):
        self.prompts.append(prompt)
        return Completion(data=self.answers.pop(0), model="fake-model", backend="fake", cost_usd=0.002, input_tokens=5, output_tokens=5, duration_ms=1)


def _service(tmp_path: Path) -> Service:
    repo = tmp_path / "mbos"; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
    (tmp_path / "m.yaml").write_text(f"name: mbos\nrepo_path: {repo}\nremote_url: x\nintegration_branch: main\nstart_here: S\nagents_file: A\nmission: test mission\n"
                                     "workers:\n  process_patterns: ['tools/worker\\.py (?P<id>[A-Z]-\\d+)(?: --lane (?P<lane>\\d+))?']\n")
    s = Service(RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t", project_files=[tmp_path / "m.yaml"]))
    s.scheduler.paused = True
    s.workers.procs_fn = lambda: [{"pid": 77, "ppid": 1, "user": "m", "cmd": "python -I tools/worker.py F-39 --lane 06", "ticks": 3, "started_at": time.time() - 120}]
    return s


def test_no_model_mode_is_deterministic_and_project_brief_grounds_answers(tmp_path):
    s = _service(tmp_path)
    s.aria.llm = FakeLLM([], ok=False)
    r = asyncio.run(s.aria.chat("What is F-39 doing?"))
    assert r["kind"] == "status (no model)" and r["cost_usd"] == 0.0 and "requires model approval" in r["reply"] and "mbos workers running (1)" in r["reply"]
    assert s.aria.status()["available"] is False and s.aria.status()["reason"]
    s.aria.llm = FakeLLM([{"reply": "Checking the project.", "action": {"name": "project_brief", "args": {"project": "mbos"}}},
                          {"reply": "F-39 is running on lane 06 (host process pid 77).", "action": None}])
    r = asyncio.run(s.aria.chat("What is F-39 doing?"))
    assert r["kind"] == "question" and r["actions"][0]["name"] == "project_brief" and r["actions"][0]["ok"] and "pid 77" in s.aria.llm.prompts[1]
    assert "F-39" in s.aria.llm.prompts[1] and "RUNNING" in s.aria.llm.prompts[1]
    # transcript persisted and recoverable through a fresh brain over the same store
    from desktop_agent.control.aria import AriaBrain
    fresh = AriaBrain(s, llm=s.aria.llm)
    assert [t["role"] for t in fresh.transcript()] == ["owner", "aria", "owner", "aria"] and fresh.transcript()[-1]["kind"] == "question"
    with TestClient(build_app(s, "tok")) as c:
        h = {"Authorization": "Bearer tok"}
        st = c.get("/v0/aria/status", headers=h).json()
        assert st["available"] and st["dictation"]["mode"] == "browser_speech" and "Google" in st["dictation"]["disclosure"] and "not installed" in st["dictation"]["on_device_alternative"]
        assert len(c.get("/v0/aria/history", headers=h).json()["turns"]) == 4


FAKE_SR = """
window.__sr = [];
class FakeSR {
  constructor() { this.continuous = false; this.interimResults = false; this.started = 0; window.__sr.push(this); }
  start() { this.started++; if (window.__srMode === 'denied') { setTimeout(() => this.onerror({error: 'not-allowed'}), 10); setTimeout(() => this.onend(), 20); } }
  stop() { setTimeout(() => this.onend(), 10); }
  emit(finals, interim) { const results = finals.map(t => Object.assign([{transcript: t}], {isFinal: true})); if (interim) results.push(Object.assign([{transcript: interim}], {isFinal: false})); this.onresult({results}); }
}
if (window.__srMode !== 'none') { window.webkitSpeechRecognition = FakeSR; window.SpeechRecognition = FakeSR; }
"""


@pytest.fixture
def served(tmp_path):
    import uvicorn
    s = _service(tmp_path)
    s.aria.llm = FakeLLM([{"reply": "F-39 is running on lane 06 of mbos; one host process, pid 77, alive two minutes.", "action": {"name": "project_brief", "args": {"project": "mbos"}}},
                          {"reply": "F-39 is running on lane 06 (host process pid 77, alive 2 min). Source: host process list.", "action": None}])
    server = uvicorn.Server(uvicorn.Config(build_app(s, "tok"), host="127.0.0.1", port=PORT, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    for _ in range(50):
        if server.started: break
        time.sleep(0.1)
    yield s
    server.should_exit = True; th.join(timeout=5)


def test_dictation_states_and_send_in_browser(served):
    s = served
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome: {exc}")
        # (a) permission denied
        ctx = browser.new_context(viewport={"width": 1648, "height": 900}); ctx.add_init_script("window.__srMode='denied';" + FAKE_SR)
        page = ctx.new_page(); page.goto(f"http://127.0.0.1:{PORT}/?access_token=tok")
        page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=15000)
        page.click("#aria_bubble"); page.wait_for_function("!document.querySelector('#aria').hidden && document.querySelector('#aria_model').textContent.includes('fake-model')", timeout=15000)
        page.click("#mic"); page.wait_for_function("document.querySelector('#mic_state').textContent.includes('permission denied')", timeout=5000)
        assert page.get_attribute("#mic", "aria-pressed") == "false" and page.input_value("#aria_text") == ""
        ctx.close()
        # (b) no engine at all → honest unavailable state, typing still works
        ctx = browser.new_context(viewport={"width": 390, "height": 844}); ctx.add_init_script("window.__srMode='none'; delete window.webkitSpeechRecognition; delete window.SpeechRecognition;")
        page = ctx.new_page(); page.goto(f"http://127.0.0.1:{PORT}/?access_token=tok")
        page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=15000)
        page.click("#aria_bubble"); page.wait_for_function("!document.querySelector('#aria').hidden", timeout=15000)
        page.click("#mic"); assert "no speech recognition" in page.inner_text("#mic_state") and page.is_disabled("#mic")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.fill("#aria_text", "typed instead"); assert page.input_value("#aria_text") == "typed instead"
        ctx.close()
        # (c)+(d)+(e)+(f): long dictation with pauses and engine restarts, correction, explicit Send, reload keeps the transcript
        for w, h in ((1648, 900), (1280, 900), (390, 844)):
            ctx = browser.new_context(viewport={"width": w, "height": h}); ctx.add_init_script("window.__srMode='ok';" + FAKE_SR)
            page = ctx.new_page(); page.goto(f"http://127.0.0.1:{PORT}/?access_token=tok")
            page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=15000)
            page.click("#aria_bubble"); page.wait_for_function("!document.querySelector('#aria').hidden", timeout=15000)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), f"drawer overflow at {w}"
            if w != 1648:
                ctx.close(); continue
            page.click("#mic"); page.wait_for_function("document.querySelector('#mic').getAttribute('aria-pressed') === 'true'", timeout=5000)
            assert "Listening" in page.inner_text("#mic_state") and page.evaluate("window.__sr[0].continuous && window.__sr[0].interimResults")
            page.evaluate("window.__sr[0].emit([], 'what is')"); assert "Transcribing" in page.inner_text("#mic_state") and "what is" in page.inner_text("#interim")
            page.evaluate("window.__sr[0].emit(['What is agent six doing right now?'], '')")
            assert page.input_value("#aria_text").strip() == "What is agent six doing right now?"
            page.evaluate("window.__sr[0].onend()")                                          # the engine stops after a pause: not the owner's Stop
            page.wait_for_function("window.__sr[0].started === 2", timeout=5000)             # … so we restart and keep listening
            assert page.get_attribute("#mic", "aria-pressed") == "true"
            page.evaluate("window.__sr[0].emit(['And is anything blocked on lane six?'], '')")
            assert page.input_value("#aria_text").strip() == "What is agent six doing right now? And is anything blocked on lane six?"
            page.click("#mic"); page.wait_for_function("document.querySelector('#mic').getAttribute('aria-pressed') === 'false'", timeout=5000)
            assert "Text ready" in page.inner_text("#mic_state") and page.locator("#aria_log .turn").count() == 0      # nothing sent by itself
            page.fill("#aria_text", "What is F-39 doing right now?")                              # the owner corrects the text
            page.click("#aria_send")
            page.wait_for_function("document.querySelectorAll('#aria_log .turn.aria').length === 1", timeout=20000)
            t = page.inner_text("#aria_log")
            assert "What is F-39 doing right now?" in t and "pid 77" in t and "project_brief ✓" in t and "question" in t
            page.reload(); page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=15000)
            page.click("#aria_bubble"); page.wait_for_function("document.querySelectorAll('#aria_log .turn').length === 2", timeout=15000)
            page.screenshot(path=str(Path(s.cfg.data_dir) / "ask-aria-1648.png"), full_page=False)
            ctx.close()
        # (g) task-confirmation boundary: the deterministic no-model mode never acts
        s.aria.llm = FakeLLM([], ok=False)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}); page = ctx.new_page(); page.goto(f"http://127.0.0.1:{PORT}/?access_token=tok")
        page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=15000)
        page.click("#aria_bubble"); page.wait_for_function("document.querySelector('#aria_model').textContent.includes('no model permitted')", timeout=15000)
        page.fill("#aria_text", "Start a new research task and pay for it"); page.click("#aria_send")
        page.wait_for_function("document.querySelector('#aria_log').textContent.includes('requires model approval')", timeout=15000)
        assert s.store.list_tasks() == [] and "status (no model)" in page.inner_text("#aria_log")
        page.screenshot(path=str(Path(s.cfg.data_dir) / "ask-aria-no-model-1280.png"), full_page=False)
        ctx.close(); browser.close()
