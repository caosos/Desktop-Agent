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
    async def complete(self, prompt, schema, model_class="cloud_cheap", system=None, timeout=0, purpose=""):
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
    assert r["kind"] == "status (no model)" and r["usage"]["billing"] == "none" and "requires model approval" in r["reply"] and "mbos workers running (1)" in r["reply"]
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
// Models the real Web Speech contract: `results` is CUMULATIVE for the session (every event carries all earlier
// results), `resultIndex` is the first changed index, an interim result at index i is later REPLACED by the final
// at the same index, and a new session after onend starts again at index 0.
window.__sr = [];
class FakeSR {
  constructor() { this.continuous = false; this.interimResults = false; this.started = 0; this.results = []; window.__sr.push(this); }
  start() { this.started++; this.results = []; if (window.__srMode === 'denied') { setTimeout(() => this.onerror({error: 'not-allowed'}), 10); setTimeout(() => this.onend(), 20); } }
  stop() { setTimeout(() => this.onend(), 10); }
  say(index, transcript, isFinal) { this.results[index] = Object.assign([{transcript}], {isFinal}); this.onresult({results: this.results, resultIndex: index}); }
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
            sr = "window.__sr[window.__sr.length-1]"
            page.evaluate(sr + ".say(0, 'what is', false)"); assert "Transcribing" in page.inner_text("#mic_state") and "what is" in page.inner_text("#interim")
            assert page.input_value("#aria_text").strip() == "what is"   # interim words are visible without a pause
            page.evaluate(sr + ".say(0, 'What is agent six doing right now?', true)")        # interim → final at the same index
            assert page.input_value("#aria_text").strip() == "What is agent six doing right now?"
            page.evaluate(sr + ".say(1, 'and is', false)")                                     # the cumulative list still carries index 0
            assert page.input_value("#aria_text").strip() == "What is agent six doing right now? and is"
            page.evaluate(sr + ".say(1, 'And is anything blocked on lane six?', true)")
            assert page.input_value("#aria_text").strip() == "What is agent six doing right now? And is anything blocked on lane six?", page.input_value("#aria_text")
            page.evaluate(sr + ".say(2, 'Aria Aria', true)")                                   # legitimately repeated words survive, once
            page.evaluate(sr + ".say(2, 'Aria Aria', true)")                                   # a re-delivered final at the same index does not duplicate
            assert page.input_value("#aria_text").strip() == "What is agent six doing right now? And is anything blocked on lane six? Aria Aria"
            page.evaluate(sr + ".onend()")                                                     # the engine stops after a pause: not the owner's Stop
            page.wait_for_function("window.__sr[0].started === 2", timeout=5000)             # … so we restart and keep listening
            assert page.get_attribute("#mic", "aria-pressed") == "true"
            page.evaluate(sr + ".say(0, 'Second session sentence.', true)")                  # new session: indices start at 0 again, text must append
            assert page.input_value("#aria_text").strip() == "What is agent six doing right now? And is anything blocked on lane six? Aria Aria Second session sentence."
            page.click("#mic"); page.wait_for_function("document.querySelector('#mic').getAttribute('aria-pressed') === 'false'", timeout=5000)
            ms = page.inner_text("#mic_state")
            assert "Text ready" in ms and "first words" in ms and "4 segments" in ms and "after Stop" in ms and page.locator("#aria_log .turn").count() == 0   # nothing sent by itself
            page.click("#aria summary:has-text('Dictation log')")
            log = page.inner_text("#dict_log")
            assert "first words heard after" in log and "segment 1 committed" in log and "segment 4 committed" in log and "engine paused after silence; restarted" in log and "Stop clicked" in log and "session closed" in log
            assert log.count("committed") == 4, log                                                           # the re-delivered final did not count twice
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


# ---- Lost first Send (owner item da-66d1cd7470) -------------------------------------------------------------------
class StoreCheckingLLM(FakeLLM):
    """Looks at a fresh brain over the same store while the model call is in flight: the owner turn must already be there."""
    def __init__(self, service, answers): super().__init__(answers); self.service = service; self.seen_in_store = None
    async def complete(self, prompt, schema, model_class="cloud_cheap", system=None, timeout=0, purpose=""):
        from desktop_agent.control.aria import AriaBrain
        fresh = AriaBrain(self.service, llm=self)
        self.seen_in_store = [t["text"] for t in fresh.transcript() if t["role"] == "owner"]
        return await super().complete(prompt, schema, model_class, system, timeout)


class ExplodingLLM(FakeLLM):
    async def complete(self, *a, **k): raise RuntimeError("boom: not an LLMError")


def test_owner_turn_is_in_the_store_before_the_model_answers_and_after_an_unhandled_failure(tmp_path):
    from desktop_agent.control.aria import AriaBrain
    s = _service(tmp_path)
    s.aria.llm = StoreCheckingLLM(s, [{"reply": "Nothing finished.", "action": None}])
    r = asyncio.run(s.aria.chat("What finished today?"))
    assert r["reply"] == "Nothing finished." and s.aria.llm.seen_in_store == ["What finished today?"], "request must be persisted on arrival, not after the reply"
    s.aria.llm = ExplodingLLM([])
    with pytest.raises(RuntimeError):
        asyncio.run(s.aria.chat("Second question"))
    turns = AriaBrain(s, llm=s.aria.llm).transcript()
    assert [t["text"] for t in turns if t["role"] == "owner"] == ["What finished today?", "Second question"]
    assert turns[-1]["role"] == "aria" and turns[-1]["kind"] == "error" and "RuntimeError" in turns[-1]["text"]


def test_typing_right_after_opening_the_drawer_reaches_the_box_even_when_status_loads_slowly(served):
    """Reproduces the lost first Send: the drawer used to focus the box only after the status and history fetches, so
    keystrokes typed meanwhile went to the bubble button; a whole string typed at once left the box empty and Send
    returned silently (no request, nothing in the store)."""
    s = served
    s.aria.llm = FakeLLM([{"reply": "Four tasks finished today.", "action": None}])
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome: {exc}")
        ctx = browser.new_context(viewport={"width": 1648, "height": 900}); page = ctx.new_page()
        posted = []
        page.on("request", lambda r: posted.append(r.post_data) if r.method == "POST" and r.url.endswith("/v0/aria/chat") else None)
        def slow(route): time.sleep(1.2); route.continue_()
        page.route("**/v0/aria/status", slow)                              # a slow or far-away control plane right after a page load
        page.goto(f"http://127.0.0.1:{PORT}/?access_token=tok")
        page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=15000)
        page.click("#aria_bubble", no_wait_after=True)
        page.keyboard.type("What finished today?")                           # typed at once, before the status fetch returns
        page.click("#aria_send", no_wait_after=True)
        page.wait_for_function("document.querySelector('#aria_send').textContent === 'Send' && document.querySelector('#aria_log').textContent.includes('Four tasks')", timeout=15000)
        import json as _json
        assert posted and _json.loads(posted[0])["text"] == "What finished today?", posted
        assert [t["text"] for t in s.aria.transcript() if t["role"] == "owner"][-1] == "What finished today?"
        # an empty Send says so instead of doing nothing silently
        page.click("#aria_send"); assert "Nothing to send yet" in page.inner_text("#mic_state")
        ctx.close(); browser.close()
