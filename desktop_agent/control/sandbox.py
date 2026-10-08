"""Sandbox command construction: systemd-run scope + bubblewrap filesystem.

Layers 2 and 3 of ARCHITECTURE_REVIEW §7. The real HOME is hidden behind a
tmpfs; only the workspace, a per-worker HOME, and listed read-only paths
are visible. Host network stays (Stage 1).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import RuntimeConfig


def have_bwrap() -> bool:
    return shutil.which("bwrap") is not None


def have_systemd_run() -> bool:
    return shutil.which("systemd-run") is not None


def prepare_worker_home(cfg: RuntimeConfig, home: Path) -> Path:
    """Create a minimal HOME with only the Claude Code credential + config."""
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude").mkdir(exist_ok=True)
    if cfg.worker_credentials.exists():
        shutil.copy2(cfg.worker_credentials, home / ".claude" / ".credentials.json")
        os.chmod(home / ".claude" / ".credentials.json", 0o600)
    if cfg.worker_claude_config.exists():
        shutil.copy2(cfg.worker_claude_config, home / ".claude.json")
    # No settings.json: workers get Claude Code defaults, no plugins, no hooks.
    return home


def scope_prefix(cfg: RuntimeConfig, unit_name: str, runtime_max_sec: int | None = None) -> list[str]:
    if not (cfg.use_systemd_scope and have_systemd_run()):
        return []
    s = cfg.scope
    # Nice is not a scope property (systemd rejects it); apply it with nice(1) inside the scope.
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        f"--unit={unit_name}",
        f"-pMemoryMax={s.memory_max}",
        f"-pCPUQuota={s.cpu_quota}",
        f"-pRuntimeMaxSec={runtime_max_sec or s.runtime_max_sec}",
        "--", "nice", "-n", str(s.nice),
    ]


def bwrap_prefix(cfg: RuntimeConfig, *, workspace: Path, worker_home: Path,
                 ro_paths: list[str], extra_rw: list[Path] | None = None) -> list[str]:
    """Filesystem sandbox: whole root read-only, real HOME replaced by tmpfs,
    workspace and worker HOME writable, selected paths re-exposed read-only."""
    if not (cfg.use_bwrap and have_bwrap()):
        return []
    real_home = Path.home()
    args = [
        "bwrap", "--ro-bind", "/", "/",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        "--tmpfs", str(real_home),
        "--unshare-pid", "--die-with-parent", "--new-session",
    ]
    # Writable: the per-worker HOME at its own path, and the workspace.
    args += ["--bind", str(worker_home), str(worker_home)]
    args += ["--bind", str(workspace), str(workspace)]
    for p in extra_rw or []:
        args += ["--bind", str(p), str(p)]
    for p in ro_paths:
        pp = Path(p)
        if pp.exists():
            args += ["--ro-bind", str(pp), str(pp)]
    # Keep the control plane's own data dir hidden (it is under HOME).
    args += ["--setenv", "HOME", str(worker_home), "--chdir", str(workspace), "--"]
    return args


def wrap(cfg: RuntimeConfig, *, unit_name: str, workspace: Path, worker_home: Path,
         ro_paths: list[str], inner: list[str], runtime_max_sec: int | None = None,
         extra_rw: list[Path] | None = None) -> list[str]:
    """Full command line: [systemd-run ...] [bwrap ...] inner."""
    return (scope_prefix(cfg, unit_name, runtime_max_sec)
            + bwrap_prefix(cfg, workspace=workspace, worker_home=worker_home, ro_paths=ro_paths, extra_rw=extra_rw)
            + list(inner))


def worker_env(worker_home: Path, test_port: int | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a worker: PATH from the host (node/claude live under
    ~/.nvm which is re-exposed read-only), HOME swapped, no inherited secrets."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(worker_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
        "CI": "1",
        "DESKTOP_AGENT_WORKER": "1",
    }
    if test_port is not None:
        env["DESKTOP_AGENT_TEST_PORT"] = str(test_port)
    for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if k in os.environ:
            env[k] = os.environ[k]
    if extra:
        env.update(extra)
    # Never pass provider secrets through to a worker process.
    for k in list(env):
        if k.endswith("_API_KEY") or k.endswith("_TOKEN"):
            env.pop(k)
    return env
