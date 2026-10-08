"""Runtime configuration (control-plane-local; never in a project repo)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "runtime.yaml"


@dataclass
class ScopeLimits:
    memory_max: str = "3G"
    cpu_quota: str = "200%"
    runtime_max_sec: int = 3600
    nice: int = 10


@dataclass
class SchedulerPolicy:
    ceiling: int = 1
    reserved_cores: int = 2
    cores_per_worker: int = 2
    host_reserve_mb: int = 4096
    mem_per_worker_mb: int = 3072
    hourly_cap_usd: float = 5.0
    daily_cap_usd: float = 20.0
    expected_cost_per_worker_hour_usd: float = 2.0
    stall_after_sec: int = 600
    tick_sec: float = 5.0


@dataclass
class RuntimeConfig:
    data_dir: Path
    workspaces_dir: Path
    token_file: Path
    api_host: str = "127.0.0.1"
    api_port: int = 8477
    worker_credentials: Path = Path("~/.claude/.credentials.json").expanduser()
    worker_claude_config: Path = Path("~/.claude.json").expanduser()
    sandbox_ro_paths: list[str] = field(default_factory=list)   # e.g. ~/.nvm for node + claude
    scope: ScopeLimits = field(default_factory=ScopeLimits)
    scheduler: SchedulerPolicy = field(default_factory=SchedulerPolicy)
    models: dict[str, str] = field(default_factory=lambda: {
        "cloud_cheap": "claude-haiku-5-5",
        "cloud_strong": "claude-sonnet-5-5",
        "cloud_max": "claude-opus-5-5",
    })
    default_budget_usd: float = 3.0
    default_max_turns: int = 60
    test_port_range: tuple[int, int] = (8100, 8199)
    project_files: list[Path] = field(default_factory=list)
    open_draft_pr: bool = True
    use_bwrap: bool = True
    use_systemd_scope: bool = True
    source_file: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "RuntimeConfig":
        path = Path(path or os.environ.get("DESKTOP_AGENT_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
        raw = yaml.safe_load(path.read_text()) if path.exists() else {}
        base = path.parent
        def p(v: str) -> Path:
            return Path(v).expanduser()
        cfg = cls(
            data_dir=p(raw.get("data_dir", "~/.local/share/desktop-agent")),
            workspaces_dir=p(raw.get("workspaces_dir", "~/Desktop-Agent-work")),
            token_file=p(raw.get("token_file", "~/.config/desktop-agent/token")),
            api_host=raw.get("api", {}).get("host", "127.0.0.1"),
            api_port=int(raw.get("api", {}).get("port", 8477)),
            source_file=path,
        )
        wc = raw.get("worker_home", {})
        if wc.get("credentials"):
            cfg.worker_credentials = p(wc["credentials"])
        if wc.get("claude_config"):
            cfg.worker_claude_config = p(wc["claude_config"])
        cfg.sandbox_ro_paths = [str(p(x)) for x in raw.get("sandbox", {}).get("ro_paths", [])]
        cfg.use_bwrap = bool(raw.get("sandbox", {}).get("bwrap", True))
        cfg.use_systemd_scope = bool(raw.get("sandbox", {}).get("systemd_scope", True))
        for k, v in (raw.get("scope") or {}).items():
            if hasattr(cfg.scope, k):
                setattr(cfg.scope, k, v)
        for k, v in (raw.get("scheduler") or {}).items():
            if hasattr(cfg.scheduler, k):
                setattr(cfg.scheduler, k, v)
        if raw.get("models"):
            cfg.models.update(raw["models"])
        cfg.default_budget_usd = float(raw.get("default_budget_usd", cfg.default_budget_usd))
        cfg.default_max_turns = int(raw.get("default_max_turns", cfg.default_max_turns))
        if raw.get("test_port_range"):
            lo, hi = raw["test_port_range"]
            cfg.test_port_range = (int(lo), int(hi))
        cfg.open_draft_pr = bool(raw.get("open_draft_pr", True))
        cfg.project_files = [(base / x) if not Path(x).expanduser().is_absolute() else p(x)
                             for x in raw.get("projects", [])]
        return cfg

    def ensure_token(self) -> str:
        """Create the API bearer token on first run (mode 0640 so a desktop
        user in the same group can read it), then return it."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.token_file.exists():
            self.token_file.write_text(secrets.token_urlsafe(32))
            os.chmod(self.token_file, 0o640)
        return self.token_file.read_text().strip()
