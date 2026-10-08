"""Model router: model_class → concrete model, with the escalation ladder."""
from __future__ import annotations

from .config import RuntimeConfig
from .contracts import LOW_RISK_TYPES

LADDER = ["cloud_cheap", "cloud_strong", "cloud_max"]


def default_class(task_type: str) -> str:
    """Coding defaults to the strong class; low-risk types may start cheap
    (ARCHITECTURE_REVIEW §1.2)."""
    return "cloud_cheap" if task_type in LOW_RISK_TYPES else "cloud_strong"


def resolve(cfg: RuntimeConfig, model_class: str, adapter: str = "claude_headless") -> str:
    """Model for a class; an adapter may carry its own class → model map."""
    table = cfg.adapter_models.get(adapter) or cfg.models
    try:
        return table[model_class]
    except KeyError:
        raise ValueError(f"unknown model_class {model_class!r} for {adapter}; known: {sorted(table)}")


def at_least(model_class: str, minimum: str | None) -> str:
    """The higher of the two classes on the ladder (a project may set a floor)."""
    if not minimum or minimum not in LADDER or model_class not in LADDER:
        return model_class
    return LADDER[max(LADDER.index(model_class), LADDER.index(minimum))]


def escalate(model_class: str) -> str | None:
    """Next class up, or None at the top."""
    try:
        i = LADDER.index(model_class)
    except ValueError:
        return None
    return LADDER[i + 1] if i + 1 < len(LADDER) else None
