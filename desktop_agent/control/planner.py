"""Planner: a plain-language request → project → bounded task contracts.

One cheap structured model call (two when the project is ambiguous). The
planner never executes anything; its output is validated here, turned into
contracts by the service, and recorded with a receipt carrying the model,
cost and tokens. Questions it cannot answer become owner decisions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLM, Completion, LLMError
from .project import ProjectPackage

MAX_TASKS = 6
CLASSES = ["cloud_cheap", "cloud_strong", "cloud_max"]
TASK_TYPES = ["docs", "tests_only", "config", "code"]

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"type": "string"},
        "summary": {"type": "string"},
        "tasks": {"type": "array", "maxItems": MAX_TASKS, "items": {
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "task_type": {"type": "string", "enum": TASK_TYPES},
                "owned_area": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "acceptance": {"type": "string"},
                "expected_artifacts": {"type": "array", "items": {"type": "string"}},
                "dependencies": {"type": "array", "items": {"type": "integer"}},
                "model_class": {"type": "string", "enum": CLASSES},
                "model_reason": {"type": "string"},
                "why_now": {"type": "string"},
            },
            "required": ["objective", "task_type", "owned_area", "model_class", "why_now"],
        }},
        "questions": {"type": "array", "items": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
                           "why": {"type": "string"}},
            "required": ["question", "why"],
        }},
        "blocked_reason": {"type": "string"},
    },
    "required": ["project", "summary", "tasks", "questions"],
}

SYSTEM = """You are the planner of a build platform that runs disposable, sandboxed coding workers.
Turn the owner's request into at most {max_tasks} bounded tasks for ONE project. Each task must be
finishable by one worker in about an hour, name the files or globs it may edit (owned_area, relative
to the repo root; never the shared contract paths), state what proves it done (acceptance), and pick the
LEAST EXPENSIVE model class that can do it accurately:
  cloud_cheap  = Luna-class: extraction, classification, summaries, small docs/config/test edits
  cloud_strong = Sol-class: normal development, coding, debugging, research
  cloud_max    = Astra-class: only when the extra capability is clearly justified
Give model_reason in one sentence. Order tasks so dependencies (by index into this list) come first.
Ask a question ONLY for a genuine owner decision (product choice, spend, destructive change, credentials,
external dependency); otherwise decide yourself. If the request cannot be made into bounded tasks, say
why in blocked_reason and return no tasks. Respond with the JSON object only."""


@dataclass
class Plan:
    project: str
    summary: str
    tasks: list[dict]
    questions: list[dict]
    blocked_reason: str | None
    completion: Completion | None = None
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"project": self.project, "summary": self.summary, "tasks": self.tasks, "questions": self.questions,
             "blocked_reason": self.blocked_reason, "problems": self.problems}
        if self.completion:
            d["model"] = self.completion.model
            d["backend"] = self.completion.backend
            d["cost_usd"] = self.completion.cost_usd
            d["tokens"] = {"in": self.completion.input_tokens, "out": self.completion.output_tokens}
        return d


def _excerpt(repo: Path, rel: str | None, max_chars: int) -> str:
    if not rel:
        return ""
    p = repo / rel
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    head, tail = text[: max_chars * 2 // 3], text[-max_chars // 3:]
    return head + "\n[...]\n" + tail


def project_context(pkg: ProjectPackage, max_chars: int = 14000) -> str:
    parts = [f"PROJECT {pkg.name} (integration branch {pkg.integration_branch})",
             f"shared contract paths (never edit): {', '.join(pkg.shared_contract_paths) or 'none'}",
             f"test command: {pkg.test_command or 'none'}"]
    budget = max_chars
    for label, rel, share in (("START_HERE", pkg.start_here, 0.35), ("READY_QUEUE", pkg.ready_queue, 0.4),
                              ("AGENTS (head)", pkg.agents_file, 0.25)):
        ex = _excerpt(pkg.repo_path, rel, int(budget * share))
        if ex:
            parts.append(f"--- {label}: {rel} ---\n{ex}")
    return "\n".join(parts)


class Planner:
    def __init__(self, llm: LLM, projects: dict[str, ProjectPackage]):
        self.llm, self.projects = llm, projects

    async def plan(self, request: str, project_hint: str | None = None, answers: list[dict] | None = None,
                   model_class: str = "cloud_cheap") -> Plan:
        if not self.projects:
            return Plan(project="", summary="", tasks=[], questions=[], blocked_reason="no projects are onboarded")
        project = project_hint if project_hint in self.projects else None
        if project is None and len(self.projects) == 1:
            project = next(iter(self.projects))
        if project is None:
            project = await self._pick_project(request)
        pkg = self.projects.get(project)
        if pkg is None:
            return Plan(project="", summary="", tasks=[], questions=[{
                "question": f"Which project is this for? ({', '.join(self.projects)})", "options": list(self.projects),
                "why": "the request names no known project"}], blocked_reason=None)
        prompt = (f"OWNER REQUEST:\n{request}\n\n"
                  + (f"OWNER ANSWERS TO EARLIER QUESTIONS:\n{json.dumps(answers)}\n\n" if answers else "")
                  + project_context(pkg))
        try:
            comp = await self.llm.complete(prompt, PLAN_SCHEMA, model_class, system=SYSTEM.format(max_tasks=MAX_TASKS))
        except LLMError as exc:
            return Plan(project=project, summary="", tasks=[], questions=[], blocked_reason=f"planner model call failed: {exc}")
        return self._validate(comp, pkg)

    async def _pick_project(self, request: str) -> str | None:
        names = list(self.projects)
        schema = {"type": "object", "properties": {"project": {"type": "string", "enum": names + ["unknown"]},
                                                   "reason": {"type": "string"}}, "required": ["project"]}
        listing = "\n".join(f"- {n}: integration {p.integration_branch}; start file {p.start_here}; repo {p.repo_path}"
                            for n, p in self.projects.items())
        try:
            comp = await self.llm.complete(f"Which project does this request concern? Answer 'unknown' if unclear.\n\nREQUEST:\n{request}\n\nPROJECTS:\n{listing}",
                                           schema, "cloud_cheap")
        except LLMError:
            return None
        p = comp.data.get("project")
        return p if p in self.projects else None

    def _validate(self, comp: Completion, pkg: ProjectPackage) -> Plan:
        d = comp.data
        problems: list[str] = []
        tasks: list[dict] = []
        for i, t in enumerate((d.get("tasks") or [])[:MAX_TASKS]):
            if not t.get("objective") or not t.get("owned_area"):
                problems.append(f"task {i}: missing objective or owned_area"); continue
            if t.get("task_type") not in TASK_TYPES:
                t["task_type"] = "code"
            if t.get("model_class") not in CLASSES:
                t["model_class"] = "cloud_strong"
            shared = [p for p in t["owned_area"] if p in pkg.shared_contract_paths]
            if shared:
                problems.append(f"task {i}: owned_area includes shared contract paths {shared}"); continue
            deps = [x for x in (t.get("dependencies") or []) if isinstance(x, int) and 0 <= x < i]
            t["dependencies"] = deps
            tasks.append(t)
        return Plan(project=pkg.name, summary=str(d.get("summary") or ""), tasks=tasks,
                    questions=[q for q in (d.get("questions") or []) if q.get("question")],
                    blocked_reason=d.get("blocked_reason") or None, completion=comp, problems=problems)
