"""Consolidated approval packet: all open gates at once, one receipt per answer, DEFER hides without consent,
blank items untouched, external gates read-only; plus a browser pass through the packet card."""
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.events import EventType as ET
from desktop_agent.control.service import Service


def _service(tmp_path: Path) -> Service:
    repo = tmp_path / "alpha"; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
    (tmp_path / "a.yaml").write_text(f"name: alpha\nrepo_path: {repo}\nremote_url: x\nintegration_branch: main\nstart_here: S\nagents_file: A\n")
    s = Service(RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "t", project_files=[tmp_path / "a.yaml"]))
    s.scheduler.paused = True
    s.ask_owner(question="Approve the pilot?", options=["approve", "not yet"], why="unlock metered costs", source="test", recommendation="approve",
                scope="control-plane cheap calls only", resumes="planner on OpenAI")
    s.ask_owner(question="Anthropic key?", options=["yes", "no"], why="credits", source="test", recommendation="yes")
    s.ask_owner(question="Raise daily cap to $40?", options=["yes", "no"], why="more throughput", source="test", recommendation="no")
    s.store.save_coordinator_check("caoscare", {"checked_at": time.time(), "state": {"waiting_owner": ["release/deploy approval", "PR #67 adoption"]}})
    s.store.set_kv("gates:michael_business_os", {"gates": [{"gate": "Do you want the faster local tmux self-wake enabled as a stopgap? Recommendation: no.", "source": "docs/messages/acks/x.md", "url": "u"}], "scanned": ["x.md"]})
    return s


def test_packet_and_submit(tmp_path):
    s = _service(tmp_path)
    with TestClient(build_app(s, "tok")) as c:
        h = {"Authorization": "Bearer tok", "X-Source": "panel"}
        pk = c.get("/v0/approvals", headers=h).json()
        assert len(pk["decisions"]) == 3 and pk["decisions"][0]["scope"] == "control-plane cheap calls only" and pk["decisions"][0]["why"] == "unlock metered costs"
        assert [g["project"] for g in pk["external_gates"]] == ["caoscare", "caoscare", "michael_business_os"]
        assert all(g["answerable_here"] is False for g in pk["external_gates"]) and "security boundary" in pk["classifier_note"]
        ids = [d["decision_id"] for d in pk["decisions"]]
        r = c.post("/v0/approvals/submit", json={"answers": [{"decision_id": ids[0], "answer": "approve"}, {"decision_id": ids[1], "answer": "DEFER"}]}, headers=h).json()
        assert r["recorded"] == [ids[0]] and r["deferred"] == [ids[1]] and r["remaining"] == [ids[2]]       # blank third item untouched
        assert s.store.receipts("decision", ids[0])[-1]["claim"] == "owner answered: approve"
        assert all(x["result_label"] == "verified" and x["actor"] == "human" for x in s.store.receipts("decision", ids[0])[-1:])
        assert not [x for x in s.store.receipts("decision", ids[1]) if "answered" in x["claim"]]            # deferral produced no answer receipt
        assert [d["decision_id"] for d in s.store.open_decisions(include_deferred=True) if d["answer"] is None] == [ids[1], ids[2]]
        pk2 = c.get("/v0/approvals", headers=h).json()
        assert [d["decision_id"] for d in pk2["decisions"]] == [ids[2]] and pk2["deferred"] == [ids[1]]
        recorded = [e for e in s.store.events() if e.type == ET.OWNER_DECISION_RECORDED.value]
        assert len(recorded) == 1 and recorded[0].provenance.source == "panel"
        assert c.post("/v0/approvals/submit", json={"answers": [{"decision_id": "d-nope", "answer": "yes"}]}, headers=h).json()["errors"]


def test_packet_in_browser(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    import uvicorn
    s = _service(tmp_path)
    server = uvicorn.Server(uvicorn.Config(build_app(s, "tok"), host="127.0.0.1", port=8497, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    for _ in range(50):
        if server.started: break
        time.sleep(0.1)
    try:
        with playwright.sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception as exc:
                pytest.skip(f"no Chrome: {exc}")
            page = browser.new_page(viewport={"width": 1100, "height": 1800})
            page.goto("http://127.0.0.1:8497/?access_token=tok&view=details")        # the packet is on demand in the Owner view
            page.wait_for_function("document.querySelectorAll('#packet fieldset').length === 3", timeout=15000)
            assert "read-only" in page.inner_text("#ext_gates") and "(3)" in page.inner_text("#ext_gates")
            page.click("#ext_gates summary")                                       # external gates are collapsed by default
            assert "tmux self-wake" in page.inner_text("#ext_gates")
            ids = [d["decision_id"] for d in s.store.open_decisions()]
            page.check(f"#packet input[name='{ids[0]}'][value='approve']")
            page.check(f"#packet input[name='{ids[2]}'][value='DEFER']")
            page.click("#packet_submit")
            page.wait_for_function("document.querySelector('#packetmsg').textContent.includes('recorded')", timeout=15000)
            assert "1 recorded" in page.inner_text("#packetmsg") and "1 deferred" in page.inner_text("#packetmsg")
            page.wait_for_function("document.querySelectorAll('#packet fieldset').length === 1", timeout=15000)
            assert s.store.get_intake is not None and len([d for d in s.store.open_decisions()]) == 1
            browser.close()
    finally:
        server.should_exit = True; th.join(timeout=5)
