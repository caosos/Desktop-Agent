"""Owner-instruction intake: GitHub → control plane → project coordinator, with receipts.

Checks are free (GitHub REST through `gh api`, bounded interval, per-source cursor); no model
is consulted to check. Items are idempotent by GitHub id and survive restarts in the store.
Statuses: POSTED → RECEIVED → DELIVERED → ACKNOWLEDGED → WORKING → BLOCKED → DONE.
Coordinators acknowledge on GitHub with a comment containing `ACK <item_id>` (or
WORKING/BLOCKED/DONE <item_id>); the poller reads those back, so GitHub stays the shared,
visible record and nothing is conflated: "posted" is not "received" is not "acknowledged".
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from .events import Actor, Event, EventType as ET, Provenance
from .receipts import FAILED, VERIFIED, write_receipt
from .store import Store

STATUSES = ["POSTED", "RECEIVED", "DELIVERED", "ACKNOWLEDGED", "WORKING", "BLOCKED", "DONE"]
_ACK_RE = re.compile(r"\b(ACK|ACKNOWLEDGED|WORKING|BLOCKED|DONE|VERIFIED)\b[^\n]{0,40}?\b(da-[0-9a-f]{6,12})\b", re.I)
_AUTOMATION_TAG = "<!-- desktop-agent-intake -->"
_COORD_TAG_RE = re.compile(r"<!--\s*[\w-]+:coordinator\s*-->", re.I)      # e.g. <!-- caos:coordinator -->
_COORD_PREFIX_RE = re.compile(r"^\s*(ACK|Coordinator report|WORKING|BLOCKED|DONE|VERIFIED)\b", re.I)


@dataclass
class IntakeSource:
    project: str
    repo: str                          # owner/name
    issues: list[int]
    owner_logins: list[str] = field(default_factory=lambda: ["caosos"])
    coordinator: dict = field(default_factory=lambda: {"kind": "control_plane"})

    @property
    def key(self) -> str:
        return f"{self.repo}#{','.join(map(str, self.issues))}"


def item_id_for(repo: str, issue: int, kind: str, gh_id: int) -> str:
    return "da-" + hashlib.sha1(f"{repo}#{issue}:{kind}:{gh_id}".encode()).hexdigest()[:10]


async def gh_api(path: str, timeout: int = 60, method: str = "GET", fields: dict | None = None) -> list | dict:
    argv = ["gh", "api", path]
    if method != "GET":
        argv += ["-X", method]
    for k, v in (fields or {}).items():
        argv += ["-f", f"{k}={v}"]
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {path}: {err.decode(errors='replace').strip()[:200]}")
    return json.loads(out.decode() or "null")


class Intake:
    def __init__(self, store: Store, sources: list[IntakeSource], deliver, poll_sec: int = 120,
                 unacked_after_sec: int = 1800, post_comments: bool = True):
        self.store, self.sources, self.deliver = store, sources, deliver
        self.poll_sec, self.unacked_after_sec, self.post_comments = poll_sec, unacked_after_sec, post_comments
        self.last_poll_at: float | None = None
        self.last_error: str | None = None

    # ---- events / receipts ---------------------------------------------------
    def _emit(self, item: dict, etype: str, payload: dict, evidence: list, source: str = "intake") -> None:
        self.store.append_event(Event(type=etype, task_id=None, payload={"item_id": item["item_id"], **payload},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source=source, evidence=evidence)))

    def _set(self, item: dict, status: str, note: str, evidence: list, label: str = VERIFIED) -> None:
        self.store.set_intake_status(item["item_id"], status, note)
        item["status"] = status
        self._emit(item, ET.CONTROL.value, {"action": "intake_status", "status": status, "note": note[:200]}, evidence)
        write_receipt(self.store, subject_type="instruction", subject_id=item["item_id"], claim=f"{status}: {note[:160]}",
                      actor=Actor.CONTROL.value, source="intake", result_label=label, evidence=evidence,
                      after_state={"status": status, "url": item.get("url")})

    # ---- polling ----------------------------------------------------------------
    async def poll_once(self) -> dict:
        found, delivered, acked = 0, 0, 0
        for src in self.sources:
            for issue in src.issues:
                try:
                    f, d, a = await self._poll_issue(src, issue)
                    found += f; delivered += d; acked += a
                    self.last_error = None
                except Exception as exc:          # a GitHub hiccup must not stop the loop
                    self.last_error = f"{src.repo}#{issue}: {exc}"[:300]
        self.last_poll_at = time.time()
        self._flag_unacked()
        return {"found": found, "delivered": delivered, "acked": acked, "error": self.last_error}

    async def _poll_issue(self, src: IntakeSource, issue: int) -> tuple[int, int, int]:
        cursor = self.store.intake_cursor(src.repo, issue)
        found = delivered = acked = 0
        first_items: list[dict] = []
        if cursor is None:                       # first sight: the issue body itself is an instruction
            body = await gh_api(f"repos/{src.repo}/issues/{issue}")
            if body.get("user", {}).get("login") in src.owner_logins:
                item = self._ingest(src, issue, "issue", int(body["id"]), body.get("user", {}).get("login", ""),
                                    body.get("body") or "", body.get("created_at", ""), body.get("html_url", ""),
                                    body.get("title", ""))
                if item:
                    found += 1
                    first_items.append(item)
            cursor = body.get("created_at") or "1970-01-01T00:00:00Z"
        comments = await gh_api(f"repos/{src.repo}/issues/{issue}/comments?since={cursor}&per_page=100")
        newest = cursor
        new_items: list[dict] = list(first_items)
        for c in comments or []:
            created = c.get("created_at", "")
            newest = max(newest, created)
            login = c.get("user", {}).get("login", "")
            text = c.get("body") or ""
            if _AUTOMATION_TAG in text:
                continue                          # our own automatic comments
            is_coord = bool(_COORD_TAG_RE.search(text)) or bool(_COORD_PREFIX_RE.match(text))
            for m in _ACK_RE.finditer(text):     # a coordinator reporting status on one or more items
                item = self.store.get_intake(m.group(2).lower())
                if item:
                    status = {"ACK": "ACKNOWLEDGED", "ACKNOWLEDGED": "ACKNOWLEDGED", "WORKING": "WORKING", "BLOCKED": "BLOCKED",
                              "DONE": "DONE", "VERIFIED": "DONE"}[m.group(1).upper()]
                    if STATUSES.index(status) > STATUSES.index(item["status"]) or status == "BLOCKED":
                        self._set(item, status, f"coordinator comment {c.get('html_url','')}", [c.get("html_url", ""), login])
                        self.store.set_intake_activity(item["item_id"], created)
                        acked += 1
                    is_coord = True
            if is_coord or login not in src.owner_logins:
                self.store.touch_coordinator_activity(src.project, created)
                continue
            item = self._ingest(src, issue, "comment", int(c["id"]), login, text, created, c.get("html_url", ""), "")
            if item:
                found += 1
                new_items.append(item)
        if len(new_items) == 1:
            delivered += await self._deliver(src, new_items[0])
        elif new_items:
            delivered += await self._deliver_batch(src, new_items)
        if newest != cursor:
            self.store.set_intake_cursor(src.repo, issue, newest)
        return found, delivered, acked

    async def _deliver_batch(self, src: IntakeSource, items: list[dict]) -> int:
        """Several new items at once (typically the backlog on first sight): one delivery, one comment."""
        batch = getattr(self.deliver, "batch", None)
        if batch is None:
            return sum([await self._deliver(src, it) for it in items])
        try:
            result = await batch(src, items)
        except Exception as exc:
            for it in items:
                self._set(it, "RECEIVED", f"delivery failed: {exc}"[:200], [str(exc)[:200]], label=FAILED)
            return 0
        ok = bool(result.get("ok"))
        for it in items:
            self._set(it, result.get("status", "DELIVERED") if ok else "RECEIVED", result.get("note", ""), result.get("evidence", []),
                      label=VERIFIED if ok else FAILED)
        if ok and self.post_comments and result.get("comment"):
            try:
                await gh_api(f"repos/{src.repo}/issues/{items[0]['issue']}/comments", method="POST",
                             fields={"body": f"{result['comment']}\n\n{_AUTOMATION_TAG}"})
            except Exception as exc:
                self._emit(items[0], ET.CONTROL.value, {"action": "intake_comment_failed", "error": str(exc)[:200]}, [str(exc)[:100]])
        return len(items) if ok else 0

    def _ingest(self, src: IntakeSource, issue: int, kind: str, gh_id: int, login: str, text: str,
                created: str, url: str, title: str) -> dict | None:
        item_id = item_id_for(src.repo, issue, kind, gh_id)
        if self.store.get_intake(item_id):
            return None                           # idempotent: already seen (replay after restart)
        item = {"item_id": item_id, "project": src.project, "repo": src.repo, "issue": issue, "kind": kind, "gh_id": gh_id,
                "author": login, "title": title or text.strip().splitlines()[0][:120] if text.strip() else title,
                "body": text, "url": url, "posted_at": created, "status": "POSTED", "coordinator": src.coordinator.get("kind")}
        self.store.save_intake(item)
        self._set(item, "RECEIVED", f"ingested from {url}", [url, f"author={login}", f"sha256={hashlib.sha256(text.encode()).hexdigest()[:12]}"])
        return item

    async def _deliver(self, src: IntakeSource, item: dict) -> int:
        try:
            result = await self.deliver(src, item)
        except Exception as exc:
            self._set(item, "RECEIVED", f"delivery failed: {exc}"[:200], [str(exc)[:200]], label=FAILED)
            return 0
        if result.get("ok"):
            self._set(item, result.get("status", "DELIVERED"), result.get("note", "delivered"), result.get("evidence", ["delivered"]))
            if self.post_comments and result.get("comment"):
                try:
                    await gh_api(f"repos/{src.repo}/issues/{item['issue']}/comments", method="POST",
                                 fields={"body": f"{result['comment']}\n\n{_AUTOMATION_TAG}"})
                except Exception as exc:
                    self._emit(item, ET.CONTROL.value, {"action": "intake_comment_failed", "error": str(exc)[:200]}, [str(exc)[:100]])
            return 1
        self._set(item, "RECEIVED", result.get("note", "delivery not possible"), result.get("evidence", []), label=FAILED)
        return 0

    def _flag_unacked(self) -> None:
        now = time.time()
        for item in self.store.list_intake():
            if item["status"] in ("RECEIVED", "DELIVERED") and now - item["updated_at"] > self.unacked_after_sec and not item.get("flagged"):
                self.store.flag_intake(item["item_id"])
                self._emit(item, ET.BLOCKED.value, {"reason": f"instruction unacknowledged for {int((now - item['updated_at']) / 60)} min",
                                                     "project": item["project"], "url": item["url"]}, [item["url"]])

    async def run_forever(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.poll_sec)

    def summary(self) -> dict:
        items = self.store.list_intake()
        return {"last_poll_at": self.last_poll_at, "poll_sec": self.poll_sec, "last_error": self.last_error,
                "counts": {s: sum(1 for i in items if i["status"] == s) for s in STATUSES},
                "unacknowledged": [i["item_id"] for i in items if i["status"] in ("RECEIVED", "DELIVERED") and i.get("flagged")],
                "items": [{k: i.get(k) for k in ("item_id", "project", "repo", "issue", "kind", "author", "title", "url", "posted_at",
                                                  "status", "note", "updated_at", "last_activity_at", "flagged")} for i in items[-30:]],
                "coordinator_activity": self.store.coordinator_activity()}
