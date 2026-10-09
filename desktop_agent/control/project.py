"""Project package loader.

A project package points at the project's own durable truth; it never
copies it (ARCHITECTURE_REVIEW §8). Until a managed project carries its own
`.agentproject/project.yaml`, the descriptor may live under this
repository's `config/projects/`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ProjectPackage:
    name: str
    repo_path: Path                 # local git repo (or worktree) used as the object source
    remote_url: str                 # where branches are pushed and base refs are fetched from
    integration_branch: str
    start_here: str
    agents_file: str
    ready_queue: str | None
    current_state: str | None
    acceptance: str | None
    test_command: str | None
    test_env: dict[str, str] = field(default_factory=dict)
    shared_contract_paths: list[str] = field(default_factory=list)
    never_read: list[str] = field(default_factory=list)
    runtime_ro_paths: list[str] = field(default_factory=list)
    worker_allowed_tools: list[str] = field(default_factory=list)
    default_read_list: list[str] = field(default_factory=list)
    github_repo: str | None = None   # owner/name for gh
    min_model_class: str | None = None   # e.g. a repo whose CLAUDE.md context exceeds the cheap class
    state_entry_by: str = "worker"       # "worker": workers append to current_state (owned area includes it);
                                         # "control": the control plane appends one entry at integration
    intake: dict | None = None           # owner-instruction intake: {issues: [..], owner_logins: [..], coordinator: {kind, session_cwd}}
    workers: dict | None = None          # grounded worker telemetry: {process_patterns: [regex with (?P<id>)], feed_url, feed_project,
                                         #   quota_shared_with_control_plane}; absent → "worker runtime not verified"
    source_file: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "ProjectPackage":
        path = Path(path).expanduser()
        raw = yaml.safe_load(path.read_text()) or {}
        required = ["name", "repo_path", "remote_url", "integration_branch", "start_here", "agents_file"]
        missing = [k for k in required if k not in raw]
        if missing:
            raise ValueError(f"{path}: missing {missing}")
        return cls(
            name=raw["name"],
            repo_path=Path(raw["repo_path"]).expanduser(),
            remote_url=raw["remote_url"],
            integration_branch=raw["integration_branch"],
            start_here=raw["start_here"],
            agents_file=raw["agents_file"],
            ready_queue=raw.get("ready_queue"),
            current_state=raw.get("current_state"),
            acceptance=raw.get("acceptance"),
            test_command=raw.get("test_command"),
            test_env={k: str(v) for k, v in (raw.get("test_env") or {}).items()},
            shared_contract_paths=list(raw.get("shared_contract_paths") or []),
            never_read=list(raw.get("never_read") or []),
            runtime_ro_paths=[str(Path(p).expanduser()) for p in (raw.get("runtime_ro_paths") or [])],
            worker_allowed_tools=list(raw.get("worker_allowed_tools") or []),
            default_read_list=list(raw.get("default_read_list") or []),
            github_repo=raw.get("github_repo"),
            min_model_class=raw.get("min_model_class"),
            state_entry_by=raw.get("state_entry_by", "worker"),
            intake=raw.get("intake"),
            workers=raw.get("workers"),
            source_file=path,
        )

    def with_state_file(self, owned_area: list[str]) -> list[str]:
        """When workers must append to the state file, every owned area includes it."""
        if self.state_entry_by == "worker" and self.current_state and self.current_state not in owned_area and owned_area:
            return list(owned_area) + [self.current_state]
        return list(owned_area)

    def bootloader_files(self) -> list[str]:
        """The files a worker must read first, in order."""
        files = [self.start_here, self.agents_file]
        for f in (self.ready_queue, self.current_state, self.acceptance):
            if f:
                files.append(f)
        return files

    def summary(self) -> dict:
        return {
            "name": self.name,
            "repo_path": str(self.repo_path),
            "remote_url": self.remote_url,
            "integration_branch": self.integration_branch,
            "bootloader": self.bootloader_files(),
            "test_command": self.test_command,
        }


def load_projects(paths: list[Path]) -> dict[str, ProjectPackage]:
    out: dict[str, ProjectPackage] = {}
    for p in paths:
        pkg = ProjectPackage.load(p)
        out[pkg.name] = pkg
    return out
