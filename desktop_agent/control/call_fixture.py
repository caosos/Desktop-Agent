"""The synthetic path of the SIMULATED calling loop: the scripted fake transport, the fixture conversation and
the one local read-only fixture tool. Nothing here touches a network, provider, credential or real destination.
Split from call_session.py so the runner stays within the 300-line limit (docs/CALLING_BRIEF.md §11)."""
from __future__ import annotations

import asyncio


def fixture_script(outcome: str = "completed", alias: str = "fixture:front-desk") -> list[dict]:
    """Two fixture turns, one allowed tool call, then the terminal event."""
    return [
        {"type": "speech.started", "turn": 1},
        {"type": "transcript.user", "turn": 1, "text": "SIMULATED caller: is the fixture office open today?"},
        {"type": "response.text", "turn": 1, "text": "SIMULATED assistant: let me check the fixture status."},
        {"type": "tool.call", "turn": 1, "tool_call_id": "tc-1", "name": "lookup_fixture_status", "args": {"alias": alias}},
        {"type": "speech.stopped", "turn": 1},
        {"type": "transcript.user", "turn": 2, "text": "SIMULATED caller: thanks, that is all."},
        {"type": "response.text", "turn": 2, "text": "SIMULATED assistant: the fixture office is open; goodbye."},
        {"type": "session.ended", "outcome": outcome.lower()},
    ]


DEFAULT_FIXTURE_SCRIPT = fixture_script()


class FakeTransport:
    """Scripted transport for the synthetic path: yields the script in order, records every send."""

    def __init__(self, script, delay_sec: float = 0.0):
        self.script = list(script)
        self.delay_sec = delay_sec
        self.sent: list[dict] = []
        self.closed = False
        self._closed_ev: asyncio.Event | None = None

    def _ev(self) -> asyncio.Event:
        if self._closed_ev is None:
            self._closed_ev = asyncio.Event()
        return self._closed_ev

    async def events(self):
        for ev in self.script:
            if self.closed:
                return
            if self.delay_sec:
                try:
                    await asyncio.wait_for(self._ev().wait(), self.delay_sec)
                    return                                          # closed while waiting
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0)                              # let tool tasks run between events
            if self.closed:
                return
            if ev.get("type") == "disconnect":
                return
            yield dict(ev)

    async def send(self, obj: dict) -> None:
        if not self.closed:
            self.sent.append(dict(obj))

    async def close(self) -> None:
        self.closed = True
        self._ev().set()


def lookup_fixture_status(alias: str) -> dict:
    return {"ok": True, "alias": alias, "status": "SIMULATED: fixture office open", "note": "SIMULATED fixture data; no real system was queried"}


