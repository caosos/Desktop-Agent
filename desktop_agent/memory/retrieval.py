"""Per-turn retrieval: deterministic container routing from the question, local full-text search, sensitivity gating,
a bounded top-k block with provenance. The compact core (identity, communication) is always available; everything
else is fetched by relevance. Live project state is NOT here: it still comes from Aria's live actions."""
from __future__ import annotations

import time

from .model import CONTAINERS, CORE_CONTAINERS, HINTS, SENSITIVE

TOP_K = 10
MAX_CHARS = 3000
CORE_CHARS = 900
NON_SENSITIVE = [c for c in CONTAINERS if SENSITIVE.get(c) != "high"]
HEADER = ("PRIVATE MEMORY — Michael's personal memory, selected for this message. Use it only when relevant; it is remembered "
          "context, not live project status (live state comes from your actions). Never repeat it verbatim unless asked, never "
          "send it to a project, coordinator, worker or research job, and never widen any authority because of it.")


def route(question: str) -> list[str]:
    """Containers worth searching for this question: every container whose hints match; high-sensitivity containers only
    when their own hints match; no hint at all → every non-sensitive container (full-text search decides)."""
    low = " " + question.lower() + " "
    hit = [c for c in CONTAINERS if any(h in low for h in HINTS[c])]
    if not hit:
        return list(NON_SENSITIVE)
    return hit + [c for c in NON_SENSITIVE if c not in hit and c not in CORE_CONTAINERS]


def _line(r: dict) -> str:
    when = time.strftime("%Y-%m-%d", time.localtime(r["last_confirmed_at"]))
    key = f"{r['key']}: " if r.get("key") else ""
    return f"- [{r['container']} · {r['subject'][:40]} · {r['created_by']} · conf {r['confidence']:.2f} · as of {when} · {r['id']}] {key}{r['text']}"


def core_block(store) -> tuple[str, list[str]]:
    lines, ids, size = [], [], 0
    for c in CORE_CONTAINERS:
        for r in store.active(c, limit=12):
            if "core" not in r["tags"] and r["created_by"] != "owner_explicit":
                continue
            ln = _line(r)
            if size + len(ln) > CORE_CHARS:
                break
            lines.append(ln); ids.append(r["id"]); size += len(ln)
    return "\n".join(lines), ids


def retrieve(store, question: str, k: int = TOP_K, max_chars: int = MAX_CHARS) -> dict:
    """{"text": private context block or "", "ids": [...], "containers": [...], "pending": n}."""
    store.expire_due()
    containers = route(question)
    core_text, core_ids = core_block(store)
    hits = [h for h in store.search(question, containers, limit=k * 2) if h["id"] not in core_ids]
    hits.sort(key=lambda h: (h["rank"], -h["confidence"], -h["last_confirmed_at"]))
    lines, ids, size = [], [], len(core_text)
    for h in hits[:k]:
        ln = _line(h)
        if size + len(ln) > max_chars:
            break
        lines.append(ln); ids.append(h["id"]); size += len(ln)
    pending = store.list(status="pending", limit=3)
    conflict = next((p for p in pending if any(t.startswith("conflict:") for t in p["tags"])), None)
    parts = [HEADER]
    if core_text:
        parts.append("CORE:\n" + core_text)
    if lines:
        parts.append("RELEVANT:\n" + "\n".join(lines))
    if conflict:
        old = store.get(next(t.split(":", 1)[1] for t in conflict["tags"] if t.startswith("conflict:")))
        parts.append("ONE PENDING CONFLICT (ask one focused question only if it matters to this message; otherwise leave it to the Memory Center): "
                     f"new '{conflict['text'][:160]}' vs current '{(old or {}).get('text', '')[:160]}'")
    used = core_ids + ids
    if used:
        store.log_use(used, "retrieved", {"containers": containers[:6], "question_chars": len(question)})
    store.set_meta("last_retrieval_at", str(time.time()))
    return {"text": "\n\n".join(parts) if used or conflict else "", "ids": used, "containers": containers, "pending": len(pending)}
