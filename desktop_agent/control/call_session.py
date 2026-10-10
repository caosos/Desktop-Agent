"""SIMULATED conversation-control session runner behind CallService (docs/CALLING_BRIEF.md section 11).

A provider-injectable transport yields conversation events; the runner pumps them without blocking, runs
contract-bounded local tools, sends receipted results back and finishes the call only from `session.ended`.
Only the fake transport exists: no network, provider, credential, telephony or real destination.
Every synthetic state, tool output, result and receipt claim carries the word SIMULATED.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import AsyncIterator, Protocol

TRANSCRIPT_WAIT_SEC = 0.9
FIXTURE_PREFIX = "fixture:"


class SessionTransport(Protocol):
    def events(self) -> AsyncIterator[dict]: ...      # provider -> runner; ending without session.ended = disconnect
    async def send(self, obj: dict) -> None: ...      # runner -> provider
    async def close(self) -> None: ...


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


class CallContract:
    """What a session may ask the runner to do: only the listed local read-only tools, validated by name and args."""

    def __init__(self, call_id: str, allowed_tools=("lookup_fixture_status",)):
        self.call_id = call_id
        self.allowed_tools = tuple(allowed_tools)

    def validate(self, name, args) -> str | None:
        """None when the call is inside the contract, else the refusal reason."""
        if name not in self.allowed_tools or name != "lookup_fixture_status":
            return "refused: outside the call contract"
        alias = args.get("alias") if isinstance(args, dict) else None
        if not isinstance(args, dict) or set(args) != {"alias"} or not isinstance(alias, str) or not alias.startswith(FIXTURE_PREFIX):
            return "refused: invalid arguments (alias must be a fixture identity)"
        return None


def _sim(text: str) -> str:
    return text if "SIMULATED" in text else f"SIMULATED {text}"


def lookup_fixture_status(alias: str) -> dict:
    return {"ok": True, "alias": alias, "status": "SIMULATED: fixture office open", "note": "SIMULATED fixture data; no real system was queried"}


class SessionRunner:
    def __init__(self, call: dict, contract: CallContract, transport, apply, record, receipt=None):
        self.call, self.contract, self.transport = call, contract, transport
        self._apply, self._record, self._receipt = apply, record, receipt
        self.call_id = call["call_id"]
        self._seq = 0
        self._cancelled = False
        self._outputs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._turn_events: dict = {}
        self._transcripts: dict = {}
        self._turns: set = set()
        self._tools = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = None

    # ---- guarded effects: nothing leaves the runner once cancelled ---------------------------------------
    def _step(self, state: str, detail: str, result: dict | None = None) -> bool:
        if self._cancelled:
            return False
        self._seq += 1
        applied = self._apply(self.call_id, self._seq, state, detail, result)["applied"]
        if not applied:
            self._cancelled = True                       # the service refused (cancelled / terminal): stop everything
        return applied

    async def _send(self, obj: dict) -> None:
        if not self._cancelled:
            await self.transport.send(obj)

    def _rec(self, entry: dict) -> None:
        if not self._cancelled:
            self._record(self.call_id, entry)

    def cancel(self) -> None:
        self._cancelled = True
        if self._loop is not None and self._thread is not threading.current_thread():
            self._loop.call_soon_threadsafe(self._cancel_now)
        else:
            self._cancel_now()

    def _cancel_now(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        if self._loop is not None:
            self._loop.create_task(self.transport.close())

    # ---- the session ---------------------------------------------------------------------------------------
    async def run(self) -> None:
        self._loop, self._thread = asyncio.get_running_loop(), threading.current_thread()
        t0 = time.monotonic()
        if not self._step("ACCEPTED", "SIMULATED worker accepted the call"):
            return await self.transport.close()
        if not self._step("STARTED", "SIMULATED call started (no line was opened)"):
            return await self.transport.close()
        first = True
        ended = False
        try:
            async for ev in self.transport.events():
                if self._cancelled:
                    break
                if first:
                    first = False
                    if not self._step("IN_PROGRESS", "SIMULATED call in progress (fixture conversation)"):
                        break
                if ev.get("type") == "session.ended":
                    ended = True
                    await self._finish(ev, t0)
                    break
                await self._handle(ev)
            if not ended and not self._cancelled:
                self._step("FAILED", "SIMULATED transport disconnected", self._result("failed", "SIMULATED call failed: transport disconnected; no real call was placed.", t0))
        finally:
            await self.transport.close()

    async def _handle(self, ev: dict) -> None:
        kind, turn = ev.get("type"), ev.get("turn")
        if turn is not None:
            self._turns.add(turn)
        if kind == "transcript.user":
            self._transcripts[turn] = ev.get("text")
            self._turn_event(turn).set()
            self._rec({"turn": turn, "role": "user", "text": _sim(ev.get("text", ""))})
        elif kind == "response.text":
            self._rec({"turn": turn, "role": "assistant", "text": _sim(ev.get("text", ""))})
        elif kind == "response.cancelled":
            self._rec({"turn": turn, "event": "response.cancelled", "note": "SIMULATED interrupted speech"})
        elif kind == "tool.call":
            tid = ev.get("tool_call_id")
            if tid in self._tasks:                       # replayed or duplicate id: re-send the stored output, execute nothing
                self._tasks[f"resend:{id(ev)}"] = asyncio.get_running_loop().create_task(self._resend(tid))
            else:
                self._tasks[tid] = asyncio.get_running_loop().create_task(self._tool(ev))

    def _turn_event(self, turn) -> asyncio.Event:
        return self._turn_events.setdefault(turn, asyncio.Event())

    async def _resend(self, tid) -> None:
        try:
            await asyncio.shield(self._tasks[tid])
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if tid in self._outputs:
            await self._send({"type": "tool.result", "tool_call_id": tid, "output": self._outputs[tid]})

    async def _tool(self, ev: dict) -> None:
        turn, tid, name = ev.get("turn"), ev.get("tool_call_id"), ev.get("name")
        if turn not in self._transcripts:                # the receive loop is not blocked: only this handler waits
            try:
                await asyncio.wait_for(self._turn_event(turn).wait(), TRANSCRIPT_WAIT_SEC)
            except asyncio.TimeoutError:
                pass
        if self._cancelled:
            return
        reason = self.contract.validate(name, ev.get("args"))
        if reason:
            out = {"ok": False, "error": f"SIMULATED {reason}"}
            claim = f"SIMULATED tool call {name!r} {reason}"
        else:
            out = lookup_fixture_status(ev["args"]["alias"]) | {"turn": turn, "transcript": self._transcripts.get(turn)}
            claim = f"SIMULATED tool {name} executed for turn {turn}"
            self._tools += 1
        self._outputs[tid] = out
        if self._receipt:
            self._receipt(self.call_id, claim, [f"call_id={self.call_id}", f"turn={turn}", f"tool_call_id={tid}", f"name={name}"])
        self._rec({"turn": turn, "tool_call_id": tid, "name": name, "output": out})
        await self._send({"type": "tool.result", "tool_call_id": tid, "output": out})
        await self._send({"type": "response.create"})

    def _result(self, outcome: str, summary: str, t0: float) -> dict:
        return {"outcome": f"SIMULATED: {outcome}", "summary": summary, "duration_sec": round(time.monotonic() - t0, 3),
                "turns": len(self._turns), "tools": self._tools}

    async def _finish(self, ev: dict, t0: float) -> None:
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:                                      # tool results are sent before the session is closed
            await asyncio.gather(*pending, return_exceptions=True)
        if self._cancelled:
            return
        alias, purpose = self.call["destination_alias"], self.call["purpose"]
        ok = ev.get("outcome", "completed") == "completed"
        summary = ev.get("summary") or (f"SIMULATED call to {alias} about '{purpose}' completed with a fixture answer; no real call was placed." if ok
                                         else f"SIMULATED call to {alias} failed (fixture failure); no real call was placed.")
        if "SIMULATED" not in summary:
            summary = "SIMULATED: " + summary
        result = self._result("completed" if ok else "failed", summary, t0)
        if self._step("COMPLETED" if ok else "FAILED", "SIMULATED call completed" if ok else "SIMULATED call failed", result):
            await self._send({"type": "session.close"})


class SessionAdapter:
    """Builds one transport and one runner per call; keeps the runner so a cancel can reach it."""
    name = "simulated"

    def __init__(self, transport_factory):
        self.transport_factory = transport_factory
        self.runners: dict[str, SessionRunner] = {}

    async def run(self, call: dict, apply) -> None:
        svc = getattr(apply, "__self__", None)
        record = getattr(svc, "record", None) or (lambda call_id, entry: None)
        runner = SessionRunner(call, CallContract(call["call_id"]), self.transport_factory(call), apply, record, getattr(svc, "tool_receipt", None))
        self.runners[call["call_id"]] = runner
        try:
            await runner.run()
        finally:
            self.runners.pop(call["call_id"], None)

    def cancel(self, call_id: str) -> None:
        runner = self.runners.get(call_id)
        if runner:
            runner.cancel()
