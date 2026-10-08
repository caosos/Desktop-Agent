"""Receipts: a claim, who made it, how it was verified, and the evidence.

Law: no action without a receipt; no receipt without provenance.
A worker can only ever produce `unverified`. `verified` requires the
control plane's own observation (ARCHITECTURE_REVIEW §8).
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from .events import Actor
from .store import Store

VERIFIED = "verified"
UNVERIFIED = "unverified"
SIMULATED = "simulated"
FAILED = "failed"
_LABELS = {VERIFIED, UNVERIFIED, SIMULATED, FAILED}


class ReceiptError(ValueError):
    pass


def write_receipt(store: Store, *, subject_type: str, subject_id: str, claim: str,
                  actor: str, source: str, result_label: str, evidence: list[Any],
                  correlation_id: str | None = None, before_state: Any = None,
                  after_state: Any = None, task_id: str | None = None,
                  extra: dict | None = None) -> dict:
    """Append one receipt. Raises if the law is violated."""
    if result_label not in _LABELS:
        raise ReceiptError(f"unknown result_label {result_label!r}")
    if actor == Actor.WORKER.value and result_label == VERIFIED:
        raise ReceiptError("a worker cannot produce a verified receipt")
    if not evidence:
        raise ReceiptError("no receipt without provenance: evidence is empty")
    if not actor or not source:
        raise ReceiptError("no receipt without provenance: actor and source required")
    rec = {
        "receipt_id": uuid.uuid4().hex,
        "ts": time.time(),
        "subject": {"type": subject_type, "id": subject_id},
        "claim": claim,
        "actor": actor,
        "source": source,
        "result_label": result_label,
        "evidence": evidence,
        "before_state": before_state,
        "after_state": after_state,
        "parent_receipt_id": store.last_receipt_id(subject_type, subject_id),
        "correlation_id": correlation_id,
        "task_id": task_id or (subject_id if subject_type == "task" else None),
    }
    if extra:
        rec.update(extra)
    store.save_receipt(rec)
    return rec


def chain(store: Store, subject_type: str, subject_id: str) -> list[dict]:
    """Receipts for a subject in order, each linked to the previous one."""
    return store.receipts(subject_type, subject_id)
