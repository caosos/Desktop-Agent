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

from .call_fixture import DEFAULT_FIXTURE_SCRIPT, FakeTransport, fixture_script, lookup_fixture_status  # noqa: F401  (synthetic path)

TRANSCRIPT_WAIT_SEC = 0.9
FIXTURE_PREFIX = "fixture:"


class SessionTransport(Protocol):
    def events(self) -> AsyncIterator[dict]: ...      # provider -> runner; ending without session.ended = disconnect
    async def send(self, obj: dict) -> None: ...      # runner -> provider
    async def close(self) -> None: ...


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


class SessionRunner:
    def __init__(self, call: dict, contract: CallContract, transport, apply, record, receipt=None):
        self.call, self.contract, self.transport = call, contract, transport
        self._apply, self._record, self._receipt = apply, record, receipt
        self.call_id = call["call_id"]
        self._seq = 0
        self._cancelled = False
        self._broken: str | None = None                  # a failed send / tool task: the session can no longer succeed
        self._fatal: asyncio.Event | None = None         # set when the session cannot continue: wakes the receive wait
        self.cleanup_error: str | None = None            # recorded, never allowed to replace the primary outcome
        self.report_error: str | None = None             # the terminal outcome itself could not be persisted
        self.task_errors: list[str] = []                 # exceptions retrieved from finished tool/resend tasks
        self._outputs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._turn_events: dict = {}
        self._transcripts: dict = {}
        self._turns: set = set()
        self._tools = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = None

    # ---- guarded effects: nothing leaves the runner once cancelled or broken ------------------------------
    def _step(self, state: str, detail: str, result: dict | None = None) -> bool:
        if self._cancelled:
            return False
        self._seq += 1
        applied = self._apply(self.call_id, self._seq, state, detail, result)["applied"]
        if not applied:
            self._cancelled = True                       # the service refused (cancelled / terminal): stop everything
        return applied

    def _terminal(self, state: str, detail: str, result: dict | None) -> bool:
        """Terminal reporting is best effort: if persisting the outcome itself fails, the failure is recorded on the
        runner (report_error) and nothing is claimed; the exception never escapes the runner."""
        try:
            return self._step(state, detail, result)
        except Exception as exc:
            self.report_error = f"terminal report failed: {exc!r}"
            return False

    async def _send(self, obj: dict) -> bool:
        if self._cancelled or self._broken:
            return False
        try:
            await self.transport.send(obj)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:                         # a failed delivery can never be followed by success
            self._broken = f"send failed: {exc!r}"
            self._wake()                                 # a quiet peer must not keep the session open after this
            return False

    def _wake(self) -> None:
        if self._fatal is not None and not self._fatal.is_set():
            self._fatal.set()

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
        self._wake()
        for t in self._tasks.values():
            t.cancel()
        if self._loop is not None:
            self._loop.create_task(self._close_quietly())

    async def _close_quietly(self) -> None:
        try:
            await self.transport.close()
        except Exception as exc:
            self.cleanup_error = f"close failed: {exc!r}"

    async def _settle_tasks(self, cancel_pending: bool) -> None:
        """Cancel (or let finish) and await every tool/resend task and retrieve their exceptions, so no tool or
        send effect survives the terminal outcome. Its own errors are recorded, never raised."""
        tasks = list(self._tasks.values())
        if cancel_pending:
            for t in tasks:
                if not t.done():
                    t.cancel()
        if tasks:
            for r in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(r, Exception) and repr(r) not in self.task_errors:   # the boundary normally saw it first
                    self.task_errors.append(repr(r))

    # ---- the session ---------------------------------------------------------------------------------------
    async def run(self) -> None:
        self._loop, self._thread = asyncio.get_running_loop(), threading.current_thread()
        t0 = time.monotonic()
        if not self._step("ACCEPTED", "SIMULATED worker accepted the call") or \
           not self._step("STARTED", "SIMULATED call started (no line was opened)"):
            return await self._close_quietly()
        first, ended, read_error = True, None, None
        self._fatal = asyncio.Event()
        try:
            async for ev in self._events_until_fatal():
                if self._cancelled or self._broken:
                    break
                if first:
                    first = False
                    if not self._step("IN_PROGRESS", "SIMULATED call in progress (fixture conversation)"):
                        break
                if ev.get("type") == "session.ended":
                    ended = ev
                    break
                try:
                    await self._handle(ev)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:                 # a record callback failing inside the receive loop is fatal too
                    read_error = f"event handling failed: {exc!r}"
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            read_error = f"transport read failed: {exc!r}"
        try:
            if ended is not None and not self._cancelled and not read_error and not self._broken:
                await self._settle_tasks(cancel_pending=False)    # tool results are delivered before the session closes
                await self._finish(ended, t0)
                return
            await self._settle_tasks(cancel_pending=True)         # failure / disconnect / cancel: no effect survives
            if self._cancelled:
                return
            reason = read_error or self._broken or "transport disconnected"
            self._terminal("FAILED", f"SIMULATED {reason}", self._result("failed", f"SIMULATED call failed: {reason}; no real call was placed.", t0))
        finally:
            await self._close_quietly()                           # cleanup errors are recorded, never the outcome

    async def _events_until_fatal(self):
        """The transport's events, abandoned the moment the session becomes fatal (failed send, cancel): a peer
        that never sends another event cannot keep the runner pending."""
        it = self.transport.events().__aiter__()
        fatal = asyncio.ensure_future(self._fatal.wait())
        try:
            while True:
                nxt = asyncio.ensure_future(it.__anext__())
                done, _ = await asyncio.wait({nxt, fatal}, return_when=asyncio.FIRST_COMPLETED)
                if nxt not in done:
                    nxt.cancel()
                    try:
                        await nxt
                    except (asyncio.CancelledError, StopAsyncIteration, Exception):
                        pass
                    return
                try:
                    yield nxt.result()
                except StopAsyncIteration:
                    return
        finally:
            fatal.cancel()

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
            loop = asyncio.get_running_loop()
            if tid in self._tasks:                       # replayed or duplicate id: re-send the stored output, execute nothing
                self._tasks[f"resend:{id(ev)}"] = loop.create_task(self._guarded(self._resend(tid), f"resend {tid}"))
            else:
                self._tasks[tid] = loop.create_task(self._guarded(self._tool(ev), f"tool {tid}"))

    async def _guarded(self, coro, label: str) -> None:
        """One exception boundary for a whole tool/resend task (execute, receipt, record, send): any failure makes
        the session fatal at once (reason set, receive wait woken) so a quiet peer cannot leave it pending.
        Cancellation is not a failure and passes through untouched."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"tool task failed ({label}): {exc!r}"
            self.task_errors.append(reason)
            if not self._broken:
                self._broken = reason
            self._wake()

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
        if self._cancelled or self._broken:
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
        if await self._send({"type": "tool.result", "tool_call_id": tid, "output": out}):
            await self._send({"type": "response.create"})

    def _result(self, outcome: str, summary: str, t0: float) -> dict:
        return {"outcome": f"SIMULATED: {outcome}", "summary": summary, "duration_sec": round(time.monotonic() - t0, 3),
                "turns": len(self._turns), "tools": self._tools}

    async def _finish(self, ev: dict, t0: float) -> None:
        """Terminal outcome after session.ended: COMPLETED only when every tool result was delivered and no
        tool task failed; otherwise FAILED with the first failure named. Never success after a failed delivery."""
        alias, purpose = self.call["destination_alias"], self.call["purpose"]
        failure = self._broken or (f"tool task failed: {self.task_errors[0]}" if self.task_errors else None)
        if failure:
            self._terminal("FAILED", f"SIMULATED {failure}", self._result("failed", f"SIMULATED call to {alias} failed: {failure}; no real call was placed.", t0))
            return
        ok = ev.get("outcome", "completed") == "completed"
        summary = ev.get("summary") or (f"SIMULATED call to {alias} about '{purpose}' completed with a fixture answer; no real call was placed." if ok
                                         else f"SIMULATED call to {alias} failed (fixture failure); no real call was placed.")
        if "SIMULATED" not in summary:
            summary = "SIMULATED: " + summary
        result = self._result("completed" if ok else "failed", summary, t0)
        if self._terminal("COMPLETED" if ok else "FAILED", "SIMULATED call completed" if ok else "SIMULATED call failed", result):
            try:
                await self.transport.send({"type": "session.close"})
            except Exception as exc:
                self.cleanup_error = f"close message failed: {exc!r}"
