"""Ask Aria message / progress / cost reliability (owner item da-22688a275b): a sent message survives a late history
response; the lifecycle line says what actually happened; the model, backend and cost kind are persisted on the reply
and shown after a reload; the status header makes no unsupported cost promise."""
import asyncio
import time

import pytest

from desktop_agent.control.aria import AriaBrain
from desktop_agent.control.aria_usage import cost_basis, status_cost_note
from desktop_agent.control.llm import Completion, LLMError
from tests.test_ask_aria import FakeLLM, PORT, _service, served  # noqa: F401  (fixture re-used)


class TokenEquivalentLLM(FakeLLM):
    async def complete(self, prompt, schema, model_class="cloud_cheap", system=None, timeout=0):
        self.prompts.append(prompt)
        return Completion(data=self.answers.pop(0), model="claude-haiku-5-5", backend="claude_cli", cost_usd=0.119, input_tokens=201085, output_tokens=965, duration_ms=11000)


class FailingLLM(FakeLLM):
    async def complete(self, *a, **k): raise LLMError("claude_cli error: boom")


def test_cost_basis_keeps_token_equivalent_metered_and_unknown_apart():
    assert cost_basis("claude_cli", 0.119) == "token-equivalent" and cost_basis("openai_api", 0.004) == "metered"
    assert cost_basis("claude_cli", None) == "unknown" and cost_basis(None, 0.0) == "none"
    assert "about a cent" not in status_cost_note("claude_cli", True) and "not an invoice" in status_cost_note("claude_cli", True)
    assert status_cost_note("claude_cli", False) == ""


def test_reply_usage_and_client_id_are_persisted_and_survive_a_fresh_brain(tmp_path):
    s = _service(tmp_path)
    s.aria.llm = TokenEquivalentLLM([{"reply": "Yes, running.", "action": None}])
    r = asyncio.run(s.aria.chat("is this working now", "client-abc"))
    u = r["usage"]
    assert (u["model"], u["backend"], u["cost_usd"], u["cost_basis"], u["rounds"]) == ("claude-haiku-5-5", "claude_cli", 0.119, "token-equivalent", 1)
    assert u["tokens"] == {"in": 201085, "out": 965} and r["cost_usd"] == 0.119 and r["model"] == "claude-haiku-5-5"
    turns = AriaBrain(s, llm=s.aria.llm).transcript()                      # what a reload reads from the store
    assert turns[0]["role"] == "owner" and turns[0]["client_id"] == "client-abc" and turns[1]["usage"]["cost_basis"] == "token-equivalent"
    assert "about a cent" not in s.aria.status()["cost_note"]


def test_model_failure_is_an_error_turn_with_retry_count_and_no_invented_cost(tmp_path):
    s = _service(tmp_path)
    s.aria.llm = FailingLLM([])
    r = asyncio.run(s.aria.chat("is this working now", "c1"))
    assert r["kind"] == "error" and "Model call failed twice" in r["reply"]
    assert r["usage"]["rounds"] == 0 and r["usage"]["retries"] == 1 and r["usage"]["cost_usd"] == 0.0
    assert [t["kind"] for t in s.aria.transcript() if t["role"] == "aria"] == ["error"]


def test_no_model_mode_says_no_model_call(tmp_path):
    s = _service(tmp_path)
    s.aria.llm = FakeLLM([], ok=False)
    r = asyncio.run(s.aria.chat("status?"))
    assert r["usage"]["cost_basis"] == "none" and r["usage"]["backend"] is None


INIT_DELAY = "const _f = window.fetch; window.fetch = async (u, o) => { const r = await _f(u, o); if (String(u).includes('/v0/aria/history')) await new Promise(x => setTimeout(x, 3000)); return r; };"


def _open(page, token="tok"):
    page.goto(f"http://127.0.0.1:{PORT}/?access_token={token}")
    page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=15000)


def test_late_history_response_never_removes_a_sent_message_and_reload_shows_it_once(served):
    """Reproduced live in Claude in Chrome 2026-10-11: with a slow history response (content built before the send),
    the sent message vanished from the transcript while the button still said thinking."""
    s = served
    s.aria.llm = TokenEquivalentLLM([{"reply": "Yes, the control plane is running.", "action": None}])
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome: {exc}")
        ctx = browser.new_context(viewport={"width": 1648, "height": 900}); ctx.add_init_script(INIT_DELAY)
        page = ctx.new_page(); _open(page)
        text = "is this working now"
        page.click("#aria_bubble"); page.wait_for_timeout(400)               # the history request is in flight; its response is built already
        page.fill("#aria_text", text); page.click("#aria_send")
        assert text in page.inner_text("#aria_log")
        page.wait_for_timeout(3600)                                           # the late history response has been applied
        assert text in page.inner_text("#aria_log"), "a late history response removed the sent message"
        page.wait_for_function("document.querySelector('#aria_send').textContent === 'Send'", timeout=15000)
        log = page.inner_text("#aria_log")
        assert "Yes, the control plane is running." in log and text in log
        assert "claude-haiku-5-5" in log and "claude_cli" in log and "$0.119 token-equivalent, not an invoice" in log and "1 round" in log
        assert "Reply received in" in page.inner_text("#aria_state")
        assert "about a cent" not in page.inner_text("#aria_model")
        page.reload(); _open(page); page.click("#aria_bubble")
        page.wait_for_function(f"document.querySelector('#aria_log').innerText.includes({text!r})", timeout=15000)
        page.wait_for_timeout(3600)                                           # even with the delayed history on reload
        owners = page.evaluate("[...document.querySelectorAll('#aria_log .turn.owner')].filter(t => t.innerText.startsWith('is this working now')).length")
        assert owners == 1 and "$0.119 token-equivalent" in page.inner_text("#aria_log")   # recovered from the store exactly once, usage persisted
        ctx.close(); browser.close()
    assert [t["text"] for t in s.aria.transcript() if t["role"] == "owner"] == [text]


def test_failures_are_classified_and_say_whether_the_message_was_recorded(served):
    s = served
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome: {exc}")
        def fresh():
            ctx = browser.new_context(viewport={"width": 1648, "height": 900}); page = ctx.new_page(); _open(page)
            page.click("#aria_bubble"); page.wait_for_function("document.querySelector('#aria_model').textContent.includes('fake-model')", timeout=15000)
            return ctx, page
        def send(page, text):
            page.fill("#aria_text", text); page.click("#aria_send")
            page.wait_for_function("document.querySelector('#aria_send').textContent === 'Send'", timeout=20000)
            return page.inner_text("#aria_state")
        # (a) network failure before the request reaches the server: not recorded, text back in the box, no ghost turn
        ctx, page = fresh(); page.route("**/v0/aria/chat", lambda r: r.abort())
        state = send(page, "network case")
        assert "Could not reach the control plane (network or tunnel)" in state and "NOT recorded" in state
        assert page.input_value("#aria_text") == "network case" and "network case" not in page.inner_text("#aria_log"); ctx.close()
        # (b) service unavailable (HTTP 503), also before the server saw it
        ctx, page = fresh(); page.route("**/v0/aria/chat", lambda r: r.fulfill(status=503, body="down"))
        state = send(page, "unavailable case")
        assert "Service unavailable (HTTP 503)" in state and "NOT recorded" in state and page.input_value("#aria_text") == "unavailable case"; ctx.close()
        # (c) the server recorded and answered but the response was lost on the way back (HTTP 502): recorded, reload shows it
        s.aria.llm = FakeLLM([{"reply": "late answer", "action": None}])
        ctx, page = fresh(); page.route("**/v0/aria/chat", lambda r: (r.fetch(), r.fulfill(status=502, body="bad gateway"))[1])
        state = send(page, "lost response case")
        assert "Service unavailable (HTTP 502)" in state and "WAS recorded" in state and page.input_value("#aria_text") == ""
        assert [t["text"] for t in s.aria.transcript() if t["role"] == "owner"].count("lost response case") == 1; ctx.close()
        # (d) the model fails (twice): the control plane received the message; the lifecycle says model failure, nothing changed
        s.aria.llm = FailingLLM([])
        ctx, page = fresh()
        state = send(page, "model case")
        assert "Model failure" in state and "received your message" in state and "Model call failed twice" in page.inner_text("#aria_log"); ctx.close()
        browser.close()
