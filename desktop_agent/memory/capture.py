"""Automatic memory capture after an Ask Aria turn, without a model (owner directive da-c6d1e57152 §F): explicit
'remember', corrections, preferences, identity and relationship facts, temporary context (with expiry) and automation
candidates become records with provenance. Clear owner facts commit; uncertain, conflicting or high-sensitivity
replacements stay PENDING for the owner's Memory Center; chatter is not saved. Only the owner's own words to Aria are read."""
from __future__ import annotations

import re
import time

from .model import CONTAINERS, SENSITIVE, SHORT_LIVED_SEC, classify_container, new_record, norm_text

CHATTER_STARTS = ("pause", "resume", "stop", "status", "what", "how", "show", "list", "yes", "no", "ok", "okay", "thanks", "thank", "hello", "hi ",
                  "hey", "go", "do it", "sure", "please check", "tell me", "explain", "why", "when", "where", "who", "is ", "are ", "can ", "could ", "run ")
_EXPLICIT = re.compile(r"^(?:please\s+)?(?:remember|note|don't forget|do not forget|keep in mind)(?:\s+that)?[:,]?\s+(.+)$", re.I)
_CORRECTION = re.compile(r"^(?:actually|correction[:,]?|no[,:]\s*|that's wrong[,:]?\s*|that is wrong[,:]?\s*)\s*(.+)$", re.I)
_AUTOMATION = re.compile(r"\b(remind me|every (?:day|week|month|morning|evening|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|follow up|check .{0,40}(?:daily|weekly|hourly)|alert me|monitor )", re.I)
_PREFERENCE = re.compile(r"\bI (?:prefer|like|love|hate|dislike|can't stand|cannot stand|always|never|want you to|don't want)\b", re.I)
_MINE = re.compile(r"\bmy ([a-z][a-z' ]{1,30}?) (?:is|are|was|will be) (.+)", re.I)
_UNCERTAIN = re.compile(r"\b(maybe|i think|probably|not sure|might|possibly|i guess)\b", re.I)
_TEMPORARY = re.compile(r"\b(today|tonight|this week|right now|for now|this afternoon|tomorrow|until friday|at the moment)\b", re.I)
_RELATION = re.compile(r"\b(wife|husband|partner|spouse|son|daughter|kid|child|mother|father|mom|dad|brother|sister|friend|colleague|boss|cousin|uncle|aunt|grand\w*)\b", re.I)


def is_chatter(text: str) -> bool:
    t = text.strip().lower()
    return len(t) < 12 or t.endswith("?") or "?" in t[:80] or t.startswith(CHATTER_STARTS)


def _subject(text: str) -> tuple[str, str | None]:
    m = _MINE.search(text)
    if m:
        return m.group(1).strip(), m.group(1).strip().lower()
    head = re.split(r"\b(?:is|are|was|will|has|have|lives?|works?)\b|[:,]", text, maxsplit=1)[0].strip()
    return (head[:60] if 0 < len(head.split()) <= 6 else " ".join(text.split()[:4])), None


def analyse(owner_text: str, now: float | None = None) -> list[dict]:
    """Candidates from one owner message: {container, subject, key, text, created_by, confidence, status, kind, valid_until, tags}."""
    now = now or time.time()
    text = " ".join(owner_text.split())
    if is_chatter(text) and not (_EXPLICIT.match(text) or _CORRECTION.match(text)):
        return []
    if m := _AUTOMATION.search(text):
        subj, _ = _subject(text)
        return [{"container": "automation_candidates", "subject": subj, "key": None, "text": text, "created_by": "runtime_extraction", "confidence": 0.7,
                 "status": "pending", "kind": "automation", "valid_until": None, "tags": ["candidate", m.group(1).lower()[:20]]}]
    kind, body, created_by, conf = "statement", text, "runtime_extraction", 0.6
    if m := _EXPLICIT.match(text):
        kind, body, created_by, conf = "explicit", m.group(1), "owner_explicit", 0.95
    elif m := _CORRECTION.match(text):
        kind, body, created_by, conf = "correction", m.group(1), "owner_explicit", 0.9
    elif _PREFERENCE.search(text):
        kind, conf = "preference", 0.8
    elif _MINE.search(text):
        kind, conf = "identity", 0.85
    elif _TEMPORARY.search(text) and (" i " in f" {text.lower()} " or text.lower().startswith(("i'm ", "i am ", "i'll ", "i will "))):
        kind, conf = "temporary", 0.7
    else:
        return []                                             # routine chatter: not saved
    subject, key = _subject(body)
    container = classify_container(body)
    if kind == "preference" and SENSITIVE.get(container) != "high":
        container = "preferences_and_dislikes"
    if kind == "identity":
        container = "people_and_relationships" if _RELATION.search(key or subject) else container
    valid_until = None
    if kind == "temporary" or (container == "current_working_context" and _TEMPORARY.search(body)):
        container, valid_until = "current_working_context", now + SHORT_LIVED_SEC
    status = "pending" if _UNCERTAIN.search(body) else "active"
    return [{"container": container, "subject": subject, "key": key, "text": body.rstrip(". "), "created_by": created_by, "confidence": conf,
             "status": status, "kind": kind, "valid_until": valid_until, "tags": [kind]}]


def commit(store, candidates: list[dict], source_ref: str) -> dict:
    """Capture policy: dedup; a conflict is superseded by an explicit correction, otherwise parked as pending; an inferred
    fact in a high-sensitivity container is pending; the owner's per-container switch is honoured."""
    n = {"committed": 0, "pending": 0, "superseded": 0, "confirmed": 0, "ignored": 0}
    for c in candidates:
        if not store.capture_on(c["container"]):
            n["ignored"] += 1
            continue
        status = c["status"]
        sensitive = SENSITIVE.get(c["container"]) == "high"
        if sensitive and c["created_by"] != "owner_explicit":
            status = "pending"
        conflict = None if c["kind"] in ("automation", "temporary") else (store.find_conflict(c["container"], c["subject"], c["key"]) if c["subject"] else None) \
            or store.find_related([c["container"]], c["text"]) or store.find_related([x for x in CONTAINERS if x != c["container"]], c["text"])
        if conflict is not None and norm_text(conflict["text"]) == norm_text(c["text"]):
            conflict = None
        if conflict is not None:                                   # the same subject lives where it was first recorded
            c["container"] = conflict["container"]; sensitive = SENSITIVE.get(c["container"]) == "high"
        tags = list(c.get("tags") or [])
        rec = new_record(container=c["container"], subject=c["subject"], key=c["key"], text=c["text"], created_by=c["created_by"], source_type="aria_turn",
                         source_ref=source_ref, confidence=c["confidence"], status=status, valid_until=c["valid_until"], tags=tags)
        if conflict is not None:
            explicit = c["created_by"] == "owner_explicit"
            if explicit and status == "active" and not (sensitive and c["kind"] != "correction"):
                store.supersede(conflict["id"], rec, actor="owner"); n["superseded"] += 1
                continue
            rec["status"] = "pending"; rec["tags"] = sorted(set(tags + [f"conflict:{conflict['id']}"]))
        added = store.add(rec, actor="control")
        if added["duplicate"]:
            n["confirmed"] += 1
        elif added["status"] == "pending":
            n["pending"] += 1
        else:
            n["committed"] += 1
    store.set_meta("last_capture_at", str(time.time()))
    return n
