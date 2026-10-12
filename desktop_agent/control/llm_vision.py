"""One-shot screenshot understanding (owner directive da-50b1e88f20, milestone 2). Aria is a looking platform, not an action platform:
this reads ONE image the owner chose to share and describes it. The Claude CLI runs on the subscription login (no API key, token or
gateway: `subscription_env`), in a directory that contains only that image, with only the Read tool, so it can look at the picture and
nothing else. No Bash, Edit, Write or web tool, so nothing on the machine can be clicked, typed into or changed."""
from __future__ import annotations

import asyncio
import json
import os
import time

from .llm import LLMError, subscription_env

SYSTEM = ("You are Aria, looking at ONE screenshot that the owner chose to share. Describe what is visible and answer the owner's question. "
          "Ground every statement in what you can actually see; say plainly what you cannot see, cannot read or are unsure of; never guess hidden content. "
          "You only look and explain: do not suggest that you will click, type or operate anything, and do not claim you can.")


async def claude_look(claude_bin: str, directory: str, filename: str, question: str, model: str, timeout: int = 150) -> dict:
    prompt = f"Read the image file ./{filename} (use the Read tool), then answer. Owner's question: {question or 'What am I looking at, and what is going on?'}"
    argv = [claude_bin, "-p", prompt, "--output-format", "json", "--model", model, "--tools", "Read", "--allowedTools", "Read", "--max-turns", "4",
            "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--append-system-prompt", SYSTEM]
    started = time.time()
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                env=subscription_env(os.environ) | {"DESKTOP_AGENT_LLM": "1"}, cwd=directory, limit=16 * 1024 * 1024)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise LLMError("screenshot understanding timed out")
    text = out.decode(errors="replace")
    try:
        msg = json.loads(text[text.index("{"):])
    except (ValueError, json.JSONDecodeError):
        raise LLMError(f"vision call returned no JSON: {text[:160]!r}")
    if msg.get("is_error") or not (msg.get("result") or "").strip():
        raise LLMError(f"vision call failed: {str(msg.get('result') or msg.get('subtype'))[:200]}")
    u = msg.get("usage") or {}
    return {"text": msg["result"].strip(), "model": model, "estimate_usd": msg.get("total_cost_usd"),
            "tokens_in": int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0),
            "tokens_out": int(u.get("output_tokens") or 0), "duration_ms": int((time.time() - started) * 1000)}
