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
    "explain_task": "details of one task: args {task_id}",
    "submit_goal": "create bounded work from a confirmed instruction: args {text, project (optional)}",
    "answer_decision": "record the owner's answer to an open decision: args {decision_id, answer}",
    "control": "pause, resume or stop: args {action, task_id (optional)}",
}

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
        self.history: list[dict] = []          # {"role": "owner"|"aria"|"action", "text": ...}
        self.system = (PROMPT_FILE.read_text() if PROMPT_FILE.exists() else "You are Aria.") + \
            "\n\nYou answer with a JSON object: {\"reply\": <one or two sentences for Michael>, \"action\": null | {\"name\", \"args\"}}. " \
            "Available actions:\n" + "\n".join(f"- {k}: {v}" for k, v in ACTIONS.items()) + \
            "\nUse at most one action per step; after its result you will be asked again. Ask for confirmation before submit_goal unless the owner already said yes/go/do it."

    async def run_action(self, name: str, args: dict) -> dict:
        s = self.service
        try:
            if name == "get_state":
                st = s.state()
                return {k: st[k] for k in ("projects", "workers", "inbox", "blocked", "next", "costs", "paused", "hold", "feedback")} | \
                       {"tasks": [{k: t[k] for k in ("task_id", "status", "stage", "objective", "last_activity", "model_class")} for t in st["tasks"][-10:]]}
            if name == "list_projects":
                return {"projects": [p.summary() for p in s.projects.values()]}
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

    async def chat(self, text: str) -> dict:
        """One owner message → reply, after at most MAX_STEPS model/action rounds."""
        started = time.time()
        cost = 0.0
        actions_taken: list[dict] = []
        pending = text
        self.history.append({"role": "owner", "text": text})
        reply = ""
        for _ in range(MAX_STEPS):
            try:
                comp = await self.llm.complete(self._prompt(pending), SCHEMA, "cloud_cheap", system=self.system, timeout=90)
            except LLMError as exc:
                reply = f"Model call failed: {exc}"
                break
            cost += comp.cost_usd or 0.0
            reply = str(comp.data.get("reply") or "").strip()
            action = comp.data.get("action")
            if not action or not action.get("name"):
                break
            result = await self.run_action(action["name"], action.get("args") or {})
            actions_taken.append({"name": action["name"], "args": action.get("args") or {}, "ok": "error" not in result})
            self.history.append({"role": "aria", "text": reply})
            self.history.append({"role": "action", "text": f"{action['name']} → {json.dumps(result, default=str)[:3000]}"})
            pending = "(continue: use the action result above to answer the owner)"
        self.history.append({"role": "aria", "text": reply})
        if cost:
            self.service.store.add_cost(None, None, self.llm.model_for("cloud_cheap"), cost)
        return {"reply": reply or "(no reply)", "actions": actions_taken, "cost_usd": round(cost, 5),
                "duration_ms": int((time.time() - started) * 1000)}

    def reset(self) -> None:
        self.history.clear()
