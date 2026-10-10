"""Coordinator watchdog: cheap, non-LLM checks per project coordinator, and a bounded
Claude wake only when a probe says useful work needs intelligence.

Per project (config `intake.coordinator`):
  state_command : prints one JSON object describing the coordinator (its own script; stdlib only)
  probe_command : prints {"verdict": "WAKE"|"IDLE", "reasons": [...]}; IDLE costs nothing
  wake          : on WAKE, one peer message `DA-HEARTBEAT reasons=...` to the live session, at most
                  once per cooldown unless the reasons change; recorded with a receipt.
Liaison inboxes (Michael Business OS): `docs/messages/inbox/*.md` on a branch, acks as files on the
coordinator's branch; read through the free GitHub contents API; no session to wake across users,
so items wait for the coordinator's own sync and are shown as such.
Quota: the last Claude Code rate-limit window seen by a worker is kept; when the scheduler paused
itself on a provider limit, it resumes once the window has reset.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time

from . import allowance
from .ackfile import EXPLICIT_LIFT, PARSER_VERSION, parse_ack
from .events import Actor, Event, EventType as ET, Provenance
from .intake import gh_api, IntakeSource
from .receipts import FAILED, VERIFIED, write_receipt
from .store import Store

WAKE_COOLDOWN_SEC = 1800


async def run_json_command(cmd: str, cwd: str | None, timeout: int = 60) -> dict:
    """Run a coordinator's own stdlib script and parse its single JSON object. Never a model."""
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and not k.endswith("_TOKEN")}
    proc = await asyncio.create_subprocess_exec("/bin/sh", "-c", cmd, cwd=cwd, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd!r} exit {proc.returncode}: {err.decode(errors='replace')[:200]}")
    return json.loads(text[text.index("{"):])


class Watchdog:
    def __init__(self, store: Store, intake, deliverer, scheduler, cooldown_sec: int = WAKE_COOLDOWN_SEC):
        self.store, self.intake, self.deliverer, self.scheduler = store, intake, deliverer, scheduler
        self.cooldown_sec = cooldown_sec
        self.workers = None                                      # WorkerMonitor, set by the service (feeds + open-PR refresh)
        self.allowance_path = None                               # <data_dir>/allowance.json, set by the service
        self.allowance_project = "desktop_agent"                 # whose coordinator session is the registered allowance writer
        self._last_wake: dict[str, tuple[float, str]] = {}      # project → (ts, reasons key)

    def _emit(self, project: str, etype: str, payload: dict, evidence: list) -> None:
        self.store.append_event(Event(type=etype, task_id=None, payload={"project": project, **payload},
                                      provenance=Provenance(actor=Actor.CONTROL.value, source="watchdog", evidence=evidence)))

    # ---- one pass over all coordinators ------------------------------------------------
    async def tick(self) -> dict:
        report: dict = {}
        for src in self.intake.sources:
            coord = src.coordinator or {}
            entry: dict = {"checked_at": time.time()}
            try:
                if coord.get("state_command"):
                    entry["state"] = await run_json_command(coord["state_command"], coord.get("cwd"))
                if coord.get("probe_command"):
                    probe = await run_json_command(coord["probe_command"], coord.get("cwd"))
                    entry["probe"] = probe
                    if str(probe.get("verdict", "")).upper() == "WAKE":
                        entry["wake"] = await self._wake(src, probe.get("reasons") or [])
                entry["error"] = None
            except Exception as exc:
                entry["error"] = str(exc)[:300]
            self.store.save_coordinator_check(src.project, entry)
            report[src.project] = {k: v for k, v in entry.items() if k != "state"}
        if self.allowance_path is not None:
            writer_path = self.allowance_path.with_name(allowance.WRITER_FILE)
            own = next((s for s in self.intake.sources if s.project == self.allowance_project), None)
            if own is not None and hasattr(self.deliverer, "resolve_for"):
                sess, note = self.deliverer.resolve_for(own)
                allowance.register_writer(writer_path, sess, note)
            report["allowance"] = allowance.ingest(self.store, self.allowance_path, writer_path=writer_path)
        self._resume_after_quota_reset()
        if self.workers is not None:
            try:
                report["workers"] = await self.workers.refresh_remote()
            except Exception as exc:
                report["workers"] = {"error": str(exc)[:200]}
        if getattr(self, "drill", None) is not None:
            try:
                report["rosters"] = await self.drill.refresh_remote()
            except Exception as exc:
                report["rosters"] = {"error": str(exc)[:200]}
        return report

    async def _wake(self, src: IntakeSource, reasons: list) -> dict:
        key = hashlib.sha1(json.dumps(sorted(map(str, reasons))).encode()).hexdigest()[:8]
        last = self._last_wake.get(src.project)
        if last and time.time() - last[0] < self.cooldown_sec and last[1] == key:
            return {"sent": False, "note": "same reasons within cooldown"}
        text = (f"DA-HEARTBEAT reasons={json.dumps(reasons)[:800]}\n"
                f"Your heartbeat probe reported WAKE at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}. Sent once by the Desktop-Agent watchdog; "
                f"no reply to this message is needed; act through your normal receipts.")
        fake_item = {"item_id": f"hb-{key}", "project": src.project, "repo": src.repo, "issue": src.issues[0] if src.issues else 0,
                     "kind": "heartbeat", "author": "watchdog", "body": text, "url": "", "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        result = await self.deliverer._claude_peer(src, fake_item, backfill=False, prebuilt_text=text)
        self._last_wake[src.project] = (time.time(), key)
        label = VERIFIED if result.get("ok") else FAILED
        write_receipt(self.store, subject_type="coordinator", subject_id=src.project, claim=f"heartbeat wake: {', '.join(map(str, reasons))[:160]}",
                      actor=Actor.CONTROL.value, source="watchdog", result_label=label, evidence=result.get("evidence") or [result.get("note", "")])
        self._emit(src.project, ET.CONTROL.value, {"action": "heartbeat_wake", "ok": result.get("ok"), "reasons": reasons[:6], "note": result.get("note", "")[:200]},
                   result.get("evidence") or [])
        if result.get("ok"):
            self.store.save_coordinator_wake(src.project, time.time(), "heartbeat")
        return {"sent": bool(result.get("ok")), "note": result.get("note", "")[:200]}

    # ---- quota reset → resume ----------------------------------------------------------
    def _resume_after_quota_reset(self) -> None:
        if not self.scheduler.paused or not (self.scheduler.hold_reason or "").startswith("paused: provider limit"):
            return
        q = self.store.get_kv("quota") or {}
        resets = q.get("resets_at")
        if resets and time.time() > float(resets):
            self.scheduler.paused = False
            self.scheduler.hold_reason = None
            self._emit("desktop_agent", ET.CONTROL.value, {"action": "resume_after_quota_reset", "resets_at": resets}, [f"resets_at={resets}"])

    # ---- liaison inbox (file-based) -----------------------------------------------------
    async def poll_liaison(self, src: IntakeSource) -> tuple[int, int]:
        """New `docs/messages/inbox/*.md` on the liaison branch → items; ack files on the coordinator
        branch → ACKNOWLEDGED. Returns (new_items, newly_acked)."""
        li = src.coordinator.get("liaison") or {}
        branch, inbox, ack_branch, ack_dir = li["branch"], li["inbox_dir"], li["ack_branch"], li["ack_dir"]
        files = await gh_api(f"repos/{src.repo}/contents/{inbox}?ref={branch}")
        acks = {f["name"]: {"sha": f.get("sha", ""), "url": f.get("html_url", "")} for f in (await gh_api(f"repos/{src.repo}/contents/{ack_dir}?ref={ack_branch}") or [])}
        found = acked = 0
        for f in files or []:
            if not f.get("name", "").endswith(".md"):
                continue
            item_id = "da-" + hashlib.sha1(f"{src.repo}:liaison:{f['path']}".encode()).hexdigest()[:10]
            item = self.store.get_intake(item_id)
            sent = self.store.get_kv("liaison_sent:" + f["name"][:-3]) if not item else None
            if sent and self.store.get_intake(sent["item_id"]):
                item = self.store.get_intake(sent["item_id"]); item_id = item["item_id"]           # our own direction: one item, not two
                if item["status"] == "SENT":
                    self.intake._set(item, "RECEIVED", f"liaison file confirmed on {branch}: {f['path']}; awaiting the coordinator's own sync (no live session reachable)",
                                     [f.get("html_url", ""), f"sha={f.get('sha','')[:12]}"])
            if not item:
                content = await gh_api(f"repos/{src.repo}/contents/{f['path']}?ref={branch}")
                body = base64.b64decode(content.get("content") or "").decode(errors="replace") if content.get("encoding") == "base64" else ""
                m = re.search(r"\*\*Date:\*\*\s*([^\n]+)", body)
                item = {"item_id": item_id, "project": src.project, "repo": src.repo, "issue": 0, "kind": "liaison", "gh_id": 0,
                        "author": "aria", "title": (body.strip().splitlines()[0].lstrip("# ").strip() if body.strip() else f["name"])[:120],
                        "body": body, "url": f.get("html_url", ""), "posted_at": (m.group(1).strip() if m else ""), "status": "POSTED",
                        "coordinator": "liaison"}
                self.store.save_intake(item)
                self.intake._set(item, "RECEIVED", f"liaison message on {branch}: {f['path']}; awaiting the coordinator's own sync (no live session reachable)",
                                 [f.get("html_url", ""), f"sha={f.get('sha','')[:12]}"])
                found += 1
            if f["name"] in acks and item["status"] not in ("ACKNOWLEDGED", "WORKING", "DONE", "BLOCKED"):
                self.intake._set(item, "ACKNOWLEDGED", f"ack file {ack_dir}/{f['name']} on {ack_branch}", [f"{ack_branch}:{ack_dir}/{f['name']}"])
                self.store.set_intake_activity(item["item_id"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                self.store.save_coordinator_ack(src.project, time.time())
                acked += 1
            if f["name"] in acks:
                await self._track_ack_disposition(src, item, ack_branch, ack_dir, f["name"], acks[f["name"]])
        return found, acked

    async def _track_ack_disposition(self, src: IntakeSource, item: dict, ack_branch: str, ack_dir: str, name: str, meta: dict) -> dict | None:
        """SHA-aware: the ack file is read only when its content SHA differs from the last reading; each
        new reading is one receipt on the instruction (claim `ACKFILE <stage>`), the gates scan runs per
        SHA, and the item's status follows only the unambiguous dispositions (BLOCKED ↔ not blocked).
        COMPLETED never becomes DONE here: it is 'coordination-completed' on the row, with what the
        ack itself says about implementation and live state."""
        key = "liaison_ack:" + item["item_id"]
        cur = self.store.get_kv(key) or {}
        sha = meta.get("sha") or ""
        reparse = bool(cur) and cur.get("sha") == sha and cur.get("parser_version") != PARSER_VERSION
        if cur.get("sha") == sha and not reparse:
            return None
        try:
            content = await gh_api(f"repos/{src.repo}/contents/{ack_dir}/{name}?ref={ack_branch}")
            body = base64.b64decode(content.get("content") or "").decode(errors="replace") if content.get("encoding") == "base64" else ""
        except Exception as exc:
            return {"error": str(exc)[:120]}
        now = time.time()
        d = parse_ack(body)
        if reparse:
            # same content, newer parser: refresh the reading in place (links re-resolved, gates reconciled per SHA);
            # a receipt only if the parse now says something else
            rec = {**cur, **d, "parser_version": PARSER_VERSION, "reparsed_at": now,
                   "links_resolved": await self._resolve_links(src.repo, ack_branch, d.get("links") or [])}
            self.store.set_kv(key, rec)
            gates = self.store.get_kv("gates:" + src.project) or {"gates": [], "scanned": []}
            unversioned = any(g.get("source") == f"{ack_dir}/{name}" and not g.get("sha") for g in gates.get("gates") or [])
            if f"{name}@{sha}" not in gates.get("scanned", []) or unversioned:        # gates recorded before SHA tracking get reconciled once
                await self._scan_ack_for_gates(src, ack_branch, ack_dir, name, body=body, url=rec.get("url", ""), sha=sha, category=d["category"])
            if (cur.get("stage"), cur.get("category")) == (d["stage"], d["category"]):
                return rec
            evidence = [rec.get("url") or f"{ack_branch}:{ack_dir}/{name}", f"sha={sha[:12]}", f"stage={d['stage']}", f"parser_version={PARSER_VERSION}"]
            write_receipt(self.store, subject_type="instruction", subject_id=item["item_id"],
                          claim=f"ACKFILE {d['stage'] or 'UNRECOGNISED'}: {d['category']}; re-read with parser v{PARSER_VERSION} (same content)",
                          actor=Actor.CONTROL.value, source="watchdog:liaison-ack", result_label=VERIFIED, evidence=evidence,
                          before_state={"stage": cur.get("stage"), "category": cur.get("category")}, after_state={"stage": d["stage"], "category": d["category"]})
            self._apply_block_rule(item, d, rec, evidence, key)
            return rec
        hist = (cur.get("history") or [])[-5:] + [{"sha": sha[:12], "at": now, "stage": d["stage"], "category": d["category"]}]
        rec = {**d, "sha": sha, "url": content.get("html_url") or meta.get("url", ""), "file": f"{ack_dir}/{name}", "observed_at": now,
               "first_seen_at": cur.get("first_seen_at") or now, "history": hist, "parser_version": PARSER_VERSION,
               "links_resolved": await self._resolve_links(src.repo, ack_branch, d.get("links") or [])}
        self.store.set_kv(key, rec)
        evidence = [rec["url"] or f"{ack_branch}:{ack_dir}/{name}", f"sha={sha[:12]}", f"stage={d['stage']}", f"implementation={d['implementation']}", f"live={d['live']}"]
        write_receipt(self.store, subject_type="instruction", subject_id=item["item_id"],
                      claim=f"ACKFILE {d['stage'] or 'UNRECOGNISED'}: {d['category']}; {d['stage_text'][:120]}",
                      actor=Actor.CONTROL.value, source="watchdog:liaison-ack", result_label=VERIFIED, evidence=evidence,
                      before_state={"sha": (cur.get("sha") or "")[:12], "stage": cur.get("stage")}, after_state={"sha": sha[:12], "stage": d["stage"], "category": d["category"]})
        self._emit(src.project, ET.CONTROL.value, {"action": "ack_disposition", "item_id": item["item_id"], "stage": d["stage"], "category": d["category"],
                                                   "implementation": d["implementation"], "live": d["live"]}, evidence)
        self._apply_block_rule(item, d, rec, evidence, key)
        self.store.set_intake_activity(item["item_id"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))
        self.store.save_coordinator_ack(src.project, now)
        gates = self.store.get_kv("gates:" + src.project) or {"gates": [], "scanned": []}
        if f"{name}@{sha}" not in gates.get("scanned", []):
            await self._scan_ack_for_gates(src, ack_branch, ack_dir, name, body=body, url=rec["url"], sha=sha, category=d["category"])
        return rec

    def _apply_block_rule(self, item: dict, d: dict, rec: dict, evidence: list, key: str) -> None:
        """BLOCKED is set by an explicit BLOCKED disposition and lifted ONLY by an explicit recognised one
        (WORKING / COMPLETED / SUPERSEDED). An unrecognised or merely acknowledged later edit keeps the
        proven blocker, records the observation, and says so on the row (`retained_block`)."""
        if d["category"] == "blocked" and item["status"] != "BLOCKED":
            rec.pop("retained_block", None); self.store.set_kv(key, rec)
            self.intake._set(item, "BLOCKED", f"BLOCKED per its ack file ({d['blocked_reason'][:160]})", evidence)
        elif item["status"] == "BLOCKED" and d["category"] in EXPLICIT_LIFT:
            rec.pop("retained_block", None); self.store.set_kv(key, rec)
            self.intake._set(item, "ACKNOWLEDGED", f"BLOCKED lifted by explicit {d['stage']} ({d['category']}) in its ack; not product DONE", evidence)
        elif item["status"] == "BLOCKED":
            rec["retained_block"] = {"since_sha": rec.get("sha", "")[:12], "edit_category": d["category"], "at": time.time(),
                                     "note": "later ack edit not recognised as a disposition; the proven blocker stands"}
            self.store.set_kv(key, rec)

    async def _resolve_links(self, repo: str, ref: str, links: list) -> list:
        """A cited link becomes clickable only when it resolves against the verified repo/branch: http(s)
        links as given (external, not checked); relative repo paths → the file must exist on `ref`; commit
        shas → the commit must exist. Otherwise the text is kept, unlinked and marked unverified. Never invented."""
        out = []
        for text in links[:8]:
            if re.match(r"^https?://", text):
                out.append({"text": text, "url": text, "verified": False, "kind": "external"}); continue
            ck = f"linkres:{repo}:{ref}:{text}"
            cached = self.store.get_kv(ck)
            if cached:
                out.append(cached); continue
            res = {"text": text, "url": None, "verified": False, "kind": "commit" if re.fullmatch(r"[0-9a-f]{7,40}", text) else "path"}
            try:
                if res["kind"] == "commit":
                    c = await gh_api(f"repos/{repo}/commits/{text}")
                    if c and c.get("html_url"):
                        res.update(url=c["html_url"], verified=True)
                else:
                    c = await gh_api(f"repos/{repo}/contents/{text}?ref={ref}")
                    if c and c.get("html_url"):
                        res.update(url=c["html_url"], verified=True)
            except Exception as exc:
                res["note"] = f"not found on {ref}: {str(exc)[:80]}"
            if res["verified"]:
                self.store.set_kv(ck, res)
            out.append(res)
        return out

    async def _scan_ack_for_gates(self, src: IntakeSource, ack_branch: str, ack_dir: str, name: str, body: str | None = None, url: str = "", sha: str = "",
                                  category: str | None = None) -> None:
        """Owner gates the coordinator wrote into its ack file (lines under an 'Owner decision(s)' heading or a
        'Next single owner decision' bullet) are quoted, with their source and SHA, for the approval packet.
        Scanned once per content SHA. The CURRENT gates of a source are those present in its newest reading;
        a gate absent from the newest reading, or any gate of a file whose disposition is explicitly SUPERSEDED,
        moves to `history` marked retired — disappearance is never an approval and authorises nothing."""
        kv = self.store.get_kv("gates:" + src.project) or {"gates": [], "scanned": []}
        kv.setdefault("history", [])
        if body is None:
            try:
                content = await gh_api(f"repos/{src.repo}/contents/{ack_dir}/{name}?ref={ack_branch}")
                body = base64.b64decode(content.get("content") or "").decode(errors="replace") if content.get("encoding") == "base64" else ""
                url, sha = content.get("html_url", ""), content.get("sha", sha)
            except Exception:
                return
        source = f"{ack_dir}/{name}"
        now = time.time()
        found: list[dict] = []
        lines = body.splitlines()
        for i, line in enumerate(lines):
            low = line.lower()
            if re.match(r"^#+\s*owner decision", low):
                for nxt in lines[i + 1:i + 6]:
                    if nxt.strip() and not nxt.startswith("#"):
                        found.append({"gate": nxt.strip()[:300], "source": source, "url": url})
                        break
            elif "next single owner decision" in low or low.lstrip("-* ").startswith("owner decisions that only unlock"):
                found.append({"gate": line.strip().lstrip("-* ")[:300], "source": source, "url": url})
        superseded = category == "superseded"
        current_same, others = [g for g in kv["gates"] if g.get("source") == source], [g for g in kv["gates"] if g.get("source") != source]
        new_current = []
        for g in current_same:
            still = any(f["gate"] == g["gate"] for f in found) and not superseded
            if still:
                new_current.append({**g, "sha": sha, "observed_at": now})
            else:
                kv["history"].append({**g, "retired_at": now, "retired_by": (f"explicit SUPERSEDED disposition of {source}@{sha[:7]}" if superseded
                                                                                  else f"no longer present in {source}@{sha[:7]}"),
                                      "approval": "none — disappearance is not an answer; nothing was authorised"})
        for f in found:
            if superseded:
                kv["history"].append({**f, "sha": sha, "first_seen_at": now, "retired_at": now, "retired_by": f"explicit SUPERSEDED disposition of {source}@{sha[:7]}",
                                      "approval": "none — the file superseded itself; nothing was authorised"})
            elif not any(g["gate"] == f["gate"] for g in new_current):
                new_current.append({**f, "sha": sha, "first_seen_at": now, "observed_at": now})
        kv["gates"] = others + new_current
        kv["history"] = kv["history"][-40:]
        kv["scanned"] = (kv.get("scanned") or [])[-200:] + [f"{name}@{sha}" if sha else name]
        self.store.set_kv("gates:" + src.project, kv)

    async def send_liaison(self, src: IntakeSource, item: dict) -> dict:
        """Transfer/Send for a liaison project: write one inbox message on the liaison branch (Aria's protocol)."""
        li = src.coordinator.get("liaison") or {}
        mid = time.strftime("ARIA-%Y%m%d-%H%M") + "-" + re.sub(r"[^a-z0-9]+", "-", item["title"].lower())[:40].strip("-")
        path = f"{li['inbox_dir']}/{mid}.md"
        body = (f"# {item['title']}\n**ID:** {mid}\n**Created:** {time.strftime('%Y-%m-%d %H:%M %Z')}\n**Sender:** Desktop-Agent control plane on behalf of Michael\n"
                f"**Type:** OWNER_DIRECTION\n**Source / provenance:** Desktop-Agent Shared Inbox item {item['item_id']}\n\n{item['body']}\n\n"
                f"Please ACK with `docs/messages/acks/{mid}.md` on your coordinator branch, as the liaison protocol says.\n")
        res = await gh_api(f"repos/{src.repo}/contents/{path}", method="PUT",
                           fields={"message": f"liaison: {item['title'][:60]} (Desktop-Agent)", "branch": li["branch"],
                                   "content": base64.b64encode(body.encode()).decode()})
        url = (res.get("content") or {}).get("html_url", "")
        self.store.set_kv("liaison_sent:" + mid, {"item_id": item["item_id"], "path": path, "sent_at": time.time()})   # the poller tracks this file on the same item
        return {"ok": True, "status": "SENT", "note": f"liaison message {mid} written on {li['branch']}; awaiting the coordinator's sync and ack file",
                "evidence": [url, mid], "url": url}
