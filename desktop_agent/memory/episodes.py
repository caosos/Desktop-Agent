"""Episodic memory: every Ask Aria exchange is archived permanently in the private store (owner directive 2026-10-12
"Aria for life": one continuous conversation with gaps, never forgets). Each new message brings back the relevant past
exchanges and where the last conversation left off. Lives in the same 0600 private SQLite file as the facts; never in
`aria:history` (a 60-turn display window), events, receipts, logs or any worker."""
from __future__ import annotations

import json
import time

from .store import fts_query

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (turn_id TEXT PRIMARY KEY, ts REAL, owner_text TEXT, aria_text TEXT, kind TEXT, actions TEXT, memory_ids TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(turn_id UNINDEXED, owner_text, aria_text, tokenize='porter unicode61');
"""
GAP_NEW_CONVERSATION_SEC = 4 * 3600       # a pause longer than this starts a new "conversation" (an interval, not a new relationship)
RECALL_K = 4
RECALL_CHARS = 1800


class Episodes:
    def __init__(self, store):
        self.store = store
        with store._lock:
            store._db.executescript(SCHEMA); store._db.commit(); store._harden()

    def record(self, turn_id: str, owner_text: str, aria_text: str, kind: str = "", actions: list | None = None, memory_ids: list | None = None) -> None:
        with self.store._lock:
            self.store._db.execute("INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?)",
                                   (turn_id, time.time(), owner_text[:4000], aria_text[:4000], kind, json.dumps(actions or []), json.dumps(memory_ids or [])))
            self.store._db.execute("DELETE FROM episodes_fts WHERE turn_id=?", (turn_id,))
            self.store._db.execute("INSERT INTO episodes_fts (turn_id, owner_text, aria_text) VALUES (?,?,?)", (turn_id, owner_text[:4000], aria_text[:4000]))
            self.store._db.commit(); self.store._harden()

    def count(self) -> int:
        with self.store._lock:
            return int(self.store._db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def last(self, n: int = 3) -> list[dict]:
        with self.store._lock:
            rows = self.store._db.execute("SELECT * FROM episodes ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def search(self, q: str, limit: int = RECALL_K, before: float | None = None) -> list[dict]:
        if not q.strip():
            return []
        with self.store._lock:
            rows = self.store._db.execute("SELECT e.*, bm25(episodes_fts) AS rank FROM episodes_fts f JOIN episodes e ON e.turn_id=f.turn_id WHERE episodes_fts MATCH ? "
                                          "AND (? IS NULL OR e.ts < ?) ORDER BY rank LIMIT ?", (fts_query(q), before, before, limit)).fetchall()
        return [dict(r) for r in rows]

    def delete(self, turn_id: str) -> bool:
        with self.store._lock:
            n = self.store._db.execute("DELETE FROM episodes WHERE turn_id=?", (turn_id,)).rowcount
            self.store._db.execute("DELETE FROM episodes_fts WHERE turn_id=?", (turn_id,)); self.store._db.commit()
        return n > 0

    def recall(self, question: str, now: float | None = None) -> str:
        """The 'where we left off' line plus the most relevant earlier exchanges, bounded. Empty when nothing is archived."""
        now = now or time.time()
        last = self.last(2)
        if not last:
            return ""
        gap = now - last[0]["ts"]
        parts = []
        if gap > GAP_NEW_CONVERSATION_SEC:
            parts.append(f"WHERE WE LEFT OFF ({_ago(gap)} ago, {time.strftime('%a %Y-%m-%d %H:%M', time.localtime(last[0]['ts']))}): "
                         f"Michael said \"{last[0]['owner_text'][:220]}\" and you answered \"{last[0]['aria_text'][:220]}\". Carry on from there; do not restart the relationship.")
        hits = [h for h in self.search(question, before=now - 60) if h["turn_id"] != last[0]["turn_id"]]
        size, lines = 0, []
        for h in hits:
            ln = f"- ({_ago(now - h['ts'])} ago) Michael: \"{h['owner_text'][:200]}\" → you: \"{h['aria_text'][:200]}\""
            if size + len(ln) > RECALL_CHARS:
                break
            lines.append(ln); size += len(ln)
        if lines:
            parts.append("EARLIER EXCHANGES RELEVANT NOW:\n" + "\n".join(lines))
        return "\n\n".join(parts)


def _ago(sec: float) -> str:
    sec = max(0.0, sec)
    if sec < 3600:
        return f"{int(sec // 60)} min"
    if sec < 86400:
        return f"{sec / 3600:.0f} h"
    return f"{sec / 86400:.0f} days"
