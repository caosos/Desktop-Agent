"""Research report → HTML → PDF: the Markdown subset is rendered faithfully and escaped; the PDF path reports
truthfully (a real PDF or a named failure) and never raises for the expected failures."""
from pathlib import Path

import pytest

from desktop_agent.control import research_pdf as rp

REPORT = """# Research: what does the gate do?
job: rj-abc  ·  project: desktop_agent  ·  sources: local repository only (no web retrieval)  ·  2026-10-11

## Question
What does `scripts/gate.sh` do?

## Findings
1. It prints PASS or FAIL from pytest's exit status **only** [1]
2. It never pipes pytest into tail [2]

## Limits
No web retrieval was used. <script>alert(1)</script>

## Sources
[1] scripts/gate.sh:3 — "rc=$?"
- bullet item
"""


def test_markdown_subset_renders_and_escapes():
    h = rp.markdown_to_html(REPORT, footer="Desktop-Agent research job rj-abc")
    assert "<h1>Research: what does the gate do?</h1>" in h and '<p class="meta">job: rj-abc' in h
    assert "<h2>Findings</h2><ol><li>It prints PASS or FAIL from pytest&#x27;s exit status <strong>only</strong> <sup>[1]</sup></li>" in h
    assert "<code>scripts/gate.sh</code>" in h and "<ul><li>bullet item</li></ul>" in h
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h and "<script>" not in h
    assert h.endswith("<footer>Desktop-Agent research job rj-abc</footer></body></html>")


def test_render_pdf_reports_missing_report_and_missing_browser(tmp_path: Path, monkeypatch):
    r = rp.render_pdf(tmp_path / "none.md", tmp_path / "out.pdf")
    assert r["ok"] is False and ("not found" in r["reason"] or "no Chrome" in r["reason"])
    monkeypatch.setattr(rp, "chrome_binary", lambda: None)
    (tmp_path / "r.md").write_text(REPORT)
    r = rp.render_pdf(tmp_path / "r.md", tmp_path / "out.pdf")
    assert r["ok"] is False and "no Chrome" in r["reason"] and not (tmp_path / "out.pdf").exists()


@pytest.mark.skipif(rp.chrome_binary() is None, reason="no Chrome on this host")
def test_render_pdf_produces_a_real_pdf(tmp_path: Path):
    (tmp_path / "r.md").write_text(REPORT)
    r = rp.render_pdf(tmp_path / "r.md", tmp_path / "out" / "r.pdf", footer="rj-abc")
    assert r["ok"] is True, r
    data = (tmp_path / "out" / "r.pdf").read_bytes()
    assert data.startswith(b"%PDF") and r["bytes"] == len(data) > 1000 and len(r["sha256"]) == 64
