"""Memory containers, sensitivity, statuses and the atomic memory record (directive da-c6d1e57152 §B, §C)."""
from __future__ import annotations

import hashlib
import re
import time
import uuid

SCHEMA_VERSION = 1

CONTAINERS = (
    "identity_and_biography", "people_and_relationships", "communication_and_values", "preferences_and_dislikes",
    "skills_and_work_history", "health_and_wellbeing", "finances", "routines_and_workflows", "projects_and_decisions",
    "timeline_and_events", "current_working_context", "corrections_and_superseded_facts", "automation_candidates",
)
SENSITIVE = {"health_and_wellbeing": "high", "finances": "high", "people_and_relationships": "medium"}
CORE_CONTAINERS = ("identity_and_biography", "communication_and_values")   # a compact summary of these is always available
STATUSES = ("active", "pending", "superseded", "expired", "deleted")
CREATED_BY = ("owner_explicit", "runtime_extraction", "import")
SHORT_LIVED_SEC = 7 * 86400                                                # current_working_context default expiry

# Deterministic routing hints: words that point a heading or a question at a container. Lower-case stems.
HINTS: dict[str, tuple[str, ...]] = {
    "identity_and_biography": ("about me", "identity", "bio", "born", "birthday", "age", "live in", "hometown", "my name", "who i am", "background"),
    "people_and_relationships": ("family", "people", "relationship", "wife", "husband", "partner", "spouse", "son", "daughter", "kid", "child", "children",
                                 "mother", "father", "mom", "dad", "parent", "brother", "sister", "sibling", "friend", "colleague", "neighbor", "cousin", "uncle", "aunt", "grand"),
    "communication_and_values": ("communication", "values", "tone", "style", "talk to me", "speak", "brief", "verbose", "principle", "believe", "honest", "respect"),
    "preferences_and_dislikes": ("prefer", "preference", "like", "love", "enjoy", "favorite", "favourite", "dislike", "hate", "can't stand", "avoid", "allergic", "never use", "always use"),
    "skills_and_work_history": ("skill", "work history", "career", "job", "employer", "resume", "experience", "expert", "certified", "degree", "profession", "trade", "business"),
    "health_and_wellbeing": ("health", "doctor", "medical", "medication", "medicine", "diagnos", "sleep", "therapy", "allergy", "blood", "weight", "exercise", "wellbeing", "mental", "appointment with dr", "surgery", "pain"),
    "finances": ("finance", "financial", "money", "bank", "budget", "invoice", "tax", "salary", "income", "loan", "mortgage", "debt", "payment", "pay ", "price", "cost", "account balance", "credit", "insurance"),
    "routines_and_workflows": ("routine", "workflow", "every morning", "every day", "daily", "weekly", "schedule", "habit", "process", "how i", "checklist", "usually", "normally"),
    "projects_and_decisions": ("project", "decision", "decided", "milestone", "deploy", "repo", "pilot", "plan", "roadmap", "caoscare", "desktop agent", "deal sniffer", "aria"),
    "timeline_and_events": ("timeline", "event", "history", "anniversary", "in 20", "last year", "next month", "trip", "moved", "started", "founded", "met"),
    "current_working_context": ("today", "tonight", "this week", "right now", "for now", "currently", "at the moment", "tomorrow", "this afternoon", "until friday"),
    "corrections_and_superseded_facts": ("correction", "corrected", "superseded", "no longer", "used to", "actually", "that's wrong", "not anymore"),
    "automation_candidates": ("remind", "reminder", "follow up", "follow-up", "check every", "monitor", "alert me", "recurring", "every monday", "every week", "automate"),
}


def classify_container(text: str, default: str = "identity_and_biography") -> str:
    """Best container for a heading or a sentence: the container with the most hint hits; ties keep declaration order."""
    low = " " + text.lower() + " "
    best, score = default, 0
    for c in CONTAINERS:
        n = sum(1 for h in HINTS[c] if h in low)
        if n > score:
            best, score = c, n
    return best


def sensitivity_of(container: str) -> str:
    return SENSITIVE.get(container, "low")


def norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower().rstrip(".!"))


def dedup_key(container: str, subject: str, text: str) -> str:
    return hashlib.sha256(f"{container}\n{norm_text(subject)}\n{norm_text(text)}".encode()).hexdigest()[:24]


def new_record(*, container: str, text: str, subject: str = "", key: str | None = None, created_by: str = "owner_explicit",
               source_type: str = "owner", source_ref: str = "", confidence: float = 0.9, status: str = "active",
               sensitivity: str | None = None, tags: list[str] | None = None, valid_from: float | None = None,
               valid_until: float | None = None, supersedes: str | None = None, now: float | None = None) -> dict:
    if container not in CONTAINERS:
        raise ValueError("unknown memory container")
    if status not in STATUSES or created_by not in CREATED_BY:
        raise ValueError("invalid memory status or creator")
    now = now or time.time()
    rid = "m-" + uuid.uuid4().hex[:12]
    return {
        "id": rid, "container": container, "subject": subject.strip()[:200], "key": (key or "").strip()[:120] or None,
        "text": text.strip()[:2000], "source_type": source_type, "source_ref": source_ref[:200], "first_seen_at": now,
        "last_confirmed_at": now, "valid_from": valid_from, "valid_until": valid_until, "confidence": max(0.0, min(1.0, float(confidence))),
        "sensitivity": sensitivity or sensitivity_of(container), "status": status, "supersedes": supersedes, "superseded_by": None,
        "tags": sorted(set(tags or [])), "created_by": created_by, "revision": 1, "dedup": dedup_key(container, subject, text),
        # immutable provenance receipt: who/what created it, from what, when; the content hash proves the text has not drifted
        "provenance": {"created_by": created_by, "source_type": source_type, "source_ref": source_ref[:200], "created_at": now,
                       "text_sha256": hashlib.sha256(text.strip().encode()).hexdigest()[:16]},
    }
