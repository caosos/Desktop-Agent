"""Import of the owner's private Markdown seed: parsed locally into container records (headings route, bullets and
paragraphs become atomic facts), the seed's hash kept as the import revision. The seed is never sent anywhere."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from .model import CORE_CONTAINERS, classify_container, new_record
from .safety import SEED_MAX_BYTES, atomic_write_private, check_private_file

SEED_NAME = "aria_working_knowledge.md"
CHUNK_CHARS = 600
CORE_PER_CONTAINER = 6
_HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_KEYED = re.compile(r"^([A-Za-z][\w' /&-]{1,40}):\s+(.+)$")


def parse_markdown(text: str) -> list[dict]:
    """Chunks: {container, subject, key, text, tags, sensitivity?}. Heading path = subject; a heading marked
    '(sensitive)' raises sensitivity; the first chunks under a core container are tagged 'core'."""
    chunks: list[dict] = []
    path: list[str] = []
    container = "identity_and_biography"
    sensitive_heading = False
    para: list[str] = []
    core_count: dict[str, int] = {}

    def flush_para() -> None:
        if para:
            emit(" ".join(para))
            para.clear()

    def emit(body: str, key: str | None = None) -> None:
        body = body.strip()
        if not body:
            return
        for piece in _split(body):
            tags = ["seed"]
            if container in CORE_CONTAINERS and core_count.get(container, 0) < CORE_PER_CONTAINER:
                tags.append("core"); core_count[container] = core_count.get(container, 0) + 1
            chunks.append({"container": container, "subject": " / ".join(path) or "seed", "key": key, "text": piece, "tags": tags,
                           **({"sensitivity": "high"} if sensitive_heading else {})})

    for raw in text.splitlines():
        line = raw.rstrip()
        m = _HEAD.match(line)
        if m:
            flush_para()
            level, title = len(m.group(1)), m.group(2)
            path = path[:level - 1] + [re.sub(r"\s*\(sensitive\)\s*", "", title, flags=re.I).strip()]
            sensitive_heading = "(sensitive)" in title.lower()
            container = classify_container(" ".join(path), default=container if level > 1 else "identity_and_biography")
            continue
        b = _BULLET.match(line)
        if b:
            flush_para()
            body = b.group(1).strip()
            k = _KEYED.match(body)
            emit(k.group(2) if k else body, k.group(1).strip() if k else None)
            continue
        if not line.strip():
            flush_para()
            continue
        if line.startswith((" ", "\t")) and chunks and not para:      # continuation of the previous bullet
            chunks[-1]["text"] = (chunks[-1]["text"] + " " + line.strip())[:CHUNK_CHARS]
            continue
        para.append(line.strip())
    flush_para()
    return chunks


def _split(body: str) -> list[str]:
    if len(body) <= CHUNK_CHARS:
        return [body]
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", body):
        if cur and len(cur) + len(sent) + 1 > CHUNK_CHARS:
            out.append(cur); cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur:
        out.append(cur)
    return out


def seed_status(private_dir: Path) -> dict:
    """Metadata only: whether a seed is loaded, its size, mtime and short SHA-256. Never its content."""
    p = Path(private_dir) / SEED_NAME
    try:
        st = check_private_file(p)
    except ValueError as exc:
        return {"loaded": False, "reason": str(exc), "path": str(p)}
    return {"loaded": True, "bytes": st.st_size, "mtime": st.st_mtime, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()[:12], "path": str(p)}


def write_seed(private_dir: Path, data: bytes) -> dict:
    if len(data) > SEED_MAX_BYTES:
        raise ValueError(f"seed exceeds the {SEED_MAX_BYTES // 1024} KiB limit")
    data.decode("utf-8")                                       # must be text; a binary upload is refused before anything is written
    atomic_write_private(Path(private_dir) / SEED_NAME, data)
    return seed_status(private_dir)


def import_seed(store, private_dir: Path) -> dict:
    """Parse the (already safe) seed into records. Facts unchanged since the previous seed are confirmed, new ones
    created, and import-created facts absent from the new seed are retired as superseded by the new seed revision."""
    p = Path(private_dir) / SEED_NAME
    check_private_file(p)
    raw = p.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()[:12]
    chunks = parse_markdown(raw.decode("utf-8", errors="replace"))
    created = confirmed = 0
    seen: set[str] = set()
    by_container: dict[str, int] = {}
    for n, ch in enumerate(chunks, 1):
        rec = new_record(container=ch["container"], subject=ch["subject"], key=ch.get("key"), text=ch["text"], created_by="import",
                         source_type="seed", source_ref=f"seed:{sha}#{n}", confidence=0.9, tags=ch["tags"], sensitivity=ch.get("sensitivity"))
        added = store.add(rec, actor="import")
        seen.add(added["id"])
        confirmed += added["duplicate"]; created += not added["duplicate"]
        by_container[ch["container"]] = by_container.get(ch["container"], 0) + 1
    retired = 0
    for old in store.list(status="active", limit=100000):
        if old["source_type"] == "seed" and old["id"] not in seen:
            store.set_status(old["id"], "superseded", actor="import", note=f"absent from seed:{sha}"); retired += 1
    store.set_meta("seed_sha", sha); store.set_meta("seed_bytes", str(len(raw))); store.set_meta("last_import_at", str(time.time()))
    return {"seed_sha256": sha, "bytes": len(raw), "chunks": len(chunks), "created": created, "confirmed": confirmed, "retired": retired, "by_container": by_container}
