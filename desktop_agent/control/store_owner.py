"""Store mixin: owner-facing records — plans (planner output per goal, answers) and decisions (ask, amend, defer,
answer, open list). Composed into `Store`; uses the shared connection and lock; SQL unchanged."""
from __future__ import annotations

import json
import time


class OwnerRecordsMixin:
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

    def amend_decision(self, decision_id: str, **fields) -> dict | None:
        """Correct the owner-facing text of an OPEN decision (question, options, recommendation, scope, resumes, why).
        Never touches answers; the caller records the receipt and the reason."""
        allowed = {"question", "options", "recommendation", "scope", "resumes", "why"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot amend {k}")
            sets.append(("options_json" if k == "options" else k) + "=?"); vals.append(json.dumps(v) if k == "options" else v)
        with self._lock:
            if sets:
                self._db.execute(f"UPDATE decisions SET {', '.join(sets)} WHERE decision_id=? AND answer IS NULL", (*vals, decision_id))
                self._db.commit()
            row = self._db.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        return dict(row) if row else None

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
