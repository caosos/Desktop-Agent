"""Push-to-talk: record with arecord while the button is held, transcribe
with OpenAI's transcription API (the path the team already uses). Text first,
voice second (IMPLEMENTATION_PLAN §2); this module is optional."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

_rec: subprocess.Popen | None = None
_path: Path | None = None


def available(cfg: dict) -> bool:
    return bool(_openai_key(cfg)) and shutil.which("arecord") is not None


def unavailable_reason(cfg: dict) -> str:
    if not shutil.which("arecord"):
        return "voice: arecord (alsa-utils) is not installed"
    if not _openai_key(cfg):
        return "voice: add openai_api_key to ~/.config/desktop-agent/widget.json"
    return "voice unavailable"


def _openai_key(cfg: dict) -> str:
    return cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")


def start_recording() -> None:
    global _rec, _path
    if _rec is not None:
        return
    _path = Path(tempfile.gettempdir()) / f"aria-{uuid.uuid4().hex}.wav"
    _rec = subprocess.Popen(["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav", str(_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_and_transcribe(cfg: dict) -> str:
    global _rec, _path
    if _rec is None or _path is None:
        return ""
    _rec.terminate()
    try:
        _rec.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _rec.kill()
    _rec = None
    path, _path = _path, None
    try:
        if not path.exists() or path.stat().st_size < 2000:
            return ""
        return _transcribe(path, _openai_key(cfg))
    finally:
        path.unlink(missing_ok=True)


def _transcribe(path: Path, key: str, model: str = "gpt-4o-mini-transcribe") -> str:
    boundary = "----aria" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode()
    body += path.read_bytes() + b"\r\n" + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return (json.loads(r.read().decode()).get("text") or "").strip()
    except Exception:
        return ""
