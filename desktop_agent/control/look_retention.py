"""Retention for stored screenshots (M2 hardening, owner directive da-50b1e88f20): a capture is private screen content, so the image
files under data/looks/ are deleted after RETENTION_DAYS. Only the image folder goes; the receipt that records the capture (with its
sha256 and time) stays, so provenance outlives the picture. One receipt per sweep that removed something; none for an empty sweep."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .events import Actor
from .receipts import VERIFIED, write_receipt

RETENTION_DAYS = 7


def sweep(store, data_dir: Path, now: float | None = None, days: int = RETENTION_DAYS) -> list[str]:
    root, now = Path(data_dir) / "looks", now or time.time()
    if not root.is_dir():
        return []
    gone = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and d.name.startswith("lk-") and now - d.stat().st_mtime > days * 86400:
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d.name)
    if gone:
        write_receipt(store, subject_type="look", subject_id="retention", claim=f"removed {len(gone)} stored screenshot folder(s) older than {days} days; their capture receipts remain",
                      actor=Actor.CONTROL.value, source="look_retention", result_label=VERIFIED, evidence=[f"removed={n}" for n in gone[:20]] + [f"older_than_days={days}"])
    return gone
