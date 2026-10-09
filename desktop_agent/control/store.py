"""Durable runtime state: SQLite index + append-only JSONL per task.

The store holds platform runtime state only (tasks, events, receipts,
goals, decisions, costs). Project truth stays in each project's repo.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .events import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
  goal_id TEXT PRIMARY KEY, project TEXT, text TEXT, created_at REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, goal_id TEXT, project TEXT, status TEXT,
  contract_json TEXT, contract_hash TEXT, created_at REAL, updated_at REAL,
  result_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, ts REAL,
  task_id TEXT, worker_id TEXT, type TEXT, json TEXT
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id TEXT PRIMARY KEY, ts REAL, subject_type TEXT, subject_id TEXT,
  result_label TEXT, correlation_id TEXT, parent_receipt_id TEXT, json TEXT
);
CREATE INDEX IF NOT EXISTS receipts_subject ON receipts(subject_type, subject_id, ts);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, task_id TEXT, question TEXT, options_json TEXT,
  answer TEXT, asked_at REAL, answered_at REAL
);
CREATE TABLE IF NOT EXISTS costs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, worker_id TEXT,
  model TEXT, usd REAL, input_tokens INTEGER, output_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY, ts REAL, response_json TEXT
);
CREATE TABLE IF NOT EXISTS plans (
  goal_id TEXT PRIMARY KEY, status TEXT, plan_json TEXT, answers_json TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS intake_items (
  item_id TEXT PRIMARY KEY, project TEXT, repo TEXT, issue INTEGER, kind TEXT, gh_id INTEGER, author TEXT,
  title TEXT, body TEXT, url TEXT, posted_at TEXT, status TEXT, note TEXT, coordinator TEXT,
  created_at REAL, updated_at REAL, last_activity_at TEXT, flagged INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS intake_cursors (
  source TEXT PRIMARY KEY, cursor TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS coordinator_activity (
  project TEXT PRIMARY KEY, last_seen TEXT, updated_at REAL
);
"""
_MIGRATIONS = [
    "ALTER TABLE decisions ADD COLUMN goal_id TEXT",
    "ALTER TABLE coordinator_activity ADD COLUMN last_check REAL",
    "ALTER TABLE coordinator_activity ADD COLUMN last_wake REAL",
    "ALTER TABLE coordinator_activity ADD COLUMN last_wake_kind TEXT",
    "ALTER TABLE coordinator_activity ADD COLUMN last_ack REAL",
    "ALTER TABLE coordinator_activity ADD COLUMN check_json TEXT",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, json TEXT, updated_at REAL)",
    "ALTER TABLE decisions ADD COLUMN project TEXT",
    "ALTER TABLE decisions ADD COLUMN scope TEXT",
    "ALTER TABLE decisions ADD COLUMN resumes TEXT",
    "ALTER TABLE decisions ADD COLUMN recommendation TEXT",
    "ALTER TABLE decisions ADD COLUMN deferred_until REAL",
]


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tasks").mkdir(exist_ok=True)
        self._db = sqlite3.connect(self.data_dir / "control.sqlite", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self._db.execute(stmt)
            except sqlite3.OperationalError:
                pass                     # column already present
        self._db.commit()
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Event], None]] = []

    # ---- events -----------------------------------------------------------
    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._listeners.append(fn)

    def append_event(self, ev: Event) -> Event:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events(id, ts, task_id, worker_id, type, json) VALUES (?,?,?,?,?,?)",
                (ev.id, ev.ts, ev.task_id, ev.worker_id, ev.type, ev.to_json()),
            )
            ev.seq = cur.lastrowid
            self._db.execute("UPDATE events SET json=? WHERE seq=?", (ev.to_json(), ev.seq))
            self._db.commit()
            if ev.task_id:
                tdir = self.data_dir / "tasks" / ev.task_id
                tdir.mkdir(parents=True, exist_ok=True)
                with open(tdir / "events.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(ev.to_json() + "\n")
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # a bad listener must not lose the event
                pass
        return ev

    def events(self, task_id: str | None = None, since_seq: int = 0, limit: int = 5000) -> list[Event]:
        q = "SELECT json FROM events WHERE seq > ?"
        args: list[Any] = [since_seq]
        if task_id:
            q += " AND task_id = ?"
            args.append(task_id)
        q += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [Event.from_dict(json.loads(r["json"])) for r in rows]

    def events_by_task(self, task_ids: Iterable[str]) -> dict[str, list[Event]]:
        out: dict[str, list[Event]] = {}
        for tid in task_ids:
            out[tid] = self.events(tid)
        return out

    def last_seq(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT MAX(seq) AS m FROM events").fetchone()
        return int(row["m"] or 0)

    # ---- goals / tasks ----------------------------------------------------
    def save_goal(self, goal_id: str, project: str, text: str, source: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO goals VALUES (?,?,?,?,?)",
                (goal_id, project, text, time.time(), source),
            )
            self._db.commit()

    def save_task(self, task_id: str, goal_id: str | None, project: str, status: str,
                  contract: dict, contract_hash: str, result: dict | None = None) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO tasks(task_id, goal_id, project, status, contract_json, contract_hash, created_at, updated_at, result_json)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,"
                " contract_json=excluded.contract_json, contract_hash=excluded.contract_hash,"
                " updated_at=excluded.updated_at, result_json=COALESCE(excluded.result_json, tasks.result_json)",
                (task_id, goal_id, project, status, json.dumps(contract), contract_hash, now, now,
                 json.dumps(result) if result is not None else None),
            )
            self._db.commit()
        (self.data_dir / "tasks" / task_id).mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tasks" / task_id / "contract.json").write_text(json.dumps(contract, indent=2))

    def set_task_status(self, task_id: str, status: str, result: dict | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status=?, updated_at=?, result_json=COALESCE(?, result_json) WHERE task_id=?",
                (status, time.time(), json.dumps(result) if result is not None else None, task_id),
            )
            self._db.commit()

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task_row(row) if row else None

    def list_tasks(self, status: str | None = None, project: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM tasks", []
        conds = []
        if status:
            conds.append("status=?"); args.append(status)
        if project:
            conds.append("project=?"); args.append(project)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [self._task_row(r) for r in rows]

    @staticmethod
    def _task_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["contract"] = json.loads(d.pop("contract_json") or "{}")
        d["result"] = json.loads(d.pop("result_json") or "null")
        return d

    # ---- receipts ---------------------------------------------------------
    def save_receipt(self, rec: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?)",
                (rec["receipt_id"], rec["ts"], rec["subject"]["type"], rec["subject"]["id"],
                 rec["result_label"], rec.get("correlation_id"), rec.get("parent_receipt_id"),
                 json.dumps(rec, default=str)),
            )
            self._db.commit()
        tid = rec["subject"]["id"] if rec["subject"]["type"] == "task" else rec.get("task_id")
        if tid:
            tdir = self.data_dir / "tasks" / tid
            tdir.mkdir(parents=True, exist_ok=True)
            with open(tdir / "receipts.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")

    def receipts(self, subject_type: str | None = None, subject_id: str | None = None,
                 correlation_id: str | None = None) -> list[dict]:
        q, args, conds = "SELECT json FROM receipts", [], []
        if subject_type:
            conds.append("subject_type=?"); args.append(subject_type)
        if subject_id:
            conds.append("subject_id=?"); args.append(subject_id)
        if correlation_id:
            conds.append("correlation_id=?"); args.append(correlation_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [json.loads(r["json"]) for r in rows]

    def last_receipt_id(self, subject_type: str, subject_id: str) -> str | None:
        recs = self.receipts(subject_type, subject_id)
        return recs[-1]["receipt_id"] if recs else None

    # ---- plans ------------------------------------------------------------
    def save_plan(self, goal_id: str, status: str, plan: dict | None, answers: list[dict] | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO plans(goal_id, status, plan_json, answers_json, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(goal_id) DO UPDATE SET status=excluded.status, plan_json=COALESCE(excluded.plan_json, plans.plan_json),"
                " answers_json=COALESCE(excluded.answers_json, plans.answers_json), updated_at=excluded.updated_at",
                (goal_id, status, json.dumps(plan) if plan is not None else None,
                 json.dumps(answers) if answers is not None else None, time.time()),
            )
            self._db.commit()

    def get_plan(self, goal_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM plans WHERE goal_id=?", (goal_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["plan"] = json.loads(d.pop("plan_json") or "null")
        d["answers"] = json.loads(d.pop("answers_json") or "[]")
        return d

    def get_goal(self, goal_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
        return dict(row) if row else None

    # ---- decisions --------------------------------------------------------
    def save_decision(self, decision_id: str, task_id: str | None, question: str, options: list[str],
                      goal_id: str | None = None, project: str | None = None, scope: str | None = None,
                      resumes: str | None = None, recommendation: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO decisions(decision_id, task_id, question, options_json, answer, asked_at, answered_at, goal_id,"
                " project, scope, resumes, recommendation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, task_id, question, json.dumps(options), None, time.time(), None, goal_id, project, scope, resumes, recommendation),
            )
            self._db.commit()

    def defer_decision(self, decision_id: str, until: float) -> dict | None:
        """Hide a decision until `until`; it stays open and unanswered (deferral is never consent)."""
        with self._lock:
            self._db.execute("UPDATE decisions SET deferred_until=? WHERE decision_id=? AND answer IS NULL", (until, decision_id))
            self._db.commit()
            row = self._db.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        return dict(row) if row else None

    def answer_decision(self, decision_id: str, answer: str) -> dict | None:
        with self._lock:
            self._db.execute("UPDATE decisions SET answer=?, answered_at=? WHERE decision_id=?",
                             (answer, time.time(), decision_id))
            self._db.commit()
            row = self._db.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        return dict(row) if row else None

    def open_decisions(self, include_deferred: bool = False) -> list[dict]:
        q = "SELECT * FROM decisions WHERE answer IS NULL"
        if not include_deferred:
            q += " AND (deferred_until IS NULL OR deferred_until < ?)"
        with self._lock:
            rows = self._db.execute(q + " ORDER BY asked_at", () if include_deferred else (time.time(),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = json.loads(d.pop("options_json") or "[]")
            out.append(d)
        return out

    # ---- owner-instruction intake ------------------------------------------
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

    # ---- costs / idempotency ---------------------------------------------
    def add_cost(self, task_id: str | None, worker_id: str | None, model: str | None,
                 usd: float | None, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """usd=None records usage whose dollar cost is unknown (subscription or unpriced model);
        it is counted separately and never estimated."""
        with self._lock:
            self._db.execute(
                "INSERT INTO costs(ts, task_id, worker_id, model, usd, input_tokens, output_tokens) VALUES (?,?,?,?,?,?,?)",
                (time.time(), task_id, worker_id, model, usd, input_tokens, output_tokens),
            )
            self._db.commit()

    def cost_summary(self) -> dict:
        now = time.time()
        with self._lock:
            day = self._db.execute("SELECT COALESCE(SUM(usd),0) s FROM costs WHERE ts > ?", (now - 86400,)).fetchone()["s"]
            hour = self._db.execute("SELECT COALESCE(SUM(usd),0) s FROM costs WHERE ts > ?", (now - 3600,)).fetchone()["s"]
            total = self._db.execute("SELECT COALESCE(SUM(usd),0) s FROM costs").fetchone()["s"]
            per_task = self._db.execute(
                "SELECT task_id, SUM(usd) s FROM costs WHERE usd IS NOT NULL GROUP BY task_id ORDER BY MAX(ts) DESC LIMIT 20").fetchall()
            unknown = self._db.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,"
                " SUM(CASE WHEN ts > ? THEN 1 ELSE 0 END) n_today FROM costs WHERE usd IS NULL", (now - 86400,)).fetchone()
        return {"today_usd": round(day, 4), "last_hour_usd": round(hour, 4), "total_usd": round(total, 4),
                "per_task": {r["task_id"]: round(r["s"], 4) for r in per_task},
                "unknown_usage": {"calls": unknown["n"], "calls_today": unknown["n_today"] or 0,
                                  "input_tokens": unknown["i"], "output_tokens": unknown["o"]}}

    def idempotent(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT response_json FROM idempotency WHERE key=?", (key,)).fetchone()
        return json.loads(row["response_json"]) if row else None

    def remember(self, key: str, response: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO idempotency VALUES (?,?,?)",
                             (key, time.time(), json.dumps(response)))
            self._db.commit()
