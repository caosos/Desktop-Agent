"""ETA-driven checks, owned by the existing local watchdog (owner operating addendum, 2026-10-11).

A coordinator's ACK / WORKING / BLOCKED comment that carries the receipt schema (`checks_receipt.py`) ARMS a check for
its instruction item at the receipt's exact `next_check_at`. The watchdog tick calls `tick()`; when a check is due and no
newer receipt has replaced it, the watchdog inspects current evidence without any model (item status, the coordinator's
session in the registry, this service's pid/start and loaded revision, the repository HEAD) and publishes EXACTLY ONE of
WORKING / DONE / BLOCKED on the item's issue, once per due time. A DONE receipt that arrives before the check is due
closes it at once (early DONE). A missed `next_check_at` is an exception recorded with provenance; it is never claimed
as "stopped" unless the registry proves the session absent, otherwise it reads APPEARS STALE — HOST STATE UNVERIFIED.
Who is notified is decided by `checks_receipt.notify_decision`. Dispatching the next item stays the supervisor's / the
coordinator's: the check only NAMES the next eligible queue row.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .checks_receipt import OUTCOMES, chicago, notify_decision, parse_receipt, validate
from .events import Actor, Event, EventType as ET, Provenance
from .receipts import VERIFIED, write_receipt

RECHECK_SEC = 900              # a re-armed check (WORKING / BLOCKED with no new receipt) is due again in 15 minutes
INDEX_KEY = "checks:index"
STALE = "APPEARS STALE — HOST STATE UNVERIFIED"


def _git_head(cwd: str | None) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip() or "UNKNOWN"
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


def next_eligible(checklist_text: str) -> str:
    """The first row of the authoritative execution queue whose state is READY or QUEUED (read-only; nothing is dispatched)."""
    for line in checklist_text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 8 and re.match(r"^(READY|QUEUED)\b", cells[4]):
            return f"{cells[0][:140]} (state {cells[4][:40]})"
    return "none READY or QUEUED in the queue table"


class Checks:
    def __init__(self, store, intake, deliverer=None, *, clock=time.time, repo_dir: str | None = None, checklist_path: Path | None = None):
        self.store, self.intake, self.deliverer, self.clock = store, intake, deliverer, clock
        self.repo_dir = repo_dir or str(Path(__file__).resolve().parents[2])
        self.checklist_path = checklist_path or Path(self.repo_dir) / "docs" / "BUILD_CHECKLIST.md"
        self.started_at = clock()
        self.loaded_head = _git_head(self.repo_dir)          # the revision this process was started from

    # ---- state ------------------------------------------------------------------------------------------------------
    def _ids(self) -> list[str]:
        return list((self.store.get_kv(INDEX_KEY) or {}).get("ids") or [])

    def get(self, item_id: str) -> dict | None:
        return self.store.get_kv("check:" + item_id)

    def list(self) -> list[dict]:
        return [c for c in (self.get(i) for i in self._ids()) if c]

    def _save(self, rec: dict) -> None:
        self.store.set_kv("check:" + rec["item_id"], rec)
        ids = self._ids()
        if rec["item_id"] not in ids:
            self.store.set_kv(INDEX_KEY, {"ids": ids + [rec["item_id"]]})

    def _event(self, item: dict, payload: dict, evidence: list) -> None:
        self.store.append_event(Event(type=ET.CONTROL.value, task_id=None, payload={"project": item.get("project"), "item_id": item["item_id"], **payload},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source="watchdog:checks", evidence=evidence)))

    def _receipt(self, item: dict, claim: str, evidence: list) -> None:
        write_receipt(self.store, subject_type="check", subject_id=item["item_id"], claim=claim, actor=Actor.CONTROL.value, source="watchdog:checks",
                      result_label=VERIFIED, evidence=evidence, after_state={"item_status": item.get("status")})

    # ---- arming: from the intake read-back of a coordinator receipt --------------------------------------------------
    def observe(self, item: dict, status: str, text: str, created_iso: str, url: str) -> dict | None:
        now = self.clock()
        try:
            created = datetime.strptime(created_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            created = now
        cur = self.get(item["item_id"])
        if status == "DONE":
            return self._close_early(item, cur, url, now)
        if status not in ("ACKNOWLEDGED", "WORKING", "BLOCKED"):
            return None
        if cur and cur.get("receipt_ts", 0) >= created:
            return cur                                        # the same (or an older) receipt seen again: nothing changes
        problems, norm = validate(parse_receipt(text), created)
        if problems:
            self._event(item, {"action": "receipt_schema_invalid", "status": status, "problems": problems}, [url, *problems[:4]])
            self._receipt(item, f"{status} receipt rejected for the ETA schema: {'; '.join(problems)[:300]}", [url])
            return None
        rec = {"item_id": item["item_id"], "project": item["project"], "fields": norm, "armed_at": now, "receipt_ts": created, "receipt_url": url,
               "status_at_arm": status, "state": "ARMED", "fired": cur.get("fired", []) if cur else [], "outcomes": cur.get("outcomes", []) if cur else []}
        self._save(rec)
        self.store.set_kv("alert:" + item["item_id"], {"cleared": True, "at": now})   # a fresh valid receipt clears a standing alert
        self._event(item, {"action": "check_armed", "next_check_at": norm["next_check_chicago"], "eta": norm["eta_chicago"]}, [url])
        self._receipt(item, f"check armed for {norm['next_check_chicago']} (ETA {norm['eta_chicago']}; basis: {norm['eta_basis'][:120]})", [url])
        return rec

    def _close_early(self, item: dict, cur: dict | None, url: str, now: float) -> dict | None:
        if not cur or cur.get("state") != "ARMED":
            return cur
        early = now < cur["fields"]["next_check_ts"]
        nxt = next_eligible(self.checklist_path.read_text()) if self.checklist_path.exists() else "UNKNOWN (checklist not readable)"
        cur.update(state="CLOSED", closed_at=now, closed_reason="DONE received early" if early else "DONE received",
                   outcomes=cur.get("outcomes", []) + [{"at": now, "outcome": "DONE", "early": early, "next_eligible": nxt}])
        self._save(cur)
        self.store.set_kv("alert:" + item["item_id"], {"cleared": True, "at": now})
        notify, why = notify_decision("done_no_owner_acceptance")
        self._event(item, {"action": "check_closed", "early": early, "next_eligible": nxt, "notify": notify, "notify_reason": why}, [url])
        self._receipt(item, f"check closed by DONE ({'before' if early else 'at/after'} next_check_at); next eligible queue row: {nxt}", [url])
        return cur

    # ---- the due pass, called from the watchdog tick ------------------------------------------------------------------
    async def tick(self) -> list[dict]:
        out = []
        from .checks_reconcile import reconcile
        reconcile(self)                                    # retire obsolete timers first (DONE, superseded, owner-wait), so none of them fires
        now = self.clock()
        for item_id in self._ids():
            rec = self.get(item_id)
            if not rec or rec.get("state") != "ARMED":
                continue
            due = rec["fields"]["next_check_ts"]
            if now < due or any(f["due_ts"] == due for f in rec.get("fired", [])):
                continue
            out.append(await self._fire(rec, due, now))
        return out

    def _host_evidence(self, item: dict) -> tuple[dict, str | None]:
        """What the host says right now (registry, this process, repository). `session` is None when unobservable."""
        src = self.intake.source_for(item["project"])
        sess, note = (None, "no resolver")
        if src is not None and self.deliverer is not None and hasattr(self.deliverer, "resolve_for"):
            try:
                sess, note = self.deliverer.resolve_for(src)
            except Exception as exc:  # noqa: BLE001
                sess, note = None, f"registry unreadable: {exc}"[:120]
        head = _git_head(self.repo_dir)
        return ({"session": (sess or {}).get("name"), "session_status": (sess or {}).get("status"), "session_note": note,
                 "service_pid": os.getpid(), "service_started": chicago(self.started_at), "loaded_head": self.loaded_head, "repo_head": head,
                 "tested_vs_loaded": "same revision" if head == self.loaded_head else f"repo {head} is NOT loaded (process runs {self.loaded_head})"},
                "ABSENT" if (sess is None and "no eligible session" in (note or "")) else ("LIVE" if sess else None))

    async def _fire(self, rec: dict, due: float, now: float) -> dict:
        item = self.store.get_intake(rec["item_id"]) or {"item_id": rec["item_id"], "project": rec["project"], "status": "UNKNOWN", "repo": "", "issue": 0}
        f = rec["fields"]
        ev, registry = self._host_evidence(item)
        status = item.get("status")
        exception = None
        if status == "DONE":
            outcome, kind, state = "DONE", "done_no_owner_acceptance", "CLOSED"
            blocker = "NONE"
        elif status == "BLOCKED" or registry == "ABSENT":
            outcome, kind, state = "BLOCKED", "need_blocked", "ARMED"
            blocker = f["blocker"] if status == "BLOCKED" and f["blocker"].upper() != "NONE" else \
                f"coordinator session not found in the registry ({ev['session_note'][:120]}); NEED_HOST_ACTION: start it in /home/caoscare-1 and re-pin"
            exception = {"kind": "missed_check", "text": "no receipt by next_check_at" + (" and the registry proves the coordinator session absent" if registry == "ABSENT" else "")}
        else:
            outcome, kind, state = "WORKING", "missed_check", "ARMED"
            blocker = f["blocker"]
            exception = {"kind": "missed_check", "text": f"no receipt from the coordinator by next_check_at; " +
                         (f"session {ev['session']} {ev['session_status']} per the registry; progress unverified" if registry == "LIVE" else STALE)}
        if outcome not in OUTCOMES:
            raise ValueError(outcome)
        notify, why = notify_decision(kind if exception or outcome != "WORKING" else "routine_progress")
        revised_eta = chicago(f["eta_ts"]) if f["eta_ts"] > now else "UNKNOWN (stated ETA has passed; the coordinator has not revised it)"
        next_due = None if state == "CLOSED" else now + RECHECK_SEC
        nxt = next_eligible(self.checklist_path.read_text()) if outcome == "DONE" and self.checklist_path.exists() else None
        body = self._body(rec, item, outcome, due, now, ev, exception, blocker, revised_eta, next_due, nxt, notify, why)
        published = await self._publish(item, body)
        entry = {"due_ts": due, "fired_at": now, "outcome": outcome, "exception": exception, "notify": notify, "published": published}
        rec["fired"] = rec.get("fired", []) + [entry]
        rec["outcomes"] = rec.get("outcomes", []) + [{"at": now, "outcome": outcome, "notify": notify}]
        rec["state"] = state
        if next_due:
            rec["fields"] = {**f, "next_check_ts": next_due, "next_check_chicago": chicago(next_due)}
        self._save(rec)
        if notify:
            self.store.set_kv("alert:" + item["item_id"], {"kind": kind, "reason": why, "text": (exception or {}).get("text") or blocker, "at": now, "outcome": outcome})
        self._event(item, {"action": "check_fired", "outcome": outcome, "due": chicago(due), "exception": exception, "notify": notify, "notify_reason": why, "published": published},
                    [rec.get("receipt_url", ""), ev["session_note"][:100]])
        self._receipt(item, f"check due {chicago(due)} → {outcome}" + (f"; exception: {exception['text']}" if exception else ""), [rec.get("receipt_url", "")])
        return {"item_id": item["item_id"], "outcome": outcome, "exception": exception, "notify": notify, "published": published}

    def _body(self, rec, item, outcome, due, now, ev, exception, blocker, revised_eta, next_due, nxt, notify, why) -> str:
        f = rec["fields"]
        lines = [f"<!-- da:watchdog -->", f"WATCHDOG CHECK — {outcome} — {item['item_id']}",
                 f"- Project / task: {f['project']} / {f['task']}", f"- Responsible: {f['responsible']}", f"- Stage at last receipt: {f['stage']}",
                 f"- Check due: {chicago(due)} · observed: {chicago(now)} (America/Chicago)",
                 f"- Evidence (host, no model): item status {item.get('status')} (last receipt {rec.get('receipt_url', '')}); coordinator session {ev['session'] or 'NOT FOUND'}"
                 f"{(' ' + ev['session_status']) if ev['session_status'] else ''}; service pid {ev['service_pid']} started {ev['service_started']} on revision {ev['loaded_head']}; "
                 f"repo HEAD {ev['repo_head']} ({ev['tested_vs_loaded']})"]
        if exception:
            lines.append(f"- EXCEPTION ({exception['kind']}): {exception['text']}")
        lines.append(f"- Acceptance still expected: {f['acceptance']}")
        if outcome == "DONE":
            lines.append(f"- Exact commit, tests, browser evidence and loaded revision are in the coordinator's DONE receipt; next eligible queue row: {nxt}")
        else:
            lines.append(f"- Revised ETA: {revised_eta}; new next_check_at: {chicago(next_due)}")
            lines.append(f"- Blocker / NEED: {blocker}")
        lines.append(f"- Notify Michael: {'YES' if notify else 'no'} — {why}")
        return "\n".join(lines)

    async def _publish(self, item: dict, body: str) -> bool:
        if not item.get("repo") or not self.intake.post_comments:
            return False
        try:
            from . import intake as im
            await im.gh_api(f"repos/{item['repo']}/issues/{item['issue']}/comments", method="POST", fields={"body": f"{body}\n\n{im._AUTOMATION_TAG}"})
            return True
        except Exception as exc:  # noqa: BLE001
            self._event(item, {"action": "check_comment_failed", "error": str(exc)[:200]}, [str(exc)[:100]])
            return False
