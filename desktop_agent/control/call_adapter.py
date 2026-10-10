"""Adapter that runs one SessionRunner per call over an injected transport factory (docs/CALLING_BRIEF.md §11).
Split from call_session.py to keep the runner under 300 lines."""
from __future__ import annotations

from .call_session import CallContract, SessionRunner


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
