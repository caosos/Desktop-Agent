"""Deliver an ingested owner instruction to a project's coordinator.

Kinds:
  control_plane : this control plane is the coordinator; the item is acknowledged immediately
                  and surfaces on the panel and to Aria.
  claude_peer   : an interactive Claude Code session on this host (found by working directory
                  in Claude Code's own session registry, allow-listed fields only, pid checked
                  alive). Delivery is a Claude Code peer message sent by a tiny headless relay
                  (cheap class, two tools, ~$0.001); the relay's SENT result is the evidence.
Checking for messages never calls a model; only delivery does.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import time
from pathlib import Path

from .config import RuntimeConfig
from .store import Store

MAX_BODY = 6000


HEADLESS_FLAGS = ("-p", "--print", "--output-format", "--json-schema")


def _session_records() -> list[dict]:
    out = []
    for p in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
        try:
            d = json.load(open(p))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def _cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [x.decode(errors="replace") for x in f.read().split(b"\0") if x]
    except OSError:
        return []


def eligible_sessions() -> list[dict]:
    """Every live session record with a verdict. Eligible = explicitly interactive (`kind: interactive`,
    `entrypoint: cli`), named, alive, and not a headless/worker command line. Missing metadata is
    ineligible (fail closed), never assumed interactive."""
    out = []
    for d in _session_records():
        pid, cwd, sname = d.get("pid"), d.get("cwd") or "", d.get("name")
        if not pid or not _alive(pid):
            continue
        why = None
        if not sname:
            why = "unnamed session"
        elif d.get("kind") != "interactive" or d.get("entrypoint") != "cli":
            why = f"not an interactive CLI session (kind={d.get('kind')!r}, entrypoint={d.get('entrypoint')!r})"
        else:
            argv = _cmdline(pid)
            if any(a in HEADLESS_FLAGS for a in argv[1:]):
                why = "headless/worker command line"
        out.append({"name": sname, "pid": pid, "cwd": cwd, "status": d.get("status"), "updatedAt": d.get("updatedAt"),
                    "tmux": d.get("tmux"), "session_id": d.get("sessionId"), "eligible": why is None, "why": why})
    return out


def find_session(cwd_prefix: str, exact: bool = False, name: str | None = None) -> dict | None:
    """Exactly one eligible session by name, or by working directory (prefix or exact). Two matches is
    ambiguity and returns None — commands are never routed to "the newest" session."""
    s, _ = resolve_session(cwd_prefix, exact=exact, name=name)
    return s


def resolve_session(cwd_prefix: str, exact: bool = False, name: str | None = None) -> tuple[dict | None, str]:
    """(session, note): the sole eligible match, or None with the reason (no candidate / N candidates)."""
    cands, rejected = [], []
    for c in eligible_sessions():
        if name:
            if c["name"] != name:
                continue
        elif exact:
            if c["cwd"].rstrip("/") != cwd_prefix.rstrip("/"):
                continue
        elif not c["cwd"].startswith(cwd_prefix.rstrip("/")):
            continue
        (cands if c["eligible"] else rejected).append(c)
    where = f"name {name}" if name else f"cwd {cwd_prefix}{' (exact)' if exact else ''}"
    if len(cands) == 1:
        return cands[0], f"sole eligible session at {where}: {cands[0]['name']} (pid {cands[0]['pid']})"
    if not cands:
        rej = "; ".join(f"{r['name']} rejected: {r['why']}" for r in rejected)
        return None, f"no eligible session at {where}" + (f" ({rej})" if rej else "")
    return None, f"ambiguous: {len(cands)} eligible sessions at {where} ({', '.join(c['name'] for c in cands)}); not routing"


def delivery_text(item: dict, backfill: bool = False) -> str:
    body = item["body"] if len(item["body"]) <= MAX_BODY else item["body"][:MAX_BODY] + "\n[... truncated; full text at the URL]"
    return (f"DA-INTAKE item={item['item_id']} project={item['project']} {'(backfill of an existing comment) ' if backfill else ''}"
            f"owner instruction from {item['author']} on {item['repo']}#{item['issue']} ({item['kind']}, {item['posted_at']}): {item['url']}\n\n"
            f"{body}\n\n"
            f"Acknowledge on the issue with a comment containing `ACK {item['item_id']}`; report progress with "
            f"`WORKING {item['item_id']}`, `BLOCKED {item['item_id']}` or `DONE {item['item_id']}`. "
            f"Delivered automatically by the Desktop-Agent control plane; no reply to this message is needed.")


class Deliverer:
    def __init__(self, cfg: RuntimeConfig, store: Store, claude_bin: str = "claude", backfill_before: float | None = None):
        self.cfg, self.store, self.claude_bin = cfg, store, claude_bin
        self.backfill_before = backfill_before or time.time()

    async def __call__(self, src, item: dict) -> dict:
        kind = (src.coordinator or {}).get("kind", "control_plane")
        backfill = self._is_backfill(item)
        if kind == "control_plane":
            return {"ok": True, "status": "ACKNOWLEDGED", "note": "received by the Desktop-Agent control plane (its own coordinator)",
                    "evidence": [item["url"], "coordinator=control_plane"],
                    "comment": f"ACK {item['item_id']} — received by the Desktop-Agent control plane (automatic intake). "
                               f"Status is shown on the Desktop-Agent panel."}
        if kind == "claude_peer":
            return await self._claude_peer(src, item, backfill)
        if kind == "liaison":
            return {"ok": True, "status": "RECEIVED", "note": "liaison inbox: no session reachable from this account; awaiting the coordinator's own sync",
                    "evidence": [item.get("url", ""), "coordinator=liaison"]}
        return {"ok": False, "note": f"unknown coordinator kind {kind!r}", "evidence": [kind]}

    async def batch(self, src, items: list[dict]) -> dict:
        """Several items at once (the backlog on first sight): one peer message, one comment."""
        kind = (src.coordinator or {}).get("kind", "control_plane")
        ids = ", ".join(i["item_id"] for i in items)
        if kind == "control_plane":
            return {"ok": True, "status": "ACKNOWLEDGED", "note": "received by the Desktop-Agent control plane (its own coordinator)",
                    "evidence": [items[0]["url"], "coordinator=control_plane", f"batch={len(items)}"],
                    "comment": f"ACK {ids} — {len(items)} owner instruction(s) received by the Desktop-Agent control plane "
                               f"(automatic intake, backfill of existing comments). Status is shown on the Desktop-Agent panel."}
        if kind == "liaison":
            return {"ok": True, "status": "RECEIVED", "note": "liaison inbox: awaiting the coordinator's own sync", "evidence": ["coordinator=liaison", f"batch={len(items)}"]}
        if kind == "claude_peer":
            combined = "\n\n=====\n\n".join(delivery_text({**i, "body": i["body"][:1800] + ("\n[... truncated; full text at the URL]" if len(i["body"]) > 1800 else "")}, True)
                                            for i in items)
            fake = {**items[0], "body": combined}
            res = await self._claude_peer(src, fake, backfill=True, prebuilt_text=combined)
            if res.get("ok"):
                res["comment"] = (f"DELIVERED {ids} ({len(items)} items, backfill) to the running {src.project} coordinator session as one "
                                  f"Claude Code peer message (automatic intake). Awaiting `ACK <item_id>` for each.")
            return res
        return {"ok": False, "note": f"unknown coordinator kind {kind!r}", "evidence": [kind]}

    def _is_backfill(self, item: dict) -> bool:
        try:
            posted = time.mktime(time.strptime(item["posted_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except (ValueError, TypeError):
            return False
        return posted < self.backfill_before

    @staticmethod
    def resolve_for(src) -> tuple[dict | None, str]:
        """Pinned name first (the registered intended coordinator). When that session is gone (every
        restart renames it), exactly one eligible interactive session in the configured working directory
        may stand in; zero or several = fail closed, with the reason. The result says how it was found."""
        c = src.coordinator or {}
        notes = []
        if c.get("session_name"):
            s, note = resolve_session("", name=c["session_name"])
            if s:
                return {**s, "resolved_by": "name"}, note
            notes.append(note)
        for key, label, exact in (("session_cwd_exact", "cwd (exact)", True), ("session_cwd", "cwd (prefix)", False)):
            if c.get(key):
                s, note = resolve_session(c[key], exact=exact)
                if s:
                    return {**s, "resolved_by": label, "pinned_name": c.get("session_name")}, "; ".join(notes + [note])
                notes.append(note)
        return None, "; ".join(notes) or "no coordinator session configured"

    @staticmethod
    def session_for(src) -> dict | None:
        return Deliverer.resolve_for(src)[0]

    async def _claude_peer(self, src, item: dict, backfill: bool, prebuilt_text: str | None = None) -> dict:
        c = src.coordinator or {}
        where = c.get("session_name") or c.get("session_cwd_exact") or c.get("session_cwd") or ""
        session, rnote = self.resolve_for(src)
        if not session:
            return {"ok": False, "note": f"no live Claude Code session at {where}; queued, awaiting connection ({rnote})"[:300], "evidence": [where, rnote[:200]]}
        how = (f"; pinned name {c['session_name']} not live, resolved by {session['resolved_by']}"
               if session.get("resolved_by", "name") != "name" and c.get("session_name") else "")
        text = prebuilt_text or delivery_text(item, backfill)
        prompt = ("You are a delivery relay. Use the ListAgents tool, then use the SendMessage tool to send EXACTLY the text between "
                  f"<<< and >>> (verbatim, no additions) to the agent named '{session['name']}'. Then reply with the single word SENT, "
                  f"or FAILED and the reason.\n<<<\n{text}\n>>>")
        model = self.cfg.models.get("cloud_cheap", "claude-haiku-5-5")
        argv = [self.claude_bin, "-p", prompt, "--output-format", "json", "--model", model, "--max-turns", "4",
                "--tools", "ListAgents,SendMessage", "--allowedTools", "ListAgents", "SendMessage",
                "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, env=env, cwd=str(Path.home()))
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "note": "relay timed out", "evidence": [session["name"]]}
        raw = out.decode(errors="replace")
        try:
            msg = json.loads(raw[raw.index("{"):])
        except (ValueError, json.JSONDecodeError):
            return {"ok": False, "note": f"relay returned no JSON: {raw[:120]!r}", "evidence": [session["name"]]}
        result = str(msg.get("result") or "").strip()
        cost = msg.get("total_cost_usd")
        self.store.add_cost(None, None, model, cost, int((msg.get("usage") or {}).get("input_tokens") or 0),
                            int((msg.get("usage") or {}).get("output_tokens") or 0))
        if result.upper().startswith("SENT") and not msg.get("is_error"):
            when = time.strftime("%Y-%m-%d %H:%M:%S %Z")
            return {"ok": True, "status": "DELIVERED",
                    "note": f"peer message to session {session['name']} (pid {session['pid']}, cwd {session['cwd']}{how}) at {when}; relay ${cost}",
                    "evidence": [f"session={session['name']}", f"pid={session['pid']}", f"relay_cost_usd={cost}", f"relay_session={msg.get('session_id')}"],
                    "comment": f"DELIVERED {item['item_id']} to the running {src.project} coordinator session `{session['name']}` at {when} "
                               f"as a Claude Code peer message (automatic intake{', backfill' if backfill else ''}). Awaiting `ACK {item['item_id']}`."}
        return {"ok": False, "note": f"relay reported: {result[:160]}", "evidence": [session["name"], f"relay_cost_usd={cost}"]}
