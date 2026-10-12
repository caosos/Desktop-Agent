"""Socket Mode transport for the Slack ingress (DA-SLACK-INGRESS-001, da-9e6b71d929). It only moves events: every decision is in
slack_ingress.py. Tokens come from ~/.config/desktop-agent/slack.env (0600), live only in this process's memory and are never logged,
echoed or put in an event/receipt. Slack events are acknowledged on the socket first (Slack retries after 3 s); the one-ACK-per-ts
guard in the ingress makes retries and reconnects harmless. Missing tokens produce one NEED_ACCESS event and a quiet re-check."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import websockets

from .events import Actor, Event, EventType as ET, Provenance

API = "https://slack.com/api/"


def read_env(path: Path) -> dict:
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def token_status(path: Path) -> dict:
    """Presence, prefix and file mode only, never a value."""
    env = read_env(path)
    mode = oct(Path(path).stat().st_mode & 0o777) if Path(path).exists() else None
    return {"file": str(path), "mode": mode, "app_token_ok": env.get("SLACK_APP_TOKEN", "").startswith("xapp-"), "bot_token_ok": env.get("SLACK_BOT_TOKEN", "").startswith("xoxb-")}


class SlackSocket:
    def __init__(self, store, ingress, env_path: Path, connect=websockets.connect, http_factory=lambda: httpx.AsyncClient(timeout=20)):
        self.store, self.ingress, self.env_path, self.connect, self.http_factory = store, ingress, Path(env_path), connect, http_factory
        self.connected = False

    def _emit(self, action: str, **extra) -> None:
        self.store.append_event(Event(type=ET.CONTROL.value, task_id=None, payload={"action": action, **extra},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source="slack_socket", evidence=[action])))

    async def _call(self, method: str, token: str, body: dict | None = None) -> dict:
        async with self.http_factory() as c:
            r = await c.post(API + method, headers={"Authorization": f"Bearer {token}"}, json=body or {})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack {method}: {data.get('error', 'error')}")
        return data

    async def post(self, channel: str, thread_ts: str, text: str) -> None:
        await self._call("chat.postMessage", read_env(self.env_path)["SLACK_BOT_TOKEN"], {"channel": channel, "thread_ts": thread_ts, "text": text})

    async def _session(self, app_token: str) -> None:
        url = (await self._call("apps.connections.open", app_token))["url"]
        async with self.connect(url) as ws:
            self.connected = True; self._emit("slack_connected")
            async for raw in ws:
                env = json.loads(raw)
                if env.get("envelope_id"):
                    await ws.send(json.dumps({"envelope_id": env["envelope_id"]}))              # acknowledge first
                if env.get("type") == "disconnect":
                    break
                if env.get("type") == "events_api":
                    try:
                        await self.ingress.handle((env.get("payload") or {}).get("event") or {})
                    except Exception as exc:                                                     # one bad event never drops the socket
                        self._emit("slack_event_error", error=str(exc)[:160])
        self.connected = False

    async def run(self, backoff: float = 5.0, idle: float = 60.0) -> None:
        warned = False
        while True:
            tok = read_env(self.env_path).get("SLACK_APP_TOKEN", "")
            if not (tok.startswith("xapp-") and read_env(self.env_path).get("SLACK_BOT_TOKEN", "").startswith("xoxb-")):
                if not warned:
                    self._emit("slack_need_access", need="SLACK_APP_TOKEN and SLACK_BOT_TOKEN in " + str(self.env_path)); warned = True
                await asyncio.sleep(idle); continue
            warned = False
            try:
                await self._session(tok)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False; self._emit("slack_disconnected", error=str(exc)[:160].replace(tok, "***"))
            await asyncio.sleep(backoff)
