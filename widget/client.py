"""Control-plane API client for the Aria widget (stdlib only, no secrets in code).

Configuration: ~/.config/desktop-agent/widget.json
  {"url": "http://127.0.0.1:8477", "token": "...", "anthropic_api_key": "..."}
The token may also come from DESKTOP_AGENT_TOKEN or, when the widget runs as the
same user as the control plane, from ~/.config/desktop-agent/token.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

CONFIG_FILE = Path("~/.config/desktop-agent/widget.json").expanduser()
TOKEN_FILE = Path("~/.config/desktop-agent/token").expanduser()


class ApiError(RuntimeError):
    pass


class RefreshCoalescer:
    """Limit refresh scheduling while retaining one trailing burst refresh."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._next_at = 0.0
        self._pending = False
        self._lock = threading.Lock()

    def request(self, now: float) -> float | None:
        """Return seconds until the refresh, or None when one is already pending."""
        with self._lock:
            if self._pending:
                return None
            scheduled_at = max(now, self._next_at)
            self._next_at = scheduled_at + self.interval
            self._pending = True
            return scheduled_at - now

    def dispatched(self) -> None:
        with self._lock:
            self._pending = False


def class_label(state: dict | None, model_class: str | None) -> str:
    """Label for a model class from the state's `class_labels`; the raw class name when unknown or absent."""
    raw = model_class or ""
    labels = (state or {}).get("class_labels")
    label = labels.get(raw) if isinstance(labels, dict) else None
    return label if isinstance(label, str) and label else raw


def load_config() -> dict:
    cfg = {"url": "http://127.0.0.1:8477", "token": "", "anthropic_api_key": ""}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    if not cfg["token"]:
        cfg["token"] = os.environ.get("DESKTOP_AGENT_TOKEN", "")
    if not cfg["token"] and TOKEN_FILE.exists():
        try:
            cfg["token"] = TOKEN_FILE.read_text().strip()
        except OSError:
            pass
    if not cfg["anthropic_api_key"]:
        cfg["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({k: cfg.get(k, "") for k in ("url", "token", "anthropic_api_key")}, indent=2))
    os.chmod(CONFIG_FILE, 0o600)


class Client:
    def __init__(self, url: str, token: str, source: str = "widget:aria", timeout: float = 20.0):
        self.url, self.token, self.source, self.timeout = url.rstrip("/"), token, source, timeout

    def _req(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("X-Source", self.source)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise ApiError(f"{exc.code} {path}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(f"control plane unreachable at {self.url}: {exc}") from None

    def health(self) -> dict:
        return self._req("GET", "/v0/health")

    def projects(self) -> list[dict]:
        return self._req("GET", "/v0/projects")["projects"]

    def state(self) -> dict:
        return self._req("GET", "/v0/state")

    def task(self, task_id: str) -> dict:
        return self._req("GET", f"/v0/tasks/{task_id}")

    def submit_goal(self, project: str, text: str, task_type: str = "code", owned_area: list[str] | None = None,
                    idempotency_key: str | None = None, **extra) -> dict:
        body = {"project": project, "text": text, "task_type": task_type, "owned_area": owned_area or []} | extra
        return self._req("POST", "/v0/goals", body, {"Idempotency-Key": idempotency_key} if idempotency_key else None)

    def control(self, action: str, task_id: str | None = None) -> dict:
        return self._req("POST", "/v0/control", {"action": action, "task_id": task_id})

    def decide(self, decision_id: str, answer: str) -> dict:
        return self._req("POST", f"/v0/decisions/{decision_id}", {"answer": answer})

    def receipts(self, task_id: str) -> list[dict]:
        return self._req("GET", f"/v0/receipts?task_id={task_id}")["receipts"]

    def follow_events(self, on_event: Callable[[dict], None], since: int = 0, stop: threading.Event | None = None,
                      on_connection: Callable[[ApiError | None], None] | None = None) -> None:
        """Blocking SSE follower; run it in a thread. Reconnects until `stop` is set."""
        stop = stop or threading.Event()
        last = since
        while not stop.is_set():
            req = urllib.request.Request(f"{self.url}/v0/events?since={last}&access_token={self.token}")
            error = None
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if on_connection:
                        on_connection(None)
                    for raw in resp:
                        if stop.is_set():
                            return
                        line = raw.decode(errors="replace").rstrip("\n")
                        if line.startswith("data: "):
                            try:
                                ev = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            last = ev.get("seq") or last
                            on_event(ev)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                error = ApiError(f"control-plane event stream unavailable: {exc}")
            if stop.is_set():
                return
            if on_connection:
                on_connection(error or ApiError("control-plane event stream closed"))
            stop.wait(3)
