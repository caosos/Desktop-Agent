"""File-safety rules for the private memory directory: regular non-symlink files owned by the service user with no
group/other permission bits, a bounded size, atomic 0600 writes. Every refusal names the rule, never the content."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

SEED_MAX_BYTES = 256 * 1024


class UnsafeFile(ValueError):
    """The file breaks a privacy rule; the message is safe to show (rule only, no content)."""


def ensure_private_dir(d: Path) -> Path:
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    st = d.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise UnsafeFile("private directory is not a directory")
    if st.st_uid != os.getuid():
        raise UnsafeFile("private directory is not owned by the service user")
    return d


def check_private_file(path: Path, max_bytes: int = SEED_MAX_BYTES, must_exist: bool = True) -> os.stat_result | None:
    """Refuse symlinks, non-regular files, foreign owners, any group/other bits and oversize files."""
    path = Path(path)
    try:
        st = path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise UnsafeFile("private file is missing")
        return None
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeFile("private file is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeFile("private file is not a regular file")
    if st.st_uid != os.getuid():
        raise UnsafeFile("private file is not owned by the service user")
    if st.st_mode & 0o077:
        raise UnsafeFile(f"private file has group/other permission bits ({stat.S_IMODE(st.st_mode):04o}); require 0600")
    if st.st_size > max_bytes:
        raise UnsafeFile(f"private file exceeds the {max_bytes // 1024} KiB limit")
    return st


def private_text_file(private_dir: Path | None, text: str) -> Path:
    """A 0600 file under <private_dir>/tmp holding text for one subprocess (the CLI's system prompt file); the caller
    unlinks it. Without a private directory the system temp dir is used, still 0600."""
    d = ensure_private_dir(Path(private_dir) / "tmp") if private_dir else Path(tempfile.gettempdir())
    fd, path = tempfile.mkstemp(prefix="ctx-", suffix=".md", dir=str(d))
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return Path(path)


def atomic_write_private(path: Path, data: bytes) -> None:
    """Write via a 0600 temp file in the same directory, fsync, then rename: readers see the old or the new file."""
    path = Path(path)
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
