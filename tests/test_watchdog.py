"""Watchdog: coordinator state/probe commands, one bounded wake per cooldown, liaison inbox polling and
ack files, liaison send, quota-reset resume. No network, no model."""
import asyncio
import base64
import json
import time
from pathlib import Path

from desktop_agent.control import intake as intake_mod, watchdog as wd_mod
from desktop_agent.control.intake import Intake, IntakeSource
from desktop_agent.control.store import Store
from desktop_agent.control.watchdog import Watchdog


class FakeDeliverer:
    def __init__(self): self.sent = []
    async def _claude_peer(self, src, item, backfill, prebuilt_text=None):
        self.sent.append(prebuilt_text or item["body"]); return {"ok": True, "status": "DELIVERED", "note": "peer", "evidence": ["session=x"]}
    async def __call__(self, src, item): return {"ok": True, "status": "RECEIVED", "note": "liaison", "evidence": ["l"]}


class FakeScheduler:
    paused = False; hold_reason = None


def _src(kind="claude_peer", **coord):
    c = {"kind": kind, **coord}
    return IntakeSource(project="caoscare", repo="x/y", issues=[117] if kind != "liaison" else [], coordinator=c)


def test_probe_wakes_once_per_cooldown(tmp_path: Path, monkeypatch):
    store = Store(tmp_path); deliv = FakeDeliverer()
    outputs = {"state": {"open_items": ["a"], "ready_unblocked": ["T-1"], "waiting_owner": []},
               "probe": {"verdict": "WAKE", "reasons": ["ready unblocked work: T-1"]}}
    async def fake_run(cmd, cwd, timeout=60): return outputs["probe" if "probe" in cmd else "state"]
    monkeypatch.setattr(wd_mod, "run_json_command", fake_run)
    src = _src(state_command="state.py", probe_command="probe.py", cwd="/w")
    it = Intake(store, [src], deliv, poll_sec=1)
    wd = Watchdog(store, it, deliv, FakeScheduler(), cooldown_sec=3600)
    r = asyncio.run(wd.tick())
    assert r["caoscare"]["wake"]["sent"] and deliv.sent and deliv.sent[0].startswith("DA-HEARTBEAT reasons=")
    row = store.coordinator_rows()["caoscare"]
    assert row["last_wake_kind"] == "heartbeat" and row["check"]["state"]["ready_unblocked"] == ["T-1"]
    assert store.receipts("coordinator", "caoscare")[0]["result_label"] == "verified"
    r2 = asyncio.run(wd.tick())
    assert r2["caoscare"]["wake"]["sent"] is False and len(deliv.sent) == 1          # same reasons: no second wake
    outputs["probe"] = {"verdict": "IDLE", "reasons": []}
    r3 = asyncio.run(wd.tick())
    assert "wake" not in r3["caoscare"] and len(deliv.sent) == 1                      # IDLE costs nothing
    outputs["probe"] = {"verdict": "WAKE", "reasons": ["intake item un-ACKed 30 min"]}
    asyncio.run(wd.tick()); assert len(deliv.sent) == 2                                 # new reasons: wake again


def test_liaison_inbox_and_acks(tmp_path: Path):
    store = Store(tmp_path); deliv = FakeDeliverer()
    inbox = [{"name": "ARIA-1.md", "path": "docs/messages/inbox/ARIA-1.md", "sha": "abc", "html_url": "u1"},
             {"name": "ARIA-2.md", "path": "docs/messages/inbox/ARIA-2.md", "sha": "def", "html_url": "u2"}]
    acks = [{"name": "ARIA-1.md"}]
    puts = []
    async def gh(path, timeout=60, method="GET", fields=None):
        if method == "PUT":
            puts.append((path, fields)); return {"content": {"html_url": "https://x/" + path}}
        if "contents/docs/messages/inbox?" in path: return inbox
        if "contents/docs/messages/acks?" in path: return acks
        if "contents/docs/messages/inbox/ARIA" in path:
            return {"encoding": "base64", "content": base64.b64encode(b"# Owner note\n**Date:** 2026-10-08 CDT\nbody").decode()}
        raise AssertionError(path)
    intake_mod.gh_api = gh; wd_mod.gh_api = gh
    src = _src("liaison", liaison={"branch": "liaison/x", "inbox_dir": "docs/messages/inbox", "ack_branch": "research/c", "ack_dir": "docs/messages/acks"})
    it = Intake(store, [src], deliv, poll_sec=1)
    wd = Watchdog(store, it, deliv, FakeScheduler()); it.watchdog = wd
    r = asyncio.run(it.poll_once())
    assert r["found"] == 2 and r["acked"] == 1
    items = store.list_intake()
    assert len(items) == 2 and all(i["kind"] == "liaison" for i in items)
    statuses = sorted(i["status"] for i in items); assert statuses == ["ACKNOWLEDGED", "RECEIVED"]
    assert asyncio.run(it.poll_once())["found"] == 0                                    # idempotent
    acks.append({"name": "ARIA-2.md"})
    assert asyncio.run(it.poll_once())["acked"] == 1 and all(i["status"] == "ACKNOWLEDGED" for i in store.list_intake())
    assert store.coordinator_rows()["caoscare"]["last_ack"]
    # direction to a liaison project writes one inbox file on the liaison branch
    d = asyncio.run(it.send_direction("caoscare", "Please confirm the heartbeat configuration.", "panel"))
    assert d["status"] == "SENT" and puts and puts[0][1]["branch"] == "liaison/x" and "ACK with" in base64.b64decode(puts[0][1]["content"]).decode()
    assert puts[0][0].startswith("repos/x/y/contents/docs/messages/inbox/ARIA-")


def test_resume_after_quota_reset(tmp_path: Path):
    store = Store(tmp_path); sched = FakeScheduler(); sched.paused = True; sched.hold_reason = "paused: provider limit (x)"
    wd = Watchdog(store, Intake(store, [], FakeDeliverer(), poll_sec=1), FakeDeliverer(), sched)
    store.set_kv("quota", {"resets_at": time.time() + 3600})
    wd._resume_after_quota_reset(); assert sched.paused
    store.set_kv("quota", {"resets_at": time.time() - 1})
    wd._resume_after_quota_reset(); assert not sched.paused and sched.hold_reason is None
    assert any(e.payload.get("action") == "resume_after_quota_reset" for e in store.events())


def test_rate_limit_capture_in_adapter():
    from desktop_agent.control.adapters.claude_headless import ClaudeHeadlessAdapter
    a = ClaudeHeadlessAdapter()
    a.parse_line(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1791487200, "rateLimitType": "five_hour",
                                                                          "unifiedWindows": {"five_hour": {"utilization": 0.28, "resetsAt": 1791487200}}}}))
    p = a.parse_line(json.dumps({"type": "result", "subtype": "success", "result": "STATUS: DONE\nCOMMIT: none", "total_cost_usd": 0.1, "usage": {}}))
    assert p.final["rate_limit"]["unifiedWindows"]["five_hour"]["utilization"] == 0.28
