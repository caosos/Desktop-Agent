"""Research report → PDF on this host, with no new software: a small Markdown subset → HTML, then the installed
Chrome renders it headless (`--print-to-pdf`). Probe 2026-10-11: 13 KB from a one-paragraph page.

The renderer never fabricates: a missing browser, a timeout or an empty file is a failure result, not a PDF.
Design: docs/RESEARCH_BRIEF.md (task B).
"""
from __future__ import annotations

import hashlib
import html
import re
import shutil
import subprocess
import time
from pathlib import Path

CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
RENDER_TIMEOUT_SEC = 60
_INLINE = [(re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
           (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
           (re.compile(r"\[(\d+)\]"), r"<sup>[\1]</sup>")]
_CSS = ("body{font:12pt/1.45 Georgia,serif;margin:28mm 22mm;color:#111}h1{font-size:20pt;margin:0 0 6pt}"
        "h2{font-size:14pt;margin:18pt 0 6pt;border-bottom:1px solid #999}p.meta{color:#555;font-size:10pt}"
        "code{font:10pt monospace;background:#f3f3f3;padding:0 2pt}sup{font-size:8pt}ol,ul{padding-left:1.4em}"
        "li{margin:3pt 0}footer{margin-top:24pt;font-size:9pt;color:#555;border-top:1px solid #ccc;padding-top:6pt}")


def chrome_binary() -> str | None:
    return next((p for c in CHROME_CANDIDATES if (p := shutil.which(c))), None)


def _inline(text: str) -> str:
    out = html.escape(text)
    for rx, rep in _INLINE:
        out = rx.sub(rep, out)
    return out


def markdown_to_html(md: str, footer: str = "") -> str:
    """Headings, paragraphs, numbered and bulleted lists, inline code, bold and [n] citation marks. Nothing else
    is interpreted; unknown syntax is shown as text (escaped), never dropped."""
    body: list[str] = []
    para: list[str] = []
    list_tag: str | None = None

    def flush_para() -> None:
        if para:
            body.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            body.append(f"</{list_tag}>")
            list_tag = None

    for raw in md.splitlines():
        line = raw.rstrip()
        m_h = re.match(r"^(#{1,3})\s+(.*)$", line)
        m_ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        m_ul = re.match(r"^\s*[-*]\s+(.*)$", line)
        if not line:
            flush_para(); close_list(); continue
        if m_h:
            flush_para(); close_list()
            body.append(f"<h{len(m_h.group(1))}>{_inline(m_h.group(2))}</h{len(m_h.group(1))}>"); continue
        if m_ol or m_ul:
            flush_para()
            tag = "ol" if m_ol else "ul"
            if list_tag != tag:
                close_list(); body.append(f"<{tag}>"); list_tag = tag
            body.append(f"<li>{_inline((m_ol or m_ul).group(1))}</li>"); continue
        if line.startswith("job:") and not para and body and body[-1].startswith("<h1>"):
            body.append(f"<p class=\"meta\">{_inline(line)}</p>"); continue
        close_list(); para.append(line.strip())
    flush_para(); close_list()
    foot = f"<footer>{_inline(footer)}</footer>" if footer else ""
    return f"<!doctype html><html><head><meta charset=\"utf-8\"><style>{_CSS}</style></head><body>{''.join(body)}{foot}</body></html>"


def render_pdf(md_path: Path, pdf_path: Path, footer: str = "", timeout: int = RENDER_TIMEOUT_SEC) -> dict:
    """Render one Markdown report to PDF. Returns {"ok", "pdf_path", "bytes", "sha256", "renderer"} or
    {"ok": False, "reason"}; never raises for the expected failures (no browser, timeout, empty output)."""
    binary = chrome_binary()
    if not binary:
        return {"ok": False, "reason": "no Chrome/Chromium binary on PATH; PDF rendering unavailable"}
    md_path, pdf_path = Path(md_path), Path(pdf_path)
    if not md_path.is_file():
        return {"ok": False, "reason": f"report not found: {md_path}"}
    html_path = pdf_path.with_suffix(".html")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(markdown_to_html(md_path.read_text(encoding="utf-8"), footer), encoding="utf-8")
    argv = [binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}", html_path.as_uri()]
    t0 = time.time()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"renderer timed out after {timeout}s", "renderer": binary}
    data = pdf_path.read_bytes() if pdf_path.is_file() else b""
    if proc.returncode != 0 or not data.startswith(b"%PDF"):
        return {"ok": False, "reason": f"renderer exit {proc.returncode}, {len(data)} bytes: {(proc.stderr or '')[-200:].strip()}",
                "renderer": binary}
    return {"ok": True, "pdf_path": str(pdf_path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "renderer": binary, "seconds": round(time.time() - t0, 2)}
