"""Milestone 2 (da-50b1e88f20): explicit "look at what I'm doing". Looking only: one stored screenshot with provenance, a grounded
explanation, same conversation, idempotent, no input injection, subscription route only. The capture source in the browser test is
a canvas stream standing in for the user's screen share; no real screen or paid call is used."""
import asyncio
import base64
import json
import os
import stat
from pathlib import Path

import pytest

from desktop_agent.control import aria_look
from desktop_agent.control.llm_vision import SYSTEM, claude_look
from tests.test_ask_aria import PORT, _service, served  # noqa: F401
from tests.test_ask_aria_reliability import _open

PNG = base64.b64encode(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")).decode()


def test_vision_call_is_read_only_subscription_only_and_runs_in_a_directory_with_just_the_image(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > argv.txt\nenv | grep -c -E "API_KEY|AUTH_TOKEN" > envcount.txt\nls > files.txt\n'
                    'echo \'{"result":"A settings page with a blue Save button.","total_cost_usd":0.004,"usage":{"input_tokens":3,"output_tokens":9}}\'\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    folder = tmp_path / "work"; folder.mkdir(); (folder / "capture.png").write_bytes(b"x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x"); monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "t")
    res = asyncio.run(claude_look(str(fake), str(folder), "capture.png", "what is this?", "claude-haiku-5-5"))
    argv = (folder / "argv.txt").read_text().split("\n")
    assert res["text"].startswith("A settings page") and res["estimate_usd"] == 0.004
    assert argv[argv.index("--tools") + 1] == "Read" and argv[argv.index("--allowedTools") + 1] == "Read"      # nothing but Read: no Bash, Edit, Write or web
    assert (folder / "envcount.txt").read_text().strip() == "0"                                                  # no API key or token reaches the vision call
    assert "capture.png" in (folder / "files.txt").read_text() and "click, type or operate" in SYSTEM


def test_look_stores_one_capture_with_provenance_and_answers_in_the_same_conversation_once(tmp_path, monkeypatch):
    s = _service(tmp_path); calls = []
    async def fake_look(claude_bin, directory, filename, question, model, timeout=150):
        calls.append((directory, filename, question)); return {"text": "I can see a 1x1 test image; I cannot read any text in it.", "model": model, "estimate_usd": 0.004, "tokens_in": 5, "tokens_out": 7, "duration_ms": 9}
    monkeypatch.setattr(aria_look, "claude_look", fake_look)
    args = dict(image_b64=PNG, mime="image/png", question="what am I looking at?", client_id="look-1", captured_at="2026-10-12T05:50:00+00:00", width=1, height=1)
    r = asyncio.run(aria_look.look(s, **args))
    assert r["kind"] == "look" and "cannot read any text" in r["reply"] and r["look_id"].startswith("lk-") and r["usage"]["billing"] == "subscription-included"
    assert Path(calls[0][0], calls[0][1]).read_bytes()[:4] == b"\x89PNG" and sorted(os.listdir(calls[0][0])) == ["capture.png"]
    rec = s.store.receipts("look", r["look_id"])[0]
    assert rec["actor"] == "human" and rec["result_label"] == "verified" and any(e.startswith("sha256=") for e in rec["evidence"]) and "captured_at=2026-10-12T05:50:00+00:00" in rec["evidence"]
    turns = s.aria.transcript()
    assert [t["role"] for t in turns] == ["owner", "aria"] and turns[0]["text"].startswith("👁 Look at my screen") and turns[1]["kind"] == "look"
    again = asyncio.run(aria_look.look(s, **args))
    assert again["duplicate"] is True and len(calls) == 1 and len(s.aria.transcript()) == 2           # a repeated delivery runs no second vision call


def test_bad_images_are_refused_and_a_vision_failure_is_an_honest_error_turn(tmp_path, monkeypatch):
    s = _service(tmp_path)
    for bad in (dict(image_b64=PNG, mime="image/gif"), dict(image_b64="!!notbase64", mime="image/png"), dict(image_b64=base64.b64encode(b"x" * (7 * 1024 * 1024)).decode(), mime="image/png")):
        with pytest.raises(ValueError):
            asyncio.run(aria_look.look(s, question="", client_id="", captured_at="", width=1, height=1, **bad))
    from desktop_agent.control.llm import LLMError
    async def boom(*a, **k): raise LLMError("vision call failed: x")
    monkeypatch.setattr(aria_look, "claude_look", boom)
    r = asyncio.run(aria_look.look(s, image_b64=PNG, mime="image/png", question="", client_id="", captured_at="", width=1, height=1))
    assert r["kind"] == "error" and "Nothing was changed on your machine" in r["reply"] and r["usage"]["rounds"] == 0


def test_no_input_injection_exists_anywhere_in_the_look_path():
    root = Path(__file__).resolve().parents[1]
    src = (root / "desktop_agent/control/aria_look.py").read_text() + (root / "desktop_agent/control/llm_vision.py").read_text()
    for banned in ("xdotool", "ydotool", "pyautogui", "SendInput", "uinput", "dispatchEvent"):
        assert banned not in src.replace('"Read"', ""), banned
    page = (root / "panel/index.html").read_text()
    look_js = page[page.index("async function lookNow"):page.index('$("look_btn").onclick')]
    assert "dispatchEvent" not in look_js and ".click(" not in look_js and "keydown" not in look_js and "getDisplayMedia" in look_js


def test_look_button_in_the_loaded_panel_shows_the_indicator_and_the_explanation(served, monkeypatch):
    s = served
    async def fake_look(claude_bin, directory, filename, question, model, timeout=150):
        return {"text": "A test pattern: coloured stripes and the word LOOK. Nothing else is visible.", "model": model, "estimate_usd": 0.004, "tokens_in": 5, "tokens_out": 7, "duration_ms": 9}
    monkeypatch.setattr(aria_look, "claude_look", fake_look)
    playwright = pytest.importorskip("playwright.sync_api")
    stub = ("navigator.mediaDevices.getDisplayMedia = async () => { const c = document.createElement('canvas'); c.width = 640; c.height = 360;"
            "const g = c.getContext('2d'); g.fillStyle = '#a33'; g.fillRect(0,0,640,360); g.fillStyle = '#fff'; g.font = '48px sans-serif'; g.fillText('LOOK', 40, 100);"
            "window.__shared = true; return c.captureStream(5); };")
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome: {exc}")
        ctx = browser.new_context(viewport={"width": 1648, "height": 900}); ctx.add_init_script(stub)
        page = ctx.new_page(); _open(page)
        page.click("#aria_bubble"); page.wait_for_timeout(300)
        assert page.inner_text("#look_state") == ""                                        # nothing is captured until the owner asks
        page.click("#look_btn")
        page.wait_for_function("document.querySelector('#aria_log').innerText.includes('Nothing else is visible')", timeout=20000)
        log = page.inner_text("#aria_log"); state = page.inner_text("#look_state")
        assert "Look at my screen" in log and "coloured stripes" in log and "included in your subscription" in log and "$0.004" not in log
        assert "Looked at" in state and "640×360" in state and "Sharing is off" in state
        # the typed phrase is an explicit request too
        page.fill("#aria_text", "look at what I'm doing"); page.click("#aria_send")
        page.wait_for_function("document.querySelectorAll('#aria_log .turn.aria').length >= 2", timeout=20000)
        assert len(list(Path(s.cfg.data_dir, "looks").iterdir())) == 2
        ctx.close(); browser.close()
