"""Bounded task contracts (ARCHITECTURE_REVIEW §8, IMPLEMENTATION_PLAN §5)."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field, asdict

from .project import ProjectPackage

RISK_LOW = "low_risk"
RISK_NORMAL = "normal"

# Task types that may start on the cheap class (ARCHITECTURE_REVIEW §1.2).
LOW_RISK_TYPES = {"docs", "tests_only", "config"}


@dataclass
class Budget:
    usd: float
    max_turns: int
    wall_clock_sec: int


@dataclass
class TaskContract:
    task_id: str
    objective: str
    why_now: str
    project: str
    base_ref: str                      # branch name; resolved SHA recorded at launch
    owned_area: list[str]              # globs relative to repo root
    read_list: list[str]
    acceptance_tests: list[str]        # commands, run by the verifier
    allowed_tools: list[str]
    forbidden_actions: list[str]
    model_class: str
    budget: Budget
    dependencies: list[str] = field(default_factory=list)
    expected_artifacts: list[str] = field(default_factory=list)
    task_type: str = "code"
    attempt: int = 1
    supersedes: str | None = None
    worker_adapter: str = "claude_headless"
    goal_id: str | None = None
    created_at: float = field(default_factory=time.time)
    result: dict | None = None          # filled by the control plane only

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskContract":
        d = dict(d)
        b = d.pop("budget")
        return cls(budget=Budget(**b), **d)

    def hash(self) -> str:
        """Hash of the immutable contract fields (result excluded)."""
        d = self.to_dict()
        d.pop("result", None)
        return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def branch_name(self) -> str:
        return f"agent/{self.task_id}"


_DEFAULT_FORBIDDEN = [
    "git push", "git merge", "gh pr", "deploy", "restart services",
    "touch another worktree", "read .env or credentials",
    "edit files outside owned_area", "modify shared contract paths",
]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:32] or "task"


def compile_contract(*, project: ProjectPackage, objective: str, why_now: str,
                     goal_id: str | None, task_type: str, owned_area: list[str],
                     model_class: str, budget_usd: float, max_turns: int, wall_clock_sec: int,
                     read_list: list[str] | None = None, acceptance_tests: list[str] | None = None,
                     allowed_tools: list[str] | None = None, expected_artifacts: list[str] | None = None,
                     dependencies: list[str] | None = None, attempt: int = 1,
                     supersedes: str | None = None) -> TaskContract:
    """Template compiler for Stage 1: one goal → one contract. The planner
    model that turns a goal into several contracts arrives in Stage 2."""
    task_id = f"{_slug(objective)}-{uuid.uuid4().hex[:6]}"
    reads = list(project.bootloader_files()) + list(project.default_read_list) + list(read_list or [])
    tests = list(acceptance_tests or [])
    if not tests and project.test_command:
        tests = [project.test_command]
    return TaskContract(
        task_id=task_id,
        objective=objective.strip(),
        why_now=why_now.strip(),
        project=project.name,
        base_ref=project.integration_branch,
        owned_area=list(owned_area),
        read_list=reads,
        acceptance_tests=tests,
        allowed_tools=list(allowed_tools or project.worker_allowed_tools),
        forbidden_actions=list(_DEFAULT_FORBIDDEN),
        model_class=model_class,
        budget=Budget(usd=budget_usd, max_turns=max_turns, wall_clock_sec=wall_clock_sec),
        dependencies=list(dependencies or []),
        expected_artifacts=list(expected_artifacts or []),
        task_type=task_type,
        attempt=attempt,
        supersedes=supersedes,
        goal_id=goal_id,
    )
