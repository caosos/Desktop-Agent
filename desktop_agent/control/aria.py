"""Aria's conversational layer, server-side: one cheap structured model call per
step, at most a few steps, with actions limited to the control plane's own
operations. The widget and panel just send text and show the reply.

Every action Aria takes is a normal control-plane operation with its own
receipt (source "aria"). Aria never edits files or runs commands.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .llm import LLM, LLMError

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
}
KIND_BY_ACTION = {"submit_goal": "new work", "send_direction": "direction", "answer_decision": "owner decision", "control": "control"}
HISTORY_KV = "aria:history"
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
        self.service = service
        self.llm = llm or service.llm
        self.history: list[dict] = list((service.store.get_kv(HISTORY_KV) or {}).get("turns") or [])   # recoverable: {"role", "text", "ts"}
        self.system = (PROMPT_FILE.read_text() if PROMPT_FILE.exists() else "You are Aria.") + \
            "\n\nYou answer with a JSON object: {\"reply\": <one or two sentences for Michael>, \"action\": null | {\"name\", \"args\"}}. " \
            "Available actions:\n" + "\n".join(f"- {k}: {v}" for k, v in ACTIONS.items()) + \
            "\nUse at most one action per step; after its result you will be asked again. Ask for confirmation before submit_goal, send_direction, answer_decision and control unless the owner already said yes/go/do it. " \
            "Classify the owner's message: a QUESTION is answered read-only from actions like get_state, project_brief, explain_task; a DIRECTION goes to the project's coordinator via send_direction; NEW WORK via submit_goal; an OWNER DECISION via answer_decision. " \
            "Ground every status claim in an action result and name its source (host process, project feed, roster table, receipt, GitHub item); if the data does not say, answer 'unknown' rather than guessing. " \
            "A submitted direction or goal is not finished work: say what will happen next and how the owner will see the result. You have no web access and no knowledge beyond these actions."

    async def run_action(self, name: str, args: dict) -> dict:
        s = self.service
        try:
            if name == "get_state":
                st = s.state()
                ik = st.get("intake") or {}
                return {k: st[k] for k in ("projects", "workers", "inbox", "blocked", "next", "costs", "paused", "hold", "feedback", "budgets")} | \
                       {"tasks": [{k: t[k] for k in ("task_id", "status", "stage", "objective", "last_activity", "model_class")} for t in st["tasks"][-10:]],
                        "shared_inbox": {"counts": ik.get("counts"), "unacknowledged": ik.get("unacknowledged"), "coordinator_activity": ik.get("coordinator_activity"),
                                         "items": [{k: i.get(k) for k in ("item_id", "project", "kind", "title", "status", "posted_at", "url")} for i in (ik.get("items") or [])[-12:]]}}
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
        cost_note = ("subscription (Claude CLI): about a cent per exchange, recorded as token-equivalent, not an invoice" if backend == "claude_cli"
                     else "metered API: recorded per token at the configured price, UNKNOWN if unpriced" if ok else "")
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

    async def chat(self, text: str) -> dict:
        """One owner message → reply, after at most MAX_STEPS model/action rounds."""
        started = time.time()
        cost = 0.0
        unknown_cost = False
        tokens_in = tokens_out = 0
        actions_taken: list[dict] = []
        pending = text
        self.history.append({"role": "owner", "text": text, "ts": started})
        reply = ""
        if not self.status()["available"]:
            reply = self._deterministic(text)
            self.history.append({"role": "aria", "text": reply, "ts": time.time(), "kind": "status (no model)"})
            self._persist()
            return {"reply": reply, "actions": [], "kind": "status (no model)", "model": None, "cost_usd": 0.0, "tokens": {"in": 0, "out": 0},
                    "duration_ms": int((time.time() - started) * 1000)}
        for _ in range(MAX_STEPS):
            try:
                comp = await self.llm.complete(self._prompt(pending), SCHEMA, "cloud_cheap", system=self.system, timeout=90)
            except LLMError as first:
                try:                                           # one bounded retry: the CLI occasionally returns an empty error
                    comp = await self.llm.complete(self._prompt(pending), SCHEMA, "cloud_cheap", system=self.system, timeout=90)
                except LLMError as exc:
                    reply = f"Model call failed twice: {first}; {exc}. Nothing was changed."
                    break
            if comp.cost_usd is None:
                unknown_cost = True
            else:
                cost += comp.cost_usd
            tokens_in += comp.input_tokens; tokens_out += comp.output_tokens
            reply = str(comp.data.get("reply") or "").strip()
            action = comp.data.get("action")
            if not action or not action.get("name"):
                break
            result = await self.run_action(action["name"], action.get("args") or {})
            evidence = {k: result.get(k) for k in ("item_id", "status", "url", "task_ids", "decision_id", "note") if isinstance(result, dict) and result.get(k)}
            actions_taken.append({"name": action["name"], "args": action.get("args") or {}, "ok": "error" not in result, "evidence": evidence})
            self.history.append({"role": "aria_step", "text": reply, "ts": time.time()})
            self.history.append({"role": "action", "text": f"{action['name']} → {json.dumps(result, default=str)[:3000]}", "ts": time.time()})
            pending = "(continue: use the action result above to answer the owner)"
        kind = next((KIND_BY_ACTION[a["name"]] for a in actions_taken if a["name"] in KIND_BY_ACTION), "question")
        self.history.append({"role": "aria", "text": reply, "ts": time.time(), "kind": kind,
                             "actions": [{"name": a["name"], "ok": a["ok"], "evidence": a["evidence"]} for a in actions_taken]})
        self._persist()
        if cost or unknown_cost:
            self.service.store.add_cost(None, None, self.llm.model_for("cloud_cheap"), None if unknown_cost else cost, tokens_in, tokens_out)
        return {"reply": reply or "(no reply)", "actions": actions_taken, "kind": kind, "model": self.llm.model_for("cloud_cheap"),
                "cost_usd": None if unknown_cost else round(cost, 5), "tokens": {"in": tokens_in, "out": tokens_out},
                "duration_ms": int((time.time() - started) * 1000)}

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
