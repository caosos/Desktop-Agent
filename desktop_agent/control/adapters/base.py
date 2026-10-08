"""Executor adapter interface.

An adapter turns a task contract into a command line and turns the
executor's output lines into normalised events. The launcher owns the
process; adapters never spawn anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..contracts import TaskContract
from ..project import ProjectPackage


@dataclass
class LaunchSpec:
    argv: list[str]
    env_extra: dict[str, str] = field(default_factory=dict)
    stdin_text: str | None = None


@dataclass
class Parsed:
    """What one output line meant. `events` are (type, payload) pairs; the
    launcher wraps them with provenance. `final` is set on the terminal line."""
    events: list[tuple[str, dict]] = field(default_factory=list)
    final: dict | None = None            # {"ok": bool, "claim": str, "cost_usd": float, "model": str, ...}


class Adapter(Protocol):
    name: str

    def launch(self, *, contract: TaskContract, project: ProjectPackage, prompt: str,
               model: str, workspace: Path) -> LaunchSpec: ...

    def parse_line(self, line: str) -> Parsed: ...
