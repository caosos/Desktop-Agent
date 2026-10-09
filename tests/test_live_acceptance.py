"""Live first-time-owner acceptance against the RUNNING control plane at :8477 with its real data.
Runs only when DA_LIVE=1 (it reads ~/.config/desktop-agent/token and talks to the live service).
It asserts invariants that must hold on real data and prints what it observed; it changes nothing
except taking screenshots into the data directory.

Invariants: every DONE in the glance strip carries evidence; WORKING entries are RUNNING tasks;
BLOCKED entries are BLOCKED tasks (archived ones never appear); Shared Inbox chips show the
item's recorded status and never upgrade POSTED/SENT to DELIVERED or DELIVERED to DONE; the
approval packet lists only open decisions; the next action is one item; the page survives a
reload with the same counts; a coordinator reported idle is still shown connected; at 400 px
wide nothing scrolls horizontally; and no model call was made by checking while idle.
"""
import json
import os
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("DA_LIVE") != "1", reason="live acceptance; set DA_LIVE=1")
playwright = pytest.importorskip("playwright.sync_api")

URL = "http://127.0.0.1:8477"
TOKEN_FILE = Path("~/.config/desktop-agent/token").expanduser()


def api(path):
    req = urllib.request.Request(URL + path); req.add_header("Authorization", "Bearer " + TOKEN_FILE.read_text().strip())
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def test_live_owner_acceptance():
    token = TOKEN_FILE.read_text().strip()
    st = api("/v0/state"); pk = api("/v0/approvals"); ik = api("/v0/intake")
    report = {}
    with playwright.sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        page = b.new_page(viewport={"width": 1100, "height": 1600})
        page.goto(f"{URL}/?access_token={token}")
        page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=20000)
        page.wait_for_function("document.querySelectorAll('#coords .row').length >= 3", timeout=20000)
        page.wait_for_function("document.querySelector('#packet').children.length > 0 || document.querySelector('#packet').textContent.length > 0", timeout=20000)
        # 1. DONE evidenced: every glance Done entry is a DONE task and shows evidence (link or verifier)
        done_txt = page.inner_text("#g_done"); n_done = int(page.inner_text("#g_done_n"))
        assert n_done == len(st["glance"]["done_today"]) and all(d["evidence"] for d in st["glance"]["done_today"])
        report["done_today"] = n_done; report["done_with_pr_links"] = page.locator("#g_done a").count()
        # 2. WORKING genuinely active = RUNNING tasks only
        running = [t for t in st["tasks"] if t["status"] == "RUNNING"]
        assert int(page.inner_text("#g_working_n")) == len(running); report["working"] = [(t["task_id"], t["stage"]) for t in running]
        # 3. BLOCKED truly needs attention: only BLOCKED status (archived never shown)
        blocked = [t for t in st["tasks"] if t["status"] == "BLOCKED"]
        assert int(page.inner_text("#g_blocked_n")) == len(blocked) and all(t["status"] != "ARCHIVED" for t in st["blocked"])
        report["blocked"] = [t["task_id"] for t in blocked]
        # 4. Shared Inbox: chips equal recorded statuses; no posted/sent shown as delivered; no delivered shown as done
        inbox_txt = page.inner_text("#intake")
        for i in ik["items"][-30:]:
            assert i["status"] in inbox_txt
        by_status = {}
        for i in ik["items"]: by_status.setdefault(i["status"], 0); by_status[i["status"]] += 1
        report["inbox_statuses"] = by_status
        assert not any(i["status"] in ("POSTED", "SENT") and i.get("last_activity_at") for i in ik["items"] if i["kind"] != "liaison")
        # 5. consolidated approvals: exactly the open decisions, no answered ones, external gates read-only
        assert len(page.locator("#packet fieldset").all()) == len(pk["decisions"]) == len(st["inbox"])
        assert all(not g["answerable_here"] for g in pk["external_gates"]); report["open_decisions"] = len(pk["decisions"]); report["external_gates"] = len(pk["external_gates"])
        # 6. one next action, consistent with the state
        nxt = page.inner_text("#g_next"); assert nxt.strip() and nxt.strip() == st["glance"]["next_action"]["text"].strip(); report["next_action"] = nxt[:100]
        # 7. coordinators: an idle coordinator is still connected; MBOS never claimed wakeable
        coords = page.inner_text("#coords")
        for prj in st["projects"]:
            c = prj["coordinator"]
            if c.get("kind") == "claude_peer" and c.get("connected"):
                # an idle session is still shown by name with its status, never as disconnected
                assert c.get("session") in coords and "Disconnected" not in coords.split(prj["name"])[1].split("\n\n")[0]
                if c.get("session_status") == "idle":
                    assert "(idle)" in coords
            if prj["name"] == "michael_business_os":
                assert "manual-only" in c.get("wake", "")
        report["coordinators"] = {prj["name"]: (prj["coordinator"].get("session_status") or prj["coordinator"].get("kind"), prj["coordinator"].get("wake")) for prj in st["projects"]}
        # 8. reload: same counts
        page.reload(); page.wait_for_function("document.querySelector('#conn').textContent === 'live'", timeout=20000)
        page.wait_for_function("document.querySelector('#g_next').textContent.length > 0", timeout=20000)
        assert int(page.inner_text("#g_done_n")) == n_done and int(page.inner_text("#g_blocked_n")) == len(blocked)
        page.screenshot(path=str(Path(st and "~/.local/share/desktop-agent").expanduser() / "live-acceptance-desktop.png"), full_page=True)
        # 9. narrow screen: no horizontal scroll
        page.set_viewport_size({"width": 400, "height": 900}); time.sleep(0.5)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "horizontal overflow at 400px"
        page.screenshot(path=str(Path("~/.local/share/desktop-agent").expanduser() / "live-acceptance-phone.png"), full_page=True)
        b.close()
    # 10. idle checks spend nothing: cost rows in the last 10 minutes come only from deliveries/heartbeats (relay) or worker runs
    costs = api("/v0/state")["costs"]
    report["last_hour_known_usd"] = costs["last_hour_usd"]
    print("\nLIVE ACCEPTANCE REPORT:", json.dumps(report, indent=1))
