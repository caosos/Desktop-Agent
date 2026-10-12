"""Recognise a supervisor's canonical check-in LOG (DA-CHECKIN-HEADING-LOOP-001, da-8d1730d062): a status report, not an instruction. It carries the
hidden marker `<!-- da:canonical-checkin` or opens with the exact heading `CANONICAL CHECK-IN LOG`. Such a comment is recorded but never delivered
and never mirrored, so no ACK is owed and the report-ACK-mirror-report loop cannot start. Deliberately narrow: other supervisor comments (even with
the da:supervisor marker) have carried real directives and keep crossing."""
from __future__ import annotations

import re

_HEADING = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?CANONICAL CHECK-IN LOG\b", re.I)


def is_checkin_report(text: str) -> bool:
    text = text or ""
    return "<!-- da:canonical-checkin" in text[:300] or bool(_HEADING.match(text.lstrip().split("\n", 1)[0] if text.strip() else ""))
