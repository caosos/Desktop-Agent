"""Per-task git workspaces.

A workspace is a local clone (objects hard-linked from the project's local
repo, fast) with the base ref fetched from the remote so the base SHA is
the true tip. The worker owns the clone; the integration checkout is never
touched. The control plane pushes from the workspace with its own
credentials after verification.
"""
from __future__ import annotations

import asyncio
import fnmatch
import shutil
from dataclasses import dataclass
from pathlib import Path

from .project import ProjectPackage


class GitError(RuntimeError):
    pass


async def git(*args: str, cwd: Path | None = None, check: bool = True, timeout: int = 300) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise GitError(f"git {' '.join(args)} timed out")
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {err.decode(errors='replace').strip()}")
    return out.decode(errors="replace")


@dataclass
class Workspace:
    path: Path
    branch: str
    base_ref: str
    base_sha: str


async def create(project: ProjectPackage, root: Path, task_id: str, branch: str) -> Workspace:
    """Clone locally, fetch the true base tip from the remote, branch from it."""
    path = root / task_id / "repo"
    if path.exists():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    await git("clone", "--quiet", "--local", "--no-checkout", str(project.repo_path), str(path))
    await git("remote", "set-url", "origin", project.remote_url, cwd=path)
    await git("fetch", "--quiet", "origin", project.integration_branch, cwd=path)
    base_sha = (await git("rev-parse", "FETCH_HEAD", cwd=path)).strip()
    await git("checkout", "--quiet", "-b", branch, base_sha, cwd=path)
    return Workspace(path=path, branch=branch, base_ref=project.integration_branch, base_sha=base_sha)


async def head_sha(path: Path) -> str:
    return (await git("rev-parse", "HEAD", cwd=path)).strip()


async def commits_since(path: Path, base_sha: str) -> list[dict]:
    out = await git("log", "--format=%H%x1f%s%x1f%an%x1f%ct", f"{base_sha}..HEAD", cwd=path)
    rows = []
    for line in out.splitlines():
        sha, subject, author, ts = line.split("\x1f")
        rows.append({"sha": sha, "subject": subject, "author": author, "ts": int(ts)})
    return rows


async def changed_files(path: Path, base_sha: str) -> list[dict]:
    """Files changed between the base and HEAD (committed work only)."""
    out = await git("diff", "--numstat", f"{base_sha}..HEAD", cwd=path)
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            add, dele, name = parts
            files.append({"path": name, "added": int(add) if add.isdigit() else 0,
                          "deleted": int(dele) if dele.isdigit() else 0})
    return files


async def diff_text(path: Path, base_sha: str, max_bytes: int = 200_000) -> str:
    out = await git("diff", f"{base_sha}..HEAD", cwd=path)
    return out[:max_bytes] + ("\n... [truncated]" if len(out) > max_bytes else "")


async def is_dirty(path: Path) -> bool:
    return bool((await git("status", "--porcelain", cwd=path)).strip())


def outside_owned_area(files: list[str], owned_area: list[str], shared: list[str]) -> dict:
    """Paths outside the owned area and paths hitting shared contracts."""
    def matches(p: str, globs: list[str]) -> bool:
        return any(fnmatch.fnmatch(p, g) or p == g or p.startswith(g.rstrip("*").rstrip("/") + "/")
                   for g in globs)
    out = {"outside": [], "shared": []}
    for f in files:
        if owned_area and not matches(f, owned_area):
            out["outside"].append(f)
        if matches(f, shared):
            out["shared"].append(f)
    return out


async def push(path: Path, remote_url: str, branch: str) -> str:
    """Push the branch and return the SHA the remote reports for it."""
    await git("push", "--quiet", remote_url, f"{branch}:{branch}", cwd=path, timeout=600)
    out = await git("ls-remote", remote_url, f"refs/heads/{branch}", cwd=path, timeout=120)
    return out.split()[0] if out.strip() else ""


async def clean_checkout(src: Path, dest: Path, sha: str) -> Path:
    """A fresh clone of the workspace at an exact SHA, for the verifier."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    await git("clone", "--quiet", "--local", "--no-checkout", str(src), str(dest))
    await git("checkout", "--quiet", "--detach", sha, cwd=dest)
    return dest


def remove(root: Path, task_id: str) -> None:
    p = root / task_id
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
