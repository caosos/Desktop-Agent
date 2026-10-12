"""The private memory store: one SQLite file (0600) in the private directory, versioned schema, FTS5 full-text index,
per-record revision history and a usage log ("why did Aria remember/use this"), per-container capture settings,
atomic backup/restore. Nothing here logs; errors name a rule, never a memory."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from .model import CONTAINERS, SCHEMA_VERSION, STATUSES
from .safety import check_private_file, ensure_private_dir

DB_NAME = "memory.sqlite"
BACKUPS_KEEP = 10
JSON_COLS = ("tags", "provenance")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, container TEXT, subject TEXT, key TEXT, text TEXT, source_type TEXT, source_ref TEXT,
  first_seen_at REAL, last_confirmed_at REAL, valid_from REAL, valid_until REAL, confidence REAL, sensitivity TEXT,
  status TEXT, supersedes TEXT, superseded_by TEXT, tags TEXT, created_by TEXT, revision INTEGER, dedup TEXT,
  provenance TEXT, updated_at REAL);
CREATE INDEX IF NOT EXISTS records_cs ON records(container, status);
CREATE INDEX IF NOT EXISTS records_dedup ON records(dedup);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(id UNINDEXED, subject, key, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS history (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, record_id TEXT, action TEXT, actor TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS uses (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, record_id TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS settings (container TEXT PRIMARY KEY, capture_on INTEGER);
"""


class MemoryStore:
    def __init__(self, private_dir: Path):
        self.dir = ensure_private_dir(Path(private_dir))
        self.path = self.dir / DB_NAME
        check_private_file(self.path, max_bytes=1 << 30, must_exist=False)      # an existing file must already be safe
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._harden()
        self._db.executescript(SCHEMA)
        cur = self._meta("schema_version")
        if cur is None:
            self._set_meta("schema_version", str(SCHEMA_VERSION)); self._set_meta("revision", "0")
        elif int(cur) > SCHEMA_VERSION:
            raise ValueError("memory store schema is newer than this build")
        self._db.commit(); self._harden()

    # ---- files and meta -----------------------------------------------------
    def _harden(self) -> None:
        for p in self.dir.glob(DB_NAME + "*"):
            try:
                os.chmod(p, 0o600)
            except FileNotFoundError:
                pass

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def meta(self, key: str) -> str | None:
        with self._lock:
            return self._meta(key)

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._set_meta(key, value); self._db.commit()

    def _bump(self, record_id: str, action: str, actor: str, detail: dict | None = None) -> None:
        """Every write advances the store revision and leaves one history row (immutable provenance trail)."""
        rev = int(self._meta("revision") or 0) + 1
        self._set_meta("revision", str(rev)); self._set_meta("updated_at", str(time.time()))
        self._db.execute("INSERT INTO history (ts, record_id, action, actor, detail) VALUES (?,?,?,?,?)",
                         (time.time(), record_id, action, actor, json.dumps(detail or {}, default=str)))

    def revision(self) -> dict:
        """Store revision counter and a short SHA-256 over (id, revision, status) of every record: comparable, content-free."""
        with self._lock:
            rows = self._db.execute("SELECT id, revision, status FROM records ORDER BY id").fetchall()
            h = hashlib.sha256("\n".join(f"{r['id']}:{r['revision']}:{r['status']}" for r in rows).encode()).hexdigest()[:12]
            return {"revision": int(self._meta("revision") or 0), "sha": h, "records": len(rows), "schema_version": int(self._meta("schema_version") or 0)}

    # ---- records ------------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for c in JSON_COLS:
            d[c] = json.loads(d.get(c) or ("[]" if c == "tags" else "{}"))
        d.pop("updated_at", None)
        return d

    def _index(self, rec: dict) -> None:
        self._db.execute("DELETE FROM records_fts WHERE id=?", (rec["id"],))
        self._db.execute("INSERT INTO records_fts (id, subject, key, text) VALUES (?,?,?,?)", (rec["id"], rec["subject"], rec["key"] or "", rec["text"]))

    def add(self, rec: dict, actor: str = "control") -> dict:
        """Insert; an identical active/pending fact (same dedup key) is confirmed instead of duplicated."""
        with self._lock:
            dup = self._db.execute("SELECT * FROM records WHERE dedup=? AND status IN ('active','pending') LIMIT 1", (rec["dedup"],)).fetchone()
            if dup is not None:
                self._db.execute("UPDATE records SET last_confirmed_at=?, updated_at=? WHERE id=?", (time.time(), time.time(), dup["id"]))
                self._bump(dup["id"], "confirmed", actor); self._db.commit(); self._harden()
                return {**self._row(self._db.execute("SELECT * FROM records WHERE id=?", (dup["id"],)).fetchone()), "duplicate": True}
            cols = [c for c in rec if c != "duplicate"]
            vals = [json.dumps(rec[c]) if c in JSON_COLS else rec[c] for c in cols]
            self._db.execute(f"INSERT INTO records ({','.join(cols)}, updated_at) VALUES ({','.join('?' * len(cols))}, ?)", vals + [time.time()])
            self._index(rec); self._bump(rec["id"], "created", actor, {"created_by": rec["created_by"], "container": rec["container"]})
            self._db.commit(); self._harden()
            return {**rec, "duplicate": False}

    def get(self, record_id: str) -> dict | None:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())

    def list(self, container: str | None = None, status: str | None = None, q: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        where, args = [], []
        if container:
            where.append("r.container=?"); args.append(container)
        if status:
            where.append("r.status=?"); args.append(status)
        sql = "SELECT r.* FROM records r"
        if q.strip():
            sql += " JOIN records_fts f ON f.id=r.id"; where.append("records_fts MATCH ?"); args.append(fts_query(q))
        sql += (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.last_confirmed_at DESC LIMIT ? OFFSET ?"
        with self._lock:
            return [self._row(r) for r in self._db.execute(sql, args + [limit, offset]).fetchall()]

    def search(self, q: str, containers: list[str], limit: int = 20, now: float | None = None) -> list[dict]:
        """Active, unexpired records in the given containers matching the full-text query, best match first."""
        if not q.strip() or not containers:
            return []
        now = now or time.time()
        marks = ",".join("?" * len(containers))
        sql = (f"SELECT r.*, bm25(records_fts) AS rank FROM records_fts f JOIN records r ON r.id=f.id WHERE records_fts MATCH ? "
               f"AND r.status='active' AND r.container IN ({marks}) AND (r.valid_until IS NULL OR r.valid_until > ?) ORDER BY rank LIMIT ?")
        with self._lock:
            rows = self._db.execute(sql, [fts_query(q), *containers, now, limit]).fetchall()
        return [{**self._row(r), "rank": r["rank"]} for r in rows]

    def active(self, container: str, limit: int = 50, now: float | None = None) -> list[dict]:
        now = now or time.time()
        with self._lock:
            rows = self._db.execute("SELECT * FROM records WHERE container=? AND status='active' AND (valid_until IS NULL OR valid_until > ?) "
                                    "ORDER BY confidence DESC, last_confirmed_at DESC LIMIT ?", (container, now, limit)).fetchall()
        return [self._row(r) for r in rows]

    def find_conflict(self, container: str, subject: str, key: str | None) -> dict | None:
        """An active record about the same subject/key (a different value means a conflict or a correction)."""
        with self._lock:
            r = self._db.execute("SELECT * FROM records WHERE container=? AND lower(subject)=lower(?) AND lower(coalesce(key,''))=lower(?) AND status='active' "
                                 "ORDER BY last_confirmed_at DESC LIMIT 1", (container, subject, key or "")).fetchone()
        return self._row(r)

    def find_related(self, containers: list[str], text: str, min_shared: int = 2) -> dict | None:
        """The best active record in those containers sharing at least `min_shared` significant words with the text
        (a correction rarely repeats the stored subject verbatim, and may name a different container)."""
        words = {w for w in fts_query(text).replace('"', "").replace("*", "").split(" OR ") if w}
        for hit in self.search(text, containers, limit=5):
            hay = " ".join((hit["text"], hit["subject"] or "", hit["key"] or "")).lower()
            if len({w for w in words if w in hay}) >= min_shared:
                return hit
        return None

    def update(self, record_id: str, fields: dict, actor: str = "owner") -> dict | None:
        allowed = {"text", "subject", "key", "tags", "confidence", "sensitivity", "container", "valid_until"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if "container" in fields and fields["container"] not in CONTAINERS:
            raise ValueError("unknown memory container")
        with self._lock:
            cur = self.get(record_id)
            if cur is None or not fields:
                return cur
            sets = ", ".join(f"{k}=?" for k in fields) + ", revision=revision+1, updated_at=?"
            vals = [json.dumps(v) if k in JSON_COLS else v for k, v in fields.items()] + [time.time(), record_id]
            self._db.execute(f"UPDATE records SET {sets} WHERE id=?", vals)
            self._index(self.get(record_id)); self._bump(record_id, "edited", actor, {"fields": sorted(fields)})
            self._db.commit(); self._harden()
            return self.get(record_id)

    def set_status(self, record_id: str, status: str, actor: str = "owner", note: str = "") -> dict | None:
        if status not in STATUSES:
            raise ValueError("invalid memory status")
        with self._lock:
            if self.get(record_id) is None:
                return None
            self._db.execute("UPDATE records SET status=?, revision=revision+1, updated_at=? WHERE id=?", (status, time.time(), record_id))
            self._bump(record_id, status, actor, {"note": note[:120]}); self._db.commit(); self._harden()
            return self.get(record_id)

    def supersede(self, old_id: str, new_rec: dict, actor: str = "owner") -> dict:
        """A correction: the new record becomes the truth, the old one stays as history marked superseded."""
        with self._lock:
            new_rec = {**new_rec, "supersedes": old_id}
            added = self.add(new_rec, actor)
            self._db.execute("UPDATE records SET status='superseded', superseded_by=?, revision=revision+1, updated_at=? WHERE id=?",
                             (added["id"], time.time(), old_id))
            self._bump(old_id, "superseded", actor, {"by": added["id"]}); self._db.commit(); self._harden()
            return added

    def expire_due(self, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            rows = self._db.execute("SELECT id FROM records WHERE status='active' AND valid_until IS NOT NULL AND valid_until <= ?", (now,)).fetchall()
            for r in rows:
                self._db.execute("UPDATE records SET status='expired', revision=revision+1, updated_at=? WHERE id=?", (now, r["id"]))
                self._bump(r["id"], "expired", "control")
            if rows:
                self._db.commit(); self._harden()
            return len(rows)

    def counts(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT container, status, COUNT(*) AS n FROM records GROUP BY container, status").fetchall()
        out = {c: {s: 0 for s in STATUSES} for c in CONTAINERS}
        for r in rows:
            out.setdefault(r["container"], {s: 0 for s in STATUSES})[r["status"]] = r["n"]
        return out

    # ---- provenance, uses, settings ------------------------------------------
    def history(self, record_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT ts, action, actor, detail FROM history WHERE record_id=? ORDER BY seq", (record_id,)).fetchall()
        return [{"ts": r["ts"], "action": r["action"], "actor": r["actor"], "detail": json.loads(r["detail"] or "{}")} for r in rows]

    def log_use(self, record_ids: list[str], action: str, detail: dict | None = None) -> None:
        if not record_ids:
            return
        with self._lock:
            self._db.executemany("INSERT INTO uses (ts, record_id, action, detail) VALUES (?,?,?,?)",
                                 [(time.time(), rid, action, json.dumps(detail or {}, default=str)) for rid in record_ids])
            self._db.commit(); self._harden()

    def uses(self, record_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT ts, action, detail FROM uses WHERE record_id=? ORDER BY seq DESC LIMIT ?", (record_id, limit)).fetchall()
        return [{"ts": r["ts"], "action": r["action"], "detail": json.loads(r["detail"] or "{}")} for r in rows]

    def capture_on(self, container: str) -> bool:
        with self._lock:
            r = self._db.execute("SELECT capture_on FROM settings WHERE container=?", (container,)).fetchone()
        return True if r is None else bool(r["capture_on"])          # default ON (owner direction)

    def set_capture(self, container: str, on: bool) -> None:
        if container not in CONTAINERS:
            raise ValueError("unknown memory container")
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (container, int(on))); self._db.commit(); self._harden()

    def all_records(self) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._db.execute("SELECT * FROM records ORDER BY first_seen_at").fetchall()]

    # ---- backup / restore ------------------------------------------------------
    def backup(self) -> dict:
        bdir = ensure_private_dir(self.dir / "backups")
        name = f"memory-{time.strftime('%Y%m%dT%H%M%S')}-r{self.revision()['revision']}.sqlite"
        dest = bdir / name
        with self._lock:
            with sqlite3.connect(str(dest)) as out:
                self._db.backup(out)
        os.chmod(dest, 0o600)
        for old in sorted(bdir.glob("memory-*.sqlite"))[:-BACKUPS_KEEP]:
            old.unlink()
        return {"name": name, "bytes": dest.stat().st_size}

    def backups(self) -> list[dict]:
        bdir = self.dir / "backups"
        return [{"name": p.name, "bytes": p.stat().st_size, "mtime": p.stat().st_mtime} for p in sorted(bdir.glob("memory-*.sqlite"))] if bdir.exists() else []

    def restore(self, name: str) -> dict:
        src = self.dir / "backups" / Path(name).name
        check_private_file(src, max_bytes=1 << 30)
        with self._lock:
            self._db.close()
            tmp = self.path.with_suffix(".restore")
            shutil.copyfile(src, tmp); os.chmod(tmp, 0o600); os.replace(tmp, self.path)
            self._db = sqlite3.connect(str(self.path), check_same_thread=False); self._db.row_factory = sqlite3.Row
            self._harden(); self._bump("", "restored", "owner", {"from": src.name}); self._db.commit()
        return self.revision()


def fts_query(q: str) -> str:
    """Safe FTS5 query: significant words OR-ed with prefix matching; punctuation and operators are never passed through."""
    words = [w for w in "".join(ch if ch.isalnum() else " " for ch in q.lower()).split() if len(w) > 2 and w not in STOP]
    return " OR ".join(f'"{w}"*' for w in words[:12]) or '""'


STOP = {"the", "and", "for", "are", "was", "you", "with", "that", "this", "what", "who", "how", "does", "did", "about", "tell", "have",
        "has", "her", "his", "she", "they", "them", "our", "your", "can", "could", "would", "should", "please", "any", "from", "into", "me"}
