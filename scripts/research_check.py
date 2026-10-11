"""Structure and citation checker for Ask Aria research reports (docs/RESEARCH_BRIEF.md §4, §11 web mode).

Usage: research_check.py <report> [--repo <root>]. Exits 0 only when every rule holds; otherwise prints each
failed rule (one per line, prefixed with its name) and exits 1. It checks structure and that citations resolve,
NOT that the findings are true.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SECTIONS = ["Question", "Findings", "Analysis", "Limits", "Sources", "Review"]
SENTENCE = "No web retrieval was used."
WEB_SENTENCE = "Web retrieval was used."
WEB_HEADER = re.compile(r"^job:.*sources:\s*web", re.M)
WEB_SOURCE = re.compile(r"^\[(\d+)\]\s+(https?://\S+)\s+—\s+\".+\"\s+\(accessed\s+(\d{4}-\d{2}-\d{2})\)\s*$")
CITE = re.compile(r"\[(\d+)\]")
SOURCE = re.compile(r"^\[(\d+)\]\s+(\S+?):(\d+)\b")
ITEM = re.compile(r"^\s*(\d+)\.\s+(.*)$")


def sections(text: str) -> tuple[list[str], dict[str, str]]:
    order, body, cur = [], {}, None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1)
            order.append(cur)
            body[cur] = ""
        elif cur:
            body[cur] += line + "\n"
    return order, body


def check(report: Path, repo: Path) -> list[str]:
    if not report.is_file():
        return [f"file: report not found: {report}"]
    text = report.read_text(encoding="utf-8")
    order, body = sections(text)
    errors: list[str] = []
    if [s for s in order if s in SECTIONS] != SECTIONS:
        errors.append(f"sections: expected {SECTIONS} in this order, found {order}")
    findings = [m.group(2) for m in map(ITEM.match, body.get("Findings", "").splitlines()) if m]
    reviews = re.split(r"\s*\|\s*|\n", body.get("Review", "").strip())
    reviews = [r for r in reviews if ITEM.match(r)]
    for i, f in enumerate(findings, 1):
        if not CITE.search(f):
            errors.append(f"uncited-finding: finding {i} has no [n] citation: {f[:60]}")
    web = bool(WEB_HEADER.search(text))
    sources: dict[str, tuple[str, int]] = {}
    web_sources: set[str] = set()
    for line in body.get("Sources", "").splitlines():
        m = SOURCE.match(line.strip())
        if m:
            sources[m.group(1)] = (m.group(2), int(m.group(3)))
            continue
        w = WEB_SOURCE.match(line.strip())
        if w:
            if not web:
                errors.append(f"web-source-in-local-report: [{w.group(1)}] cites a URL but the header says local sources only")
            web_sources.add(w.group(1))
        elif line.strip().startswith("[") and "http" in line:
            errors.append(f'web-source-format: {line.strip()[:70]} must read [n] https://… — "quote" (accessed YYYY-MM-DD)')
    for n in sorted(set(CITE.findall(text)) - set(sources) - web_sources, key=int):
        errors.append(f"unresolved-citation: [{n}] has no Sources line")
    for n, (path, line_no) in sources.items():
        target = repo / path
        if not target.is_file():
            errors.append(f"missing-path: [{n}] {path} does not exist under {repo}")
        elif line_no < 1 or line_no > len(target.read_text(encoding="utf-8", errors="replace").splitlines()):
            errors.append(f"missing-line: [{n}] {path} has no line {line_no}")
    want = WEB_SENTENCE if web else SENTENCE
    if want not in body.get("Limits", ""):
        errors.append(f'limits-sentence: the Limits section must contain "{want}"')
    if len(reviews) != len(findings):
        errors.append(f"review-count: {len(findings)} finding(s) but {len(reviews)} Review entr{'y' if len(reviews) == 1 else 'ies'}")
    return errors


def main(argv: list[str]) -> int:
    args = list(argv)
    repo = Path.cwd()
    if "--repo" in args:
        i = args.index("--repo")
        repo = Path(args[i + 1])
        del args[i:i + 2]
    if len(args) != 1:
        print("usage: research_check.py <report> [--repo <root>]", file=sys.stderr)
        return 2
    errors = check(Path(args[0]), repo)
    for e in errors:
        print(e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
