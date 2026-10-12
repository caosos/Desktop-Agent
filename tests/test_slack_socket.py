"""Socket Mode transport with a fake socket and fake Slack API: no network, no real token."""
import asyncio
import json
from pathlib import Path

from desktop_agent.control.slack_ingress import SlackIngress
from desktop_agent.control.slack_socket import SlackSocket, token_status
from desktop_agent.control.store import Store

CH, ME = "C0C81K9QNGK", "U0C82H6G9NZ"
APP, BOT = "xapp-1-SECRETAPPVALUE", "xoxb-SECRETBOTVALUE"


class FakeWS:
    def __init__(self, frames): self.frames, self.sent = frames, []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def send(self, s): self.sent.append(json.loads(s))
    def __aiter__(self): return self._it()
    async def _it(self):
        for f in self.frames: yield json.dumps(f)


class FakeHTTP:
    def __init__(self, calls): self.calls = calls
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers, json):
        self.calls.append((url.rsplit("/", 1)[1], headers["Authorization"], json))
        class R:
            def json(s): return {"ok": True, "url": "wss://fake"}
        return R()


def env(tmp_path, app=APP, bot=BOT):
    p = tmp_path / "slack.env"; p.write_text(f"SLACK_APP_TOKEN={app}\nSLACK_BOT_TOKEN={bot}\n"); p.chmod(0o600); return p


def rig(tmp_path, frames):
    store, calls, mirrored, sent = Store(tmp_path / "s"), [], [], []
    ws = FakeWS(frames)
    class Intake:
        async def send_direction(self, project, text, source): sent.append(project); return {"item_id": "da-x"}
    sock = SlackSocket(store, None, env(tmp_path), connect=lambda url: ws, http_factory=lambda: FakeHTTP(calls))
    async def mirror(t): mirrored.append(t)
    sock.ingress = SlackIngress(store, Intake(), sock.post, CH, ME, mirror=mirror)
    return store, sock, ws, calls, mirrored, sent


def msg(eid, ts, text, **kw):
    return {"type": "events_api", "envelope_id": eid, "payload": {"event": {"type": "message", "channel": CH, "user": ME, "text": text, "ts": ts, **kw}}}


def test_one_ack_one_transport_receipt_and_envelopes_acknowledged_first(tmp_path):
    frames = [{"type": "hello"}, msg("e1", "10.1", "DESKTOP AGENT: TEST"), msg("e2", "10.1", "DESKTOP AGENT: TEST"),     # Slack redelivery of the same event
              msg("e3", "10.2", "ack text", bot_id="B1"), {"type": "disconnect", "envelope_id": "e9"}]
    store, sock, ws, calls, mirrored, sent = rig(tmp_path, frames)
    asyncio.run(sock._session(APP))
    assert [x["envelope_id"] for x in ws.sent] == ["e1", "e2", "e3", "e9"]                           # every envelope acknowledged on the socket
    posts = [c for c in calls if c[0] == "chat.postMessage"]
    assert len(posts) == 1 and posts[0][1] == "Bearer " + BOT and posts[0][2]["channel"] == CH and posts[0][2]["thread_ts"] == "10.1" and posts[0][2]["text"].startswith("DOT ARYA ACK")
    assert len(mirrored) == 1 and mirrored[0].startswith("DESKTOP AGENT RECEIPT: ACK") and "da-9e6b71d929" in mirrored[0] and sent == ["desktop_agent"]
    assert len(store.receipts("slack", "10.1")) == 1


def test_a_missing_or_malformed_token_file_is_need_access_and_never_leaks_a_value(tmp_path):
    store = Store(tmp_path / "s"); p = tmp_path / "none.env"
    sock = SlackSocket(store, None, p)
    async def go():
        t = asyncio.create_task(sock.run(idle=0.05)); await asyncio.sleep(0.2); t.cancel()
        try: await t
        except asyncio.CancelledError: pass
    asyncio.run(go())
    needs = [e for e in store.events() if e.payload.get("action") == "slack_need_access"]
    assert len(needs) == 1 and "SLACK_APP_TOKEN" in needs[0].payload["need"]
    st = token_status(env(tmp_path, app="bad", bot=BOT))
    assert st == {"file": str(tmp_path / "slack.env"), "mode": "0o600", "app_token_ok": False, "bot_token_ok": True} and APP not in json.dumps(st) and BOT not in json.dumps(st)


def test_a_connection_error_is_recorded_without_the_token_and_the_loop_retries(tmp_path):
    store = Store(tmp_path / "s")
    def boom(url): raise RuntimeError(f"cannot connect with {APP}")
    sock = SlackSocket(store, None, env(tmp_path), connect=boom, http_factory=lambda: FakeHTTP([]))
    async def go():
        t = asyncio.create_task(sock.run(backoff=0.05)); await asyncio.sleep(0.3); t.cancel()
        try: await t
        except asyncio.CancelledError: pass
    asyncio.run(go())
    errs = [e.payload["error"] for e in store.events() if e.payload.get("action") == "slack_disconnected"]
    assert len(errs) >= 2 and all(APP not in e for e in errs)
