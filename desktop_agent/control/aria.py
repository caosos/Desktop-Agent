"""Aria's conversational layer, server-side: one cheap structured model call per
step, at most a few steps, with actions limited to the control plane's own
operations. The widget and panel just send text and show the reply.

Every action Aria takes is a normal control-plane operation with its own
receipt (source "aria"). Aria never edits files or runs commands.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from .llm import LLM, LLMError
from .aria_replay import interrupted_result, stored_exchange
from .aria_state import compact_state  # noqa: F401  (re-exported: tests and run_action import it from here)
from .aria_usage import status_cost_note, usage_record

PROMPT_FILE = Path(__file__).resolve().parents[2] / "widget" / "aria_prompt.md"
MAX_STEPS = 4
HISTORY_TURNS = 12

ACTIONS = {
    "get_state": "current workers, tasks with stages, blocked items, open owner decisions, costs, feedback",
    "list_projects": "projects the control plane manages",
    "project_brief": "one project's drilldown: mission, coordinator vs workers, agent roster (declared vs actual now, with proof), tasks, instructions with status/age, freshness, primary action: args {project}",
    "explain_task": "details of one task: args {task_id}",
    "submit_goal": "NEW WORK: create bounded work from a confirmed instruction: args {text, project (optional)}",
    "send_direction": "DIRECTION: message a project's existing coordinator through the Shared Inbox (posted to GitHub, delivered, tracked POSTED→DELIVERED→ACK→DONE); never completes work by itself: args {project, text}",
    "answer_decision": "OWNER DECISION: record the owner's answer to an open decision: args {decision_id, answer}",
    "control": "pause, resume or stop this control plane's scheduler: args {action, task_id (optional)}",
    "request_call": "SIMULATED CALL: start a simulated conversational call to a fixture identity; no real call is ever placed: args {destination_alias (must start with 'fixture:'), purpose}",
    "call_status": "state, history and SIMULATED result of one simulated call: args {call_id}",
    "request_research": "NEW WORK (research): one bounded worker writes a cited report; args {question, project, web} — web=false: local repository sources only; web=true: also WebSearch/WebFetch with URL citations and access dates ($3 ceiling)",
    "research_status": "state, history and result of one research job: args {job_id}",
}
KIND_BY_ACTION = {"request_research": "research", "request_call": "call (SIMULATED)", "submit_goal": "new work", "send_direction": "direction", "answer_decision": "owner decision", "control": "control"}
HISTORY_KV = "aria:history"
ACTION_RESULT_CHARS = 10000  # an action result is folded into the model context up to this size


def _unwrap(data: dict) -> tuple[str, dict | None]:
    """The model occasionally returns its whole JSON answer as the `reply` string (seen live 2026-10-11 with the
    CLI's multi-turn completion). One level is unwrapped so the owner never reads raw JSON."""
    reply = str(data.get("reply") or "").strip()
    action = data.get("action")
    if reply.startswith("{") and '"reply"' in reply:
        try:
            inner = json.loads(reply)
        except ValueError:
            return reply, action
        if isinstance(inner, dict) and isinstance(inner.get("reply"), str):
            reply = inner["reply"].strip()
            action = action or inner.get("action")
    return reply, action


HISTORY_KEEP = 60

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "action": {"type": ["object", "null"], "properties": {
            "name": {"type": "string", "enum": list(ACTIONS)},
            "args": {"type": "object"}}, "required": ["name"]},
    },
    "required": ["reply", "action"],
}


class AriaBrain:
    def __init__(self, service, llm: LLM | None = None):
        self._inflight: dict[str, asyncio.Future] = {}              # client_id → the one running exchange
        self.service = service
        self.llm = llm or service.llm
        self.history: list[dict] = list((service.store.get_kv(HISTORY_KV) or {}).get("turns") or [])   # recoverable: {"role", "text", "ts"}
        self.system = (PROMPT_FILE.read_text() if PROMPT_FILE.exists() else "You are Aria.") + \
            "\n\nYou answer with a JSON object: {\"reply\": <one or two sentences for Michael>, \"action\": null | {\"name\", \"args\"}}. " \
            "Available actions:\n" + "\n".join(f"- {k}: {v}" for k, v in ACTIONS.items()) + \
            "\nUse at most one action per step; after its result you will be asked again. Ask for confirmation before submit_goal, send_direction, answer_decision and control unless the owner already said yes/go/do it. " \
            "Classify the owner's message: a QUESTION is answered read-only from actions like get_state, project_brief, explain_task; a DIRECTION goes to the project's coordinator via send_direction; NEW WORK via submit_goal; an OWNER DECISION via answer_decision. " \
            "Ground every status claim in an action result and name its source (host process, project feed, roster table, receipt, GitHub item); if the data does not say, answer 'unknown' rather than guessing. " \
            "A submitted direction or goal is not finished work: say what will happen next and how the owner will see the result. You have no web access and no knowledge beyond these actions. " \
            "A request for a report, investigation or comparison is research: state the plan (four passes in one bounded worker, model class cloud_strong; local repository sources only at a $1.5 ceiling, or, when the owner asks for public/online/market sources or the question cannot be answered from the repository, web=true: WebSearch/WebFetch with URL citations and access dates at a $3 ceiling) and ask yes/no before request_research, naming which source mode you will use; its report is the worker's and unverified beyond resolving citations. " \
            "Calls are SIMULATED: no real call is ever placed, destinations are fixture labels ('fixture:…'), and a call result is a SIMULATED result."

    async def run_action(self, name: str, args: dict, turn: str | None = None) -> dict:
        """`turn` is the owner turn id the action belongs to (request-local: concurrent chats never share it)."""
        s = self.service
        try:
            if name == "get_state":
                return compact_state(s.state())
            if name == "list_projects":
                return {"projects": [p.summary() for p in s.projects.values()]}
            if name == "project_brief":
                d = s.drill.build(str(args.get("project", "")))
                if not d:
                    return {"error": f"no such project; known: {list(s.projects)}"}
                return {k: d[k] for k in ("name", "mission", "coordinator", "workers", "roster", "tasks", "instructions", "unacknowledged", "freshness", "primary_action")}
            if name == "send_direction":
                return await s.intake.send_direction(str(args.get("project", "")), str(args.get("text", "")), "aria")
            if name == "explain_task":
                t = await s.task_detail(str(args.get("task_id", "")))
                if not t:
                    return {"error": "no such task"}
                return {k: t.get(k) for k in ("task_id", "status", "stage", "objective", "last_activity", "result", "files")} | \
                       {"receipts": [{"actor": r["actor"], "label": r["result_label"], "claim": r["claim"][:200]} for r in t.get("receipts", [])],
                        "events": [e["text"] for e in t.get("events", [])[-15:]]}
            if name == "submit_goal":
                return await s.plan_goal(text=str(args.get("text", "")), source="aria", project_hint=args.get("project"))
            if name == "answer_decision":
                return await s.answer_decision(str(args.get("decision_id", "")), str(args.get("answer", "")), "aria")
            if name == "request_call":
                return s.calling.request(None, str(args.get("destination_alias", "")), str(args.get("purpose", "")), turn, "aria")
            if name == "call_status":
                return s.calling.get(str(args.get("call_id", ""))) or {"error": "no such call"}
            if name == "request_research":
                return s.research.request(turn or "turn-" + uuid.uuid4().hex[:12], str(args.get("question", "")), str(args.get("project", "")), "aria",
                                          web=str(args.get("web", "")).lower() in ("true", "1", "yes"))
            if name == "research_status":
                return s.research.get(str(args.get("job_id", ""))) or {"error": "no such research job"}
            if name == "control":
                return await s.control(str(args.get("action", "")), args.get("task_id"), "aria")
            return {"error": f"unknown action {name}"}
        except (KeyError, ValueError) as exc:
            return {"error": str(exc)[:300]}

    def _prompt(self, text: str) -> str:
        lines = [f"{h['role'].upper()}: {h['text']}" for h in self.history[-HISTORY_TURNS:]]
        lines.append(f"OWNER: {text}")
        return "CONVERSATION SO FAR (newest last):\n" + "\n".join(lines) + "\n\nRespond with the JSON object."

    def status(self) -> dict:
        """Whether a model is configured and permitted for conversation, which one, and what it costs. Never flips a gate."""
        backend = self.llm.refresh_backend() if hasattr(self.llm, "refresh_backend") else getattr(self.llm, "backend", "fake")
        ok = bool(self.llm.available(backend)) if hasattr(self.llm, "available") else True
        model = self.llm.model_for("cloud_cheap") if ok else None
        cost_note = status_cost_note(backend, ok)
        return {"available": ok, "backend": backend if ok else None, "model": model, "cost_note": cost_note,
                "reason": None if ok else "conversation requires a configured and permitted model; status answers stay deterministic",
                "turns": len(self.history), "actions": list(ACTIONS)}

    def _persist(self) -> None:
        self.history = self.history[-HISTORY_KEEP:]
        self.service.store.set_kv(HISTORY_KV, {"turns": self.history, "saved_at": time.time()})

    def _deterministic(self, text: str) -> str:
        """No model permitted: a truthful status line from live state, no reasoning, no fabrication."""
        st = self.service.state()
        running = [t for t in st["tasks"] if t["status"] == "RUNNING"]
        ik = st.get("intake") or {}
        proj = ", ".join(f"{p['name']} workers {p['workers']['status'].lower()}" + (f" ({p['workers']['active']})" if p['workers']['active'] else "") for p in st["projects"])
        return ("Conversation requires model approval, so this is a deterministic status, not an answer to your question. "
                f"Projects: {proj}. Running control-plane tasks: {len(running)}. Open decisions: {len(st['inbox'])}. "
                f"Unacknowledged instructions: {len(ik.get('unacknowledged') or [])}. Open a project card for its roster, instructions and evidence.")

    async def chat(self, text: str, client_id: str | None = None) -> dict:
        """One owner message → reply. A repeated delivery of the same client_id (a retried POST, a double submit) is the
        same exchange: while it is in flight the duplicate waits for that one result; afterwards the stored exchange is
        returned; nothing is executed or costed twice. Separate sends carry separate ids and stay separate."""
        if not client_id:
            return await self._chat_once(text, None)
        live = self._inflight.get(client_id)
        if live is not None:
            return {**await asyncio.shield(live), "duplicate": True}
        done = stored_exchange(self.history, client_id)
        if done is not None:
            return {**(done if done is not False else interrupted_result()), "duplicate": True}
        task = asyncio.ensure_future(self._chat_once(text, client_id))
        self._inflight[client_id] = task
        task.add_done_callback(lambda _t: self._inflight.pop(client_id, None))
        return await asyncio.shield(task)

    async def _chat_once(self, text: str, client_id: str | None) -> dict:
        started = time.time()
        turn = "turn-" + uuid.uuid4().hex[:12]                # new owner turn = new command identity; repeated actions inside it share it (local to this call)
        self.history.append({"role": "owner", "text": text, "ts": started, "turn_id": turn, **({"client_id": client_id} if client_id else {})})
        self._persist()                                       # the request is in the store the moment it arrives, before any model call
        reply = ""
        if not self.status()["available"]:
            reply = self._deterministic(text)
            usage = usage_record(backend=None, model=None, cost_usd=0.0, rounds=0, tokens_in=0, tokens_out=0, duration_ms=int((time.time() - started) * 1000))
            self.history.append({"role": "aria", "text": reply, "ts": time.time(), "kind": "status (no model)", "usage": usage})
            self._persist()
            return {"reply": reply, "actions": [], "kind": "status (no model)", "model": None, "cost_usd": 0.0, "tokens": {"in": 0, "out": 0},
                    "duration_ms": usage["duration_ms"], "usage": usage}
        try:
            return await self._converse(text, turn, started)
        except Exception as exc:                              # anything the loop did not handle: the failure is recorded, never silently lost
            self.history.append({"role": "aria", "text": f"Request failed inside the control plane: {type(exc).__name__}: {str(exc)[:200]}. Nothing was changed.",
                                 "ts": time.time(), "kind": "error", "actions": []})
            self._persist()
            raise

    async def _converse(self, text: str, turn: str, started: float) -> dict:
        cost = 0.0
        unknown_cost = False
        tokens_in = tokens_out = 0
        rounds = retries = 0
        backend = model = None
        failed = False
        actions_taken: list[dict] = []
        pending = text
        reply = ""
        for _ in range(MAX_STEPS):
            try:
                comp = await self.llm.complete(self._prompt(pending), SCHEMA, "cloud_cheap", system=self.system, timeout=90)
            except LLMError as first:
                retries += 1
                try:                                           # one bounded retry: the CLI occasionally returns an empty error
                    comp = await self.llm.complete(self._prompt(pending), SCHEMA, "cloud_cheap", system=self.system, timeout=90)
                except LLMError as exc:
                    reply = f"Model call failed twice: {first}; {exc}. Nothing was changed."
                    failed = True
                    break
            rounds += 1
            backend, model = comp.backend, comp.model
            if comp.cost_usd is None:
                unknown_cost = True
            else:
                cost += comp.cost_usd
            tokens_in += comp.input_tokens; tokens_out += comp.output_tokens
            reply, action = _unwrap(comp.data)
            if not action or not action.get("name"):
                break
            result = await self.run_action(action["name"], action.get("args") or {}, turn)
            evidence = {k: result.get(k) for k in ("item_id", "status", "url", "task_ids", "decision_id", "note", "call_id", "job_id") if isinstance(result, dict) and result.get(k)}
            actions_taken.append({"name": action["name"], "args": action.get("args") or {}, "ok": "error" not in result, "evidence": evidence})
            self.history.append({"role": "aria_step", "text": reply, "ts": time.time()})
            self.history.append({"role": "action", "text": f"{action['name']} → {json.dumps(result, default=str)[:ACTION_RESULT_CHARS]}", "ts": time.time()})
            pending = "(continue: use the action result above to answer the owner)"
        kind = "error" if failed else next((KIND_BY_ACTION[a["name"]] for a in actions_taken if a["name"] in KIND_BY_ACTION), "question")
        usage = usage_record(backend=backend, model=model or self.llm.model_for("cloud_cheap"), cost_usd=None if unknown_cost else cost, rounds=rounds,
                             tokens_in=tokens_in, tokens_out=tokens_out, duration_ms=int((time.time() - started) * 1000), retries=retries)
        if backend is None and not failed:
            usage["backend"] = None
        self.history.append({"role": "aria", "text": reply, "ts": time.time(), "kind": kind, "usage": usage,
                             "actions": [{"name": a["name"], "ok": a["ok"], "evidence": a["evidence"]} for a in actions_taken]})
        self._persist()
        if cost or unknown_cost:
            self.service.store.add_cost(None, None, self.llm.model_for("cloud_cheap"), None if unknown_cost else cost, tokens_in, tokens_out)
        return {"reply": reply or "(no reply)", "actions": actions_taken, "kind": kind, "model": usage["model"],
                "cost_usd": usage["cost_usd"], "tokens": usage["tokens"], "duration_ms": usage["duration_ms"], "usage": usage}

    def note(self, text: str, kind: str, evidence: dict | None = None) -> None:
        """Append one Aria turn from a background worker (e.g. a SIMULATED call status) to the same conversation."""
        self.history.append({"role": "aria", "text": text, "ts": time.time(), "kind": kind,
                             "actions": [{"name": "note", "ok": True, "evidence": evidence or {}}]})
        self._persist()

    def transcript(self) -> list[dict]:
        """Owner and Aria turns (action rows folded into the Aria turn), for the panel after reload or restart."""
        return [h for h in self.history if h["role"] in ("owner", "aria")]

    def reset(self) -> None:
        self.history.clear()
        self._persist()

    def greeting(self) -> dict:
        """Opening line from live state, no model call: decisions waiting, what is running, what finished,
        spend, anything unacknowledged. Offers to walk through decisions one at a time."""
        st = self.service.state()
        running = [t for t in st["tasks"] if t["status"] == "RUNNING"]
        done_today = [t for t in st["tasks"] if t["status"] == "DONE" and time.time() - t["updated_at"] < 86400]
        blocked = st["blocked"]
        inbox = st["inbox"]
        ik = st.get("intake") or {}
        parts = ["Hello Michael."]
        if inbox:
            parts.append(f"I see {len(inbox)} decision{'s' if len(inbox) != 1 else ''} waiting. Want to go through them together?")
        else:
            parts.append("No decisions are waiting for you.")
        if running:
            parts.append(f"{len(running)} worker{'s are' if len(running) != 1 else ' is'} running: " +
                         "; ".join(f"{t['project']} {t['stage'].lower()} ({t['objective'][:60]})" for t in running[:2]) + ".")
        if done_today:
            parts.append(f"{len(done_today)} task{'s' if len(done_today) != 1 else ''} finished and verified in the last day.")
        if blocked:
            parts.append(f"{len(blocked)} blocked item{'s' if len(blocked) != 1 else ''} to look at.")
        if ik.get("unacknowledged"):
            parts.append(f"{len(ik['unacknowledged'])} instruction{'s' if len(ik['unacknowledged']) != 1 else ''} to coordinators still unacknowledged.")
        b = st["budgets"]
        parts.append(f"Known spend today ${b['spent_today_usd']:.2f} of the ${b['daily_cap_usd']:.0f} cap"
                     + (f", plus {b['unknown_usage']['calls_today']} subscription calls with no dollar figure" if b["unknown_usage"]["calls_today"] else "") + ".")
        first = inbox[0] if inbox else None
        return {"greeting": " ".join(parts),
                "first_decision": ({"decision_id": first["decision_id"], "question": first["question"], "options": first["options"] or ["yes", "no"]}
                                   if first else None),
                "counts": {"decisions": len(inbox), "running": len(running), "done_today": len(done_today), "blocked": len(blocked)}}
