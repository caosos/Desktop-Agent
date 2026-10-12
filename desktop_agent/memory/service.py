"""MemoryService: the one object the control plane touches. Opens the private store (or stays disabled with a rule
message), builds the per-turn private context, runs capture after a turn, imports the seed, exports/imports the private
bundle for Dot Aria reconciliation, and reports content-free status. Nothing here logs memory content."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import capture, importer, retrieval
from .episodes import Episodes
from .model import CONTAINERS, SCHEMA_VERSION
from .safety import UnsafeFile, ensure_private_dir
from .store import MemoryStore

BUNDLE_KIND = "desktop-agent-private-memory-bundle"


class MemoryService:
    def __init__(self, private_dir: Path | None):
        self.dir = Path(private_dir) if private_dir else None
        self.store: MemoryStore | None = None
        self.reason: str | None = None
        self.last_turn: dict | None = None            # counts only
        if self.dir is None:
            self.reason = "no private directory configured"
            return
        try:
            self.store = MemoryStore(self.dir)
            self.episodes = Episodes(self.store)       # every exchange, for life (owner directive 2026-10-12)
        except (UnsafeFile, ValueError, OSError) as exc:
            self.reason = f"private memory disabled: {str(exc)[:120]}"

    @property
    def enabled(self) -> bool:
        return self.store is not None

    # ---- the two per-turn calls ---------------------------------------------
    def context_for(self, question: str) -> dict:
        if not self.enabled:
            return {"text": "", "ids": [], "containers": [], "pending": 0}
        out = retrieval.retrieve(self.store, question)
        recall = self.episodes.recall(question)           # where we left off + relevant earlier exchanges: one continuous conversation
        if recall:
            out["text"] = (out["text"] + "\n\n" + recall) if out["text"] else (retrieval.HEADER + "\n\n" + recall)
        return out

    def after_turn(self, owner_text: str, turn_id: str, aria_text: str = "", kind: str = "", actions: list | None = None, memory_ids: list | None = None) -> dict:
        """Archive the exchange for life and capture durable facts from the owner's message; returns counts only (never content)."""
        if not self.enabled:
            return {}
        self.episodes.record(turn_id, owner_text, aria_text, kind, actions, memory_ids)
        cands = capture.analyse(owner_text)
        counts = capture.commit(self.store, cands, source_ref=f"aria_turn:{turn_id}") if cands else {"committed": 0, "pending": 0, "superseded": 0, "confirmed": 0, "ignored": 0}
        self.last_turn = {**counts, "at": time.time()}
        return counts

    @contextmanager
    def private_file(self, text: str):
        """A 0600 file in the private directory holding text for one subprocess call (e.g. the CLI's system prompt file),
        removed afterwards. Keeps private context out of argv, where any local process could read it."""
        d = ensure_private_dir((self.dir or Path(tempfile.gettempdir())) / "tmp")
        fd, path = tempfile.mkstemp(prefix="ctx-", suffix=".md", dir=str(d))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            yield Path(path)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    # ---- owner operations ---------------------------------------------------
    def import_markdown(self, data: bytes) -> dict:
        if not self.enabled:
            raise UnsafeFile(self.reason or "private memory disabled")
        self.store.backup()
        importer.write_seed(self.dir, data)
        return importer.import_seed(self.store, self.dir)

    def export_bundle(self) -> dict:
        """Private, versioned bundle (all records with provenance) for owner-local reconciliation with Dot Aria."""
        rev = self.store.revision()
        recs = self.store.all_records()
        body = json.dumps(recs, sort_keys=True, default=str).encode()
        return {"kind": BUNDLE_KIND, "schema_version": SCHEMA_VERSION, "exported_at": time.time(), "revision": rev, "bundle_id": uuid.uuid4().hex[:12],
                "content_sha256": hashlib.sha256(body).hexdigest()[:16], "records": recs}

    def import_bundle(self, bundle: dict) -> dict:
        if bundle.get("kind") != BUNDLE_KIND or int(bundle.get("schema_version", 0)) > SCHEMA_VERSION:
            raise ValueError("not a private memory bundle of a supported schema version")
        self.store.backup()
        created = updated = same = 0
        for r in bundle.get("records") or []:
            cur = self.store.get(r["id"])
            if cur is None:
                self.store.add({k: v for k, v in r.items() if k != "duplicate"}, actor="bundle"); created += 1
            elif int(r.get("revision", 0)) > int(cur["revision"]):
                self.store.update(r["id"], {k: r[k] for k in ("text", "subject", "key", "tags", "confidence", "sensitivity", "container", "valid_until") if k in r}, actor="bundle")
                if r.get("status") != cur["status"]:
                    self.store.set_status(r["id"], r["status"], actor="bundle", note="bundle")
                updated += 1
            else:
                same += 1
        self.store.set_meta("last_reconciled_bundle", str(bundle.get("bundle_id", ""))); self.store.set_meta("last_reconciled_at", str(time.time()))
        return {"created": created, "updated": updated, "unchanged": same, "revision": self.store.revision()}

    # ---- status (content-free) -------------------------------------------------
    def status(self) -> dict:
        if not self.enabled:
            return {"enabled": False, "reason": self.reason, "loaded": False}
        st = self.store
        counts = st.counts()
        totals = {s: sum(c[s] for c in counts.values()) for s in ("active", "pending", "superseded", "expired", "deleted")}
        seed = importer.seed_status(self.dir)
        meta = lambda k: (float(st.meta(k)) if st.meta(k) else None)   # noqa: E731
        return {"enabled": True, "loaded": totals["active"] > 0, "revision": st.revision(), "records": totals, "containers": counts,
                "seed": {k: v for k, v in seed.items() if k != "path"},
                "last_import_at": meta("last_import_at"), "last_retrieval_at": meta("last_retrieval_at"), "last_capture_at": meta("last_capture_at"),
                "last_turn": self.last_turn, "capture": {c: st.capture_on(c) for c in CONTAINERS},
                "backups": len(st.backups()),
                "conversation": {"exchanges_archived": self.episodes.count(), "last_exchange_at": (self.episodes.last(1) or [{}])[0].get("ts"), "retention": "for life; owner deletes in the Memory Center"},
                "dot_aria": {"synced": False, "transport": "none yet (separate stores; no GitHub/Slack transport for personal memory)",
                             "last_reconciled_bundle": st.meta("last_reconciled_bundle"), "last_reconciled_at": meta("last_reconciled_at")}}
