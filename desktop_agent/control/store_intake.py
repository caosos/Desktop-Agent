"""Store mixin: owner-instruction intake items and cursors, coordinator activity/check/wake/ack rows, and the kv
table used for coordinator state, gates and Aria history. Composed into `Store`; shared connection and lock; SQL
unchanged."""
from __future__ import annotations

import json
import time


class IntakeStoreMixin:
    def save_intake(self, item: dict) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO intake_items(item_id, project, repo, issue, kind, gh_id, author, title, body, url, posted_at,"
                " status, note, coordinator, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["item_id"], item["project"], item["repo"], item["issue"], item["kind"], item["gh_id"], item["author"],
                 item.get("title", ""), item.get("body", ""), item.get("url", ""), item.get("posted_at", ""),
                 item.get("status", "POSTED"), "", item.get("coordinator"), now, now))
            self._db.commit()

    def set_intake_status(self, item_id: str, status: str, note: str) -> None:
        with self._lock:
            self._db.execute("UPDATE intake_items SET status=?, note=?, updated_at=? WHERE item_id=?", (status, note[:500], time.time(), item_id))
            self._db.commit()

    def set_intake_github(self, item_id: str, gh_id: int, url: str, posted_at: str) -> None:
        with self._lock:
            self._db.execute("UPDATE intake_items SET gh_id=?, url=?, posted_at=? WHERE item_id=?", (gh_id, url, posted_at, item_id)); self._db.commit()

    def update_intake_body(self, item_id: str, body: str) -> None:
        with self._lock:
            self._db.execute("UPDATE intake_items SET body=?, title=?, updated_at=? WHERE item_id=?",
                             (body, body.strip().splitlines()[0][:120] if body.strip() else "", time.time(), item_id)); self._db.commit()

    def set_intake_activity(self, item_id: str, when: str) -> None:
        with self._lock:
            self._db.execute("UPDATE intake_items SET last_activity_at=? WHERE item_id=?", (when, item_id))
            self._db.commit()

    def flag_intake(self, item_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE intake_items SET flagged=1 WHERE item_id=?", (item_id,)); self._db.commit()

    def get_intake(self, item_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM intake_items WHERE item_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def list_intake(self, project: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM intake_items", []
        if project:
            q += " WHERE project=?"; args.append(project)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY created_at", args).fetchall()
        return [dict(r) for r in rows]

    def intake_cursor(self, repo: str, issue: int) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT cursor FROM intake_cursors WHERE source=?", (f"{repo}#{issue}",)).fetchone()
        return row["cursor"] if row else None

    def set_intake_cursor(self, repo: str, issue: int, cursor: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO intake_cursors VALUES (?,?,?)", (f"{repo}#{issue}", cursor, time.time())); self._db.commit()

    def touch_coordinator_activity(self, project: str, when: str) -> None:
        with self._lock:
            self._ensure_coord(project)
            row = self._db.execute("SELECT last_seen FROM coordinator_activity WHERE project=?", (project,)).fetchone()
            if not row or (row["last_seen"] or "") < when:
                self._db.execute("UPDATE coordinator_activity SET last_seen=?, updated_at=? WHERE project=?", (when, time.time(), project))
            self._db.commit()

    def coordinator_activity(self) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT project, last_seen FROM coordinator_activity").fetchall()
        return {r["project"]: r["last_seen"] for r in rows}

    def _ensure_coord(self, project: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO coordinator_activity(project, last_seen, updated_at) VALUES (?,?,?)", (project, None, time.time()))

    def save_coordinator_check(self, project: str, entry: dict) -> None:
        with self._lock:
            self._ensure_coord(project)
            self._db.execute("UPDATE coordinator_activity SET last_check=?, check_json=?, updated_at=? WHERE project=?",
                             (entry.get("checked_at", time.time()), json.dumps(entry, default=str)[:20000], time.time(), project)); self._db.commit()

    def save_coordinator_wake(self, project: str, when: float, kind: str) -> None:
        with self._lock:
            self._ensure_coord(project)
            self._db.execute("UPDATE coordinator_activity SET last_wake=?, last_wake_kind=?, updated_at=? WHERE project=?", (when, kind, time.time(), project)); self._db.commit()

    def save_coordinator_ack(self, project: str, when: float) -> None:
        with self._lock:
            self._ensure_coord(project)
            self._db.execute("UPDATE coordinator_activity SET last_ack=?, updated_at=? WHERE project=?", (when, time.time(), project)); self._db.commit()

    def coordinator_rows(self) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM coordinator_activity").fetchall()
        out = {}
        for r in rows:
            d = dict(r); d["check"] = json.loads(d.pop("check_json") or "null"); out[d["project"]] = d
        return out

    def set_kv(self, key: str, value: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value, default=str), time.time())); self._db.commit()

    def get_kv(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT json FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["json"]) if row else None
