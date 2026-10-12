"""Ask Aria "look at what I'm doing" (owner directive da-50b1e88f20, milestone 2): store one explicitly requested screenshot with its
provenance, have the vision backend explain it, and put both turns in the same conversation. Looking only: nothing here, and nothing
the vision call can reach, clicks, types, moves a mouse or controls an application. Idempotent by client_id (a repeat returns the
stored exchange and runs no second vision call)."""
from __future__ import annotations

import base64
import hashlib
import time
import uuid
from pathlib import Path

from .aria_replay import stored_exchange
from .aria_usage import usage_record
from .events import Actor
from .llm import LLMError
from .llm_vision import claude_look
from .receipts import VERIFIED, write_receipt

MAX_BYTES = 6 * 1024 * 1024
EXT = {"image/jpeg": "jpg", "image/png": "png"}


async def look(service, *, image_b64: str, mime: str, question: str, client_id: str, captured_at: str, width: int, height: int, source: str = "panel getDisplayMedia") -> dict:
    aria = service.aria
    if client_id:
        done = stored_exchange(aria.history, client_id)
        if done:
            return done | {"duplicate": True}
        if done is False:
            return {"reply": "This look request was received earlier but was interrupted; send a new one.", "kind": "error", "actions": [], "duplicate": True}
    if mime not in EXT:
        raise ValueError("the screenshot must be image/jpeg or image/png")
    try:
        data = base64.b64decode(image_b64, validate=True)
    except Exception:
        raise ValueError("the screenshot is not valid base64")
    if not data or len(data) > MAX_BYTES:
        raise ValueError(f"the screenshot must be 1 byte to {MAX_BYTES // 1024 // 1024} MB")
    look_id = "lk-" + uuid.uuid4().hex[:10]
    folder = Path(service.cfg.data_dir) / "looks" / look_id                      # only this image lives in the vision call's working directory
    folder.mkdir(parents=True, exist_ok=True)
    name = f"capture.{EXT[mime]}"
    (folder / name).write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    evidence = [f"look_id={look_id}", f"sha256={sha}", f"bytes={len(data)}", f"size={width}x{height}", f"captured_at={captured_at}", f"source={source}", "explicit owner request"]
    write_receipt(service.store, subject_type="look", subject_id=look_id, claim="owner explicitly asked Aria to look; one screenshot stored (looking only, no input injection)",
                  actor=Actor.HUMAN.value, source="panel", result_label=VERIFIED, evidence=evidence, after_state={"path": str(folder / name)})
    started = time.time()
    text = (question or "").strip() or "What am I looking at, and what is going on?"
    aria.history.append({"role": "owner", "text": f"👁 Look at my screen: {text}", "ts": started, **({"client_id": client_id} if client_id else {})})
    aria._persist()
    try:
        res = await claude_look(service.llm.claude_bin, str(folder), name, text, service.cfg.models["cloud_cheap"])
        reply, kind, ok = res["text"], "look", True
    except LLMError as exc:
        res, reply, kind, ok = {}, f"I could not read the screenshot: {exc}. Nothing was changed on your machine.", "error", False
    usage = usage_record(backend="claude_cli", model=res.get("model") or service.cfg.models["cloud_cheap"], reported_usd=res.get("estimate_usd"), rounds=1 if ok else 0,
                         tokens_in=res.get("tokens_in", 0), tokens_out=res.get("tokens_out", 0), duration_ms=int((time.time() - started) * 1000))
    action = {"name": "look", "ok": ok, "evidence": {"look_id": look_id, "sha256": sha[:16], "captured_at": captured_at}}
    aria.history.append({"role": "aria", "text": reply, "ts": time.time(), "kind": kind, "usage": usage, "actions": [action]})
    aria._persist()
    if res.get("estimate_usd") is not None:
        service.store.add_cost(None, None, usage["model"], res["estimate_usd"], res.get("tokens_in", 0), res.get("tokens_out", 0))     # diagnostics only
    return {"reply": reply, "kind": kind, "actions": [action], "model": usage["model"], "tokens": usage["tokens"], "duration_ms": usage["duration_ms"], "usage": usage,
            "look_id": look_id, "captured_at": captured_at, "sha256": sha}
