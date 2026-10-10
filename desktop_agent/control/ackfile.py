"""Read-only disposition of a liaison ACK file (Agent 01's `docs/messages/acks/<message>.md`).

An ack file is edited over time (RECEIVED → WORKING → BLOCKED → COMPLETED …), so the poller keys its
reading on the file's content SHA, not its name. The parsed disposition keeps SEPARATE facts and never
collapses them: `stage` (the coordinator's own word), `category` (acknowledged / working / blocked /
coordination-completed), `implementation` (what the ack says about actual product work: not stated /
not implemented / result linked), `live` (verified / not live or pending / unknown), the evidence links
it cites, what it marks NOT proven, and queued follow-ups. **COMPLETED means the coordination step is
complete, never that product work is DONE or live.** Anything the file does not say stays unknown.
Format is Agent 01's semi-structured markdown; the raw stage line is always kept beside the parse.
"""
from __future__ import annotations

import re

PARSER_VERSION = 2            # bump when parse_ack learns a format; stored readings are re-parsed (no new receipt unless the category changes)

STAGE_RX = re.compile(r"\*\*stage:?\*\*:?\s*([A-Za-z_-]+)\s*(.*)$", re.I)          # anywhere in the line: Agent 01 also writes "**Acked by:** … **Stage:** COMPLETED (…)"
LINK_RX = re.compile(r"\((https?://[^)\s]+)\)|(?<![\w/])(docs/(?:receipts|reports|handoff|status)/[\w./-]+)|\b(?:commit|sha)\s+`?([0-9a-f]{7,40})`?", re.I)
DISPOSITION_RX = re.compile(r"\*\*disposition:?(?:\*\*)?:?\s*([A-Za-z_ +()/-]+?)(?:[.\n]|\*\*|$)", re.I)   # older acks: "**Disposition:** INCORPORATED"
FOLLOWUP_RX = re.compile(r"\b(F-?\d+)\b[^.\n]{0,80}?\bqueued\b|\bqueued\b[^.\n]{0,40}?\b(F-?\d+)\b", re.I)

CATEGORY = {"RECEIVED": "acknowledged", "ACKNOWLEDGED": "acknowledged", "ACK": "acknowledged", "WORKING": "working", "IN_PROGRESS": "working",
            "BLOCKED": "blocked", "WAITING_OWNER": "blocked", "COMPLETED": "coordination-completed", "COMPLETE": "coordination-completed",
            "DONE": "coordination-completed", "RESOLVED": "coordination-completed", "INCORPORATED": "coordination-completed",
            "NEEDS_OWNER_DECISION": "blocked", "SUPERSEDED": "superseded", "CANCELLED": "superseded", "CANCELED": "superseded"}


def _section(lines: list[str], marker: str, limit: int = 6) -> list[str]:
    out = []
    for i, line in enumerate(lines):
        if marker in line.lower():
            for nxt in lines[i + 1:i + 1 + limit]:
                s = nxt.strip()
                if not s or (not s.startswith(("-", "*")) and not nxt.startswith((" ", "\t"))):
                    break
                out.append(s.lstrip("-* ").strip()[:200])
            break
    return out


def parse_ack(body: str) -> dict:
    lines = body.splitlines()
    stage, stage_text, stage_line = None, "", ""
    for line in lines:
        m = STAGE_RX.search(line)
        if m:
            stage, stage_text, stage_line = m.group(1).upper().replace("-", "_"), m.group(2).strip(), line.strip()[:300]
            break
    if stage is None:
        for line in lines:
            m = DISPOSITION_RX.search(line)
            if m:
                words = [w for w in re.findall(r"[A-Za-z_]+", m.group(1).upper()) if w in CATEGORY]
                if words:
                    stage, stage_text, stage_line = "+".join(dict.fromkeys(words)), m.group(1).strip(), line.strip()[:300]
                break
    low = body.lower()
    parts = (stage or "").split("+")
    category = "blocked" if "NEEDS_OWNER_DECISION" in parts else CATEGORY.get(parts[0], "unknown")
    not_impl = bool(re.search(r"\bnot implemented\b|\bis not implemented\b|\bnot yet implemented\b", low))
    not_proven = [l.split("**", 2)[-1].strip(": ").strip()[:200] for l in lines if re.match(r"^\s*[-*]?\s*\*\*not proven", l, re.I)]
    evidence = _section(lines, "done by the coordinator")
    links = []
    for m in LINK_RX.finditer(body):
        v = next(g for g in m.groups() if g)
        if v not in links:
            links.append(v)
    live = "unknown"
    if re.search(r"\*\*verified live\*\*|verified live[:\s]", low) and not re.search(r"not (yet )?verified live", low):
        live = "verified"
    elif re.search(r"\bnot live\b|deployment (is )?(still )?pending|pending deployment|\bnot deployed\b|awaiting deploy|live acceptance pending|pending (the )?owner reload", low):
        live = "not live (pending)"
    if not_impl and live != "verified":
        live = "not applicable (nothing implemented per the ack)"
    if category == "coordination-completed":
        implementation = "not implemented (per the ack)" if not_impl else ("result linked (per the ack)" if (evidence or links) else "not stated")
    elif category == "blocked":
        implementation = "blocked (per the ack)"
    else:
        implementation = "not stated"
    followups = []
    for m in FOLLOWUP_RX.finditer(body):
        v = (m.group(1) or m.group(2) or "").upper().replace("F", "F-").replace("F--", "F-")
        if v and v not in followups:
            followups.append(v)
    acked_by = next((l.strip().lstrip("-* ")[:200] for l in lines if re.match(r"^\s*[-*]?\s*\*\*(acked|resolved) by", l, re.I)), "")
    return {"stage": stage, "stage_text": stage_text[:240], "stage_line": stage_line, "category": category,
            "blocked_reason": (stage_text[:240] if stage != "NEEDS_OWNER_DECISION" and "NEEDS_OWNER_DECISION" not in parts else "needs an owner decision (per the ack)") if category == "blocked" else "",
            "implementation": implementation, "not_implemented": not_impl, "implementation_evidence": evidence[:5], "links": links[:8],
            "live": live, "not_proven": not_proven[:3], "followups": followups[:4], "acked_by": acked_by}


def next_action(d: dict | None, status: str) -> str:
    """One truthful sentence for the instruction row, from the ack's own disposition. Never 'done'."""
    if not d:
        return ""
    c = d.get("category")
    if c == "blocked":
        return f"BLOCKED per its ack: {d.get('blocked_reason') or d.get('stage_text') or 'reason not stated'}"
    if c == "coordination-completed":
        fu = f"; queued: {', '.join(d['followups'])}" if d.get("followups") else ""
        if d.get("not_implemented"):
            return f"coordination step completed per its ack; the product change is NOT implemented (its own words){fu} — product result unknown until a worker receipt or PR appears"
        if d.get("implementation") == "result linked (per the ack)":
            lv = {"verified": "verified live per the ack", "not live (pending)": "deployment/live verification still pending per the ack"}.get(d.get("live"), "live state not stated — unknown")
            return f"coordination completed; implementation result linked ({len(d.get('links') or [])} link(s)); {lv}{fu}"
        return f"coordination completed per its ack; implementation result not stated — unknown{fu}"
    if c == "superseded":
        return f"SUPERSEDED per its ack ({d.get('stage_text') or 'no detail'}); nothing further expected from it"
    if c == "working":
        return "WORKING per its ack; result arrives in a later edit of the same file"
    if c == "acknowledged":
        return "acknowledged per its ack file; no result stated yet"
    return "ack file present but its stage line was not recognised — see the file"
