"""SIMULATED conversational calling: one calling task owns one call, driven by a provider-free adapter.

Nothing here dials, uses a network, a provider, a credential or a real destination. Destinations are
fixture labels (`fixture:<label>`); every adapter-produced state, result and note carries the word
SIMULATED. State lives in kv `call:<id>` and moves only through `CallService.apply`
(compare-and-set against TRANSITIONS and the highest applied seq). The receipt chain on subject
("call", call_id) is the trace. Design: docs/CALLING_BRIEF.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid

from . import receipts as rc

FIXTURE_PREFIX = "fixture:"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
TRANSITIONS = {
    "REQUESTED": {"ACCEPTED", "CANCELLED", "FAILED"},
    "ACCEPTED": {"STARTED", "CANCELLED", "FAILED"},
    "STARTED": {"IN_PROGRESS", "CANCELLED", "FAILED"},
    "IN_PROGRESS": {"COMPLETED", "CANCELLED", "FAILED"},
    "COMPLETED": set(), "FAILED": set(), "CANCELLED": set(),
}
INDEX_KV = "calls:index"


class SimulatedAdapter:
    """The only adapter the code can construct: walks a call through its states with no I/O."""
    name = "simulated"

    def __init__(self, delay_sec: float = 0.0, outcome: str = "COMPLETED"):
        self.delay_sec = delay_sec
        self.outcome = outcome

    async def run(self, call: dict, apply) -> None:
        steps = [("ACCEPTED", "SIMULATED worker accepted the call"),
                 ("STARTED", "SIMULATED call started (no line was opened)"),
                 ("IN_PROGRESS", "SIMULATED call in progress (fixture conversation)")]
        for seq, (state, detail) in enumerate(steps, 1):
            await asyncio.sleep(self.delay_sec)
            if not apply(call["call_id"], seq, state, detail)["applied"]:
                return
        await asyncio.sleep(self.delay_sec)
        alias = call["destination_alias"]
        if self.outcome == "COMPLETED":
            result = {"outcome": "SIMULATED: completed", "duration_sec": round(self.delay_sec * 4, 3),
                      "summary": f"SIMULATED call to {alias} about '{call['purpose']}' completed with a fixture answer; no real call was placed."}
            apply(call["call_id"], 4, "COMPLETED", "SIMULATED call completed", result)
        else:
            result = {"outcome": "SIMULATED: failed", "duration_sec": round(self.delay_sec * 4, 3),
                      "summary": f"SIMULATED call to {alias} failed (fixture failure); no real call was placed."}
            apply(call["call_id"], 4, "FAILED", "SIMULATED call failed", result)


class CallService:
    def __init__(self, store, aria, adapter: SimulatedAdapter | None = None):
        self.store = store
        self.aria = aria
        self.adapter = adapter or SimulatedAdapter()
        self.adapter_starts = 0
        self.tasks: dict[str, asyncio.Task] = {}
        self._lock = threading.RLock()

    # ---- helpers ----------------------------------------------------------
    def _key(self, call_id: str) -> str:
        return f"call:{call_id}"

    def _receipt(self, call: dict, claim: str, label: str, actor: str, source: str, before=None, after=None) -> dict:
        ids = [f"call_id={call['call_id']}", f"task_id={call['task_id']}", f"command_id={call['command_id']}"]
        return rc.write_receipt(
            self.store, subject_type="call", subject_id=call["call_id"], claim=claim, actor=actor, source=source,
            result_label=label, evidence=ids + [f"state={call['state']}"], before_state=before, after_state=after,
            correlation_id=call["command_id"],
            extra={"call_id": call["call_id"], "calling_task_id": call["task_id"], "command_id": call["command_id"]})

    def _view(self, call: dict, duplicate: bool = False) -> dict:
        out = {k: call[k] for k in ("task_id", "call_id", "command_id", "conversation_ref", "destination_alias", "purpose", "state",
                                    "simulated", "label", "adapter", "history", "result", "created_at")}
        out["duplicate"] = duplicate
        out["receipts"] = len(rc.chain(self.store, "call", call["call_id"]))
        return out

    def _note(self, call: dict, text: str) -> None:
        self.aria.note(text, "call (SIMULATED)", {"call_id": call["call_id"], "task_id": call["task_id"], "state": call["state"]})

    # ---- commands ---------------------------------------------------------
    def request(self, command_id: str | None, destination_alias: str, purpose: str, conversation_ref=None, source: str = "test") -> dict:
        alias = str(destination_alias or "")
        if not alias.startswith(FIXTURE_PREFIX) or len(alias) == len(FIXTURE_PREFIX):
            raise ValueError("destination_alias must be a fixture identity starting with 'fixture:' (calls are SIMULATED)")
        purpose = str(purpose or "")[:200]
        cid = command_id or "cmd-" + hashlib.sha1(f"{conversation_ref}|{alias}|{purpose}".encode()).hexdigest()[:12]
        with self._lock:
            seen = self.store.idempotent("call:" + cid)
            if seen:
                return self._view(self.store.get_kv(self._key(seen["call_id"])), duplicate=True)
            call = {"task_id": "calltask-" + uuid.uuid4().hex[:10], "call_id": "call-" + uuid.uuid4().hex[:10], "command_id": cid,
                    "conversation_ref": conversation_ref, "destination_alias": alias, "purpose": purpose, "state": "REQUESTED",
                    "simulated": True, "label": "SIMULATED", "adapter": self.adapter.name, "history": [], "result": None,
                    "last_seq": 0, "created_at": time.time(), "source": source}
            self.store.set_kv(self._key(call["call_id"]), call)
            self.store.remember("call:" + cid, {"call_id": call["call_id"]})
            index = (self.store.get_kv(INDEX_KV) or {}).get("ids", [])
            self.store.set_kv(INDEX_KV, {"ids": index + [call["call_id"]]})
            self._receipt(call, f"SIMULATED call command accepted for {alias}; no real call will be placed", rc.VERIFIED, "control", source,
                          after={"state": "REQUESTED"})
        self._start(call)
        return self._view(call)

    def _start(self, call: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                                     # no loop: the caller drives apply() itself
        self.adapter_starts += 1
        self.tasks[call["call_id"]] = loop.create_task(self.adapter.run(dict(call), self.apply))

    async def wait(self, call_id: str) -> None:
        task = self.tasks.get(call_id)
        if task:
            await task

    def cancel(self, call_id: str, source: str = "test") -> dict:
        with self._lock:
            call = self.store.get_kv(self._key(call_id))
            if not call:
                raise KeyError(call_id)
            if call["state"] in TERMINAL:
                return self._view(call) | {"cancelled": False, "reason": f"already {call['state']}"}
            before = call["state"]
            call["state"] = "CANCELLED"
            call["history"].append({"seq": None, "state": "CANCELLED", "at": time.time(), "source": source, "detail": "SIMULATED call cancelled by the owner side"})
            self.store.set_kv(self._key(call_id), call)
            self._receipt(call, "SIMULATED call cancelled by command", rc.VERIFIED, "control", source, before={"state": before}, after={"state": "CANCELLED"})
        self._note(call, f"SIMULATED call {call_id} to {call['destination_alias']} was cancelled; any later result is ignored.")
        return self._view(call) | {"cancelled": True}

    def get(self, call_id: str) -> dict | None:
        call = self.store.get_kv(self._key(call_id))
        return self._view(call) if call else None

    def list(self) -> list[dict]:
        return [v for v in (self.get(i) for i in (self.store.get_kv(INDEX_KV) or {}).get("ids", [])) if v]

    # ---- the only way state moves -----------------------------------------
    def apply(self, call_id: str, seq: int, state: str, detail: str | None = None, result: dict | None = None) -> dict:
        with self._lock:
            call = self.store.get_kv(self._key(call_id))
            if not call:
                return {"applied": False, "reason": "no such call", "state": None}
            if seq <= call["last_seq"]:
                return {"applied": False, "reason": "replayed seq ignored", "state": call["state"]}
            if call["state"] in TERMINAL:
                self._receipt(call, f"SIMULATED late result ignored: {state} (seq {seq}) arrived after {call['state']}", rc.SIMULATED, "calling_worker",
                              "simulated", before={"state": call["state"]}, after={"state": call["state"]})
                return {"applied": False, "reason": "late result ignored", "state": call["state"]}
            if state not in TRANSITIONS.get(call["state"], set()):
                self._receipt(call, f"SIMULATED out-of-order event rejected: {call['state']} -> {state} (seq {seq})", rc.SIMULATED, "calling_worker",
                              "simulated", before={"state": call["state"]}, after={"state": call["state"]})
                return {"applied": False, "reason": f"transition {call['state']} -> {state} not allowed", "state": call["state"]}
            before = call["state"]
            call.update(state=state, last_seq=seq)
            call["history"].append({"seq": seq, "state": state, "at": time.time(), "source": "simulated", "detail": detail or f"SIMULATED {state}"})
            if result is not None:
                call["result"] = result
            self.store.set_kv(self._key(call_id), call)
            self._receipt(call, f"SIMULATED call {before} -> {state}: {detail or ''}".strip(), rc.SIMULATED, "calling_worker", "simulated",
                          before={"state": before}, after={"state": state})
        if state == "STARTED":
            self._note(call, f"SIMULATED call {call_id} to {call['destination_alias']} started ({call['purpose']}).")
        elif state in ("COMPLETED", "FAILED"):
            self._note(call, f"SIMULATED call {call_id} {state.lower()}: {(call['result'] or {}).get('summary', detail)}")
        return {"applied": True, "reason": None, "state": state}
