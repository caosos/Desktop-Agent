"""Owner-instruction intake with a fake GitHub and a fake deliverer: ingestion, idempotency, cursor,
ACK read-back, unacknowledged flagging, replay after restart, control-plane coordinator."""
import asyncio
import time
from pathlib import Path

from desktop_agent.control import intake as intake_mod
from desktop_agent.control.intake import Intake, IntakeSource, item_id_for
from desktop_agent.control.intake_delivery import delivery_text
from desktop_agent.control.store import Store

ISSUE = {"id": 9001, "user": {"login": "caosos"}, "body": "# Owner dispatch\nDo the thing.", "created_at": "2026-10-09T00:35:46Z",
         "html_url": "https://github.com/x/y/issues/117", "title": "OWNER DISPATCH"}


class FakeGH:
    def __init__(self):
        self.comments = []; self.posted = []; self.calls = 0
    async def __call__(self, path, timeout=60, method="GET", fields=None):
        self.calls += 1
        if method == "POST":
            self.posted.append(fields["body"]); return {}
        if path.endswith("/issues/117"):
            return ISSUE
        if "/comments" in path:
            since = path.split("since=")[1].split("&")[0]
            return [c for c in self.comments if c["created_at"] > since]
        raise AssertionError(path)


def comment(cid, login, body, created):
    return {"id": cid, "user": {"login": login}, "body": body, "created_at": created, "html_url": f"https://github.com/x/y/issues/117#issuecomment-{cid}"}


def make(store, gh, deliver, **kw):
    intake_mod.gh_api = gh
    src = IntakeSource(project="caoscare", repo="x/y", issues=[117], coordinator={"kind": "claude_peer", "session_cwd": "/nowhere"})
    return Intake(store, [src], deliver, poll_sec=1, **kw)


def test_ingest_deliver_ack_and_replay(tmp_path: Path):
    store = Store(tmp_path); gh = FakeGH(); delivered = []
    async def deliver(src, item):
        delivered.append(item["item_id"]); return {"ok": True, "status": "DELIVERED", "note": "peer", "evidence": ["session=x"], "comment": f"DELIVERED {item['item_id']}"}
    it = make(store, gh, deliver)
    r = asyncio.run(it.poll_once())
    assert r["found"] == 1 and r["delivered"] == 1
    iid = item_id_for("x/y", 117, "issue", 9001)
    assert store.get_intake(iid)["status"] == "DELIVERED" and delivered == [iid]
    assert gh.posted and "DELIVERED " + iid in gh.posted[0] and "desktop-agent-intake" in gh.posted[0]
    # a second poll with nothing new: no re-ingest, no re-delivery
    assert asyncio.run(it.poll_once())["found"] == 0 and delivered == [iid]
    # owner comment arrives → new item; coordinator ACK comment (marked) → status; our own tagged comment ignored;
    # a marked coordinator report without an item id is activity, not an instruction
    gh.comments += [comment(1, "caosos", "## New owner update\nMore work.", "2026-10-09T01:00:00Z"),
                    comment(2, "caosos", f"<!-- caos:coordinator -->\nACK {iid} — received and working", "2026-10-09T01:05:00Z"),
                    comment(3, "caosos", f"DELIVERED {iid} to session <!-- desktop-agent-intake -->", "2026-10-09T01:06:00Z"),
                    comment(5, "caosos", "<!-- caos:coordinator -->\nCoordinator report: logs correlated, nothing to ack", "2026-10-09T01:07:00Z")]
    r = asyncio.run(it.poll_once())
    iid2 = item_id_for("x/y", 117, "comment", 1)
    assert r["found"] == 1 and r["acked"] == 1 and store.get_intake(iid)["status"] == "ACKNOWLEDGED"
    assert store.get_intake(iid2)["status"] == "DELIVERED" and store.intake_cursor("x/y", 117) == "2026-10-09T01:07:00Z"
    assert store.get_intake(item_id_for("x/y", 117, "comment", 5)) is None
    assert it.summary()["coordinator_activity"]["caoscare"] == "2026-10-09T01:07:00Z"
    gh.comments.append(comment(4, "caosos", f"DONE {iid}: merged and verified", "2026-10-09T02:00:00Z"))
    asyncio.run(it.poll_once())
    assert store.get_intake(iid)["status"] == "DONE"
    # replay after restart: a fresh Intake over the same store sees nothing new and delivers nothing
    it2 = make(store, gh, deliver)
    assert asyncio.run(it2.poll_once())["found"] == 0 and len(delivered) == 2
    recs = store.receipts("instruction", iid)
    assert [x["result_label"] for x in recs] and all(x["actor"] == "control" for x in recs)
    s = it2.summary()
    assert s["counts"]["DONE"] == 1 and s["counts"]["DELIVERED"] == 1


def test_direction_roundtrip_actions_and_coordinator_question(tmp_path: Path):
    """Owner direction from the panel → posted → ingested without duplication → delivered; deny/defer; ASK-OWNER → decision."""
    store = Store(tmp_path); gh = FakeGH(); delivered = []; asked = []
    posted_comments = []
    async def gh_with_post(path, timeout=60, method="GET", fields=None):
        if method == "POST":
            cid = 500 + len(posted_comments)
            c = comment(cid, "caosos", fields["body"], f"2026-10-09T03:0{len(posted_comments)}:00Z")
            posted_comments.append(c)
            if "da:direction" in fields["body"]:
                gh.comments.append(c)            # the direction now exists on GitHub like any owner comment
            return c
        return await gh(path, timeout, method, fields)
    async def deliver(src, item):
        delivered.append(item["item_id"]); return {"ok": True, "status": "DELIVERED", "note": "peer", "evidence": ["s"]}
    intake_mod.gh_api = gh_with_post
    src = IntakeSource(project="caoscare", repo="x/y", issues=[117], coordinator={"kind": "claude_peer", "session_cwd": "/nowhere"})
    it = Intake(store, [src], deliver, poll_sec=1, ask_owner=lambda **kw: asked.append(kw))
    asyncio.run(it.poll_once())                                  # backlog: the issue body
    d = asyncio.run(it.send_direction("caoscare", "Please correlate the 8 ft wake tests.", "panel"))
    assert d["status"] == "SENT" and d["kind"] == "direction" and "da:direction" in posted_comments[0]["body"]
    r = asyncio.run(it.poll_once())
    d2 = store.get_intake(d["item_id"])
    assert r["found"] == 1 and d2["status"] == "DELIVERED" and d2["gh_id"] == 500 and delivered[-1] == d["item_id"]
    assert len([i for i in store.list_intake() if i["kind"] in ("direction", "comment")]) == 1, "no duplicate item for the posted direction"
    # owner actions
    denied = asyncio.run(it.owner_action(d["item_id"], "deny", "changed my mind", "panel"))
    assert denied["status"] == "DENIED"
    iid = item_id_for("x/y", 117, "issue", 9001)
    assert asyncio.run(it.owner_action(iid, "defer", "", "panel"))["status"] == "DEFERRED"
    before = len(delivered); asyncio.run(it.owner_action(iid, "transfer", "", "panel")); assert len(delivered) == before + 1
    # coordinator question → owner inbox
    gh.comments.append(comment(7, "caosos", "<!-- caos:coordinator -->\nWORKING " + iid + "\nASK-OWNER: May I close the kitchen test task?", "2026-10-09T04:00:00Z"))
    asyncio.run(it.poll_once())
    assert asked and asked[0]["question"].startswith("May I close") and asked[0]["source"] == "coordinator:caoscare"
    assert store.get_intake(iid)["status"] == "WORKING"
    assert it.summary()["sources"][0]["project"] == "caoscare"


def test_backlog_is_delivered_as_one_batch(tmp_path: Path):
    store = Store(tmp_path); gh = FakeGH(); batches = []
    gh.comments += [comment(1, "caosos", "## update one", "2026-10-09T01:00:00Z"), comment(2, "caosos", "## update two", "2026-10-09T01:01:00Z")]
    class D:
        async def __call__(self, src, item): raise AssertionError("single delivery must not be used for a backlog")
        async def batch(self, src, items):
            batches.append([i["item_id"] for i in items])
            return {"ok": True, "status": "DELIVERED", "note": "one peer message", "evidence": ["batch"], "comment": "DELIVERED " + ", ".join(i["item_id"] for i in items)}
    it = make(store, gh, D())
    r = asyncio.run(it.poll_once())
    assert r["found"] == 3 and r["delivered"] == 3 and len(batches) == 1 and len(batches[0]) == 3
    assert len(gh.posted) == 1 and all(i in gh.posted[0] for i in batches[0])
    assert all(store.get_intake(i)["status"] == "DELIVERED" for i in batches[0])


def test_delivery_failure_and_unacked_flag(tmp_path: Path):
    store = Store(tmp_path); gh = FakeGH()
    async def deliver(src, item): return {"ok": False, "note": "no live session", "evidence": ["x"]}
    it = make(store, gh, deliver, unacked_after_sec=0)
    asyncio.run(it.poll_once())
    iid = item_id_for("x/y", 117, "issue", 9001)
    item = store.get_intake(iid)
    assert item["status"] == "RECEIVED" and "no live session" in item["note"]
    time.sleep(0.01); asyncio.run(it.poll_once())
    assert store.get_intake(iid)["flagged"] == 1 and iid in it.summary()["unacknowledged"]
    assert any(e.type == "BLOCKED" and "unacknowledged" in e.payload.get("reason", "") for e in store.events())


def test_control_plane_coordinator_acks_itself(tmp_path: Path):
    from desktop_agent.control.config import RuntimeConfig
    from desktop_agent.control.intake_delivery import Deliverer
    store = Store(tmp_path); gh = FakeGH()
    intake_mod.gh_api = gh
    src = IntakeSource(project="desktop_agent", repo="x/y", issues=[117], coordinator={"kind": "control_plane"})
    cfg = RuntimeConfig(data_dir=tmp_path, workspaces_dir=tmp_path, token_file=tmp_path / "t")
    it = Intake(store, [src], Deliverer(cfg, store), poll_sec=1)
    asyncio.run(it.poll_once())
    iid = item_id_for("x/y", 117, "issue", 9001)
    assert store.get_intake(iid)["status"] == "ACKNOWLEDGED" and gh.posted and gh.posted[0].startswith(f"ACK {iid}")
    text = delivery_text(store.get_intake(iid))
    assert f"ACK {iid}" in text and "Do the thing." in text and "DA-INTAKE item=" in text


def _sessions(monkeypatch, records, alive=None, cmdlines=None):
    from desktop_agent.control import intake_delivery as idm
    monkeypatch.setattr(idm, "_session_records", lambda: records)
    monkeypatch.setattr(idm, "_alive", lambda pid: pid in (alive if alive is not None else {r["pid"] for r in records}))
    monkeypatch.setattr(idm, "_cmdline", lambda pid: (cmdlines or {}).get(pid, ["claude"]))
    return idm


def _rec(pid, name, cwd="/home/caoscare-1", kind="interactive", entrypoint="cli", sid=None, **extra):
    d = {"pid": pid, "name": name, "cwd": cwd, "status": "idle", "updatedAt": pid, "sessionId": sid or f"sid-{pid}"}
    if kind is not None: d["kind"] = kind
    if entrypoint is not None: d["entrypoint"] = entrypoint
    return {**d, **extra}


def _src(**coord):
    return IntakeSource(project="desktop_agent", repo="x/y", issues=[3], coordinator={"kind": "claude_peer", **coord})


def test_routing_pinned_name_then_sole_eligible_candidate_else_fail_closed(monkeypatch):
    """Stale pin + exactly one eligible interactive session in the directory → that one, saying so.
    Two eligible home-directory sessions → ambiguous, nothing routed (never "the newest")."""
    idm = _sessions(monkeypatch, [_rec(5, "caoscare-1-05", updatedAt=1), _rec(9, "caoscare-1-09", updatedAt=99)])
    src = _src(session_name="caoscare-1-20", session_cwd_exact="/home/caoscare-1")
    s, note = idm.Deliverer.resolve_for(src)
    assert s is None and "ambiguous: 2 eligible sessions" in note and "caoscare-1-05" in note and "caoscare-1-09" in note
    # the pinned name is live again: by name, regardless of the other session
    src.coordinator["session_name"] = "caoscare-1-09"
    s, note = idm.Deliverer.resolve_for(src)
    assert s["name"] == "caoscare-1-09" and s["resolved_by"] == "name" and s["session_id"] == "sid-9"
    # only one eligible session left: stale pin falls back to it, and the result says how
    idm = _sessions(monkeypatch, [_rec(5, "caoscare-1-05")])
    src.coordinator["session_name"] = "caoscare-1-20"
    s, note = idm.Deliverer.resolve_for(src)
    assert s["name"] == "caoscare-1-05" and s["resolved_by"] == "cwd (exact)" and s["pinned_name"] == "caoscare-1-20"
    assert "no eligible session at name caoscare-1-20" in note and "sole eligible session at cwd /home/caoscare-1 (exact)" in note
    # nothing configured beyond a dead name: None with the reason
    assert idm.Deliverer.resolve_for(_src(session_name="caoscare-1-20"))[0] is None


def test_routing_excludes_sdk_worker_and_metadata_less_sessions(monkeypatch):
    """A session record without kind/entrypoint, an SDK session, and a `claude -p` worker are never
    candidates, so they can neither receive commands nor make the directory ambiguous."""
    recs = [_rec(5, "caoscare-1-05"),
            _rec(6, "sdk-no-kind", kind=None, entrypoint="sdk"),
            _rec(7, "no-metadata", kind=None, entrypoint=None),
            _rec(8, "worker-print", kind="interactive", entrypoint="cli"),
            _rec(10, "dead", )]
    idm = _sessions(monkeypatch, recs, alive={5, 6, 7, 8}, cmdlines={8: ["claude", "-p", "do work", "--output-format", "stream-json"]})
    elig = {e["name"]: e for e in idm.eligible_sessions()}
    assert elig["caoscare-1-05"]["eligible"] and not elig["sdk-no-kind"]["eligible"] and not elig["no-metadata"]["eligible"]
    assert "headless/worker" in elig["worker-print"]["why"] and "dead" not in elig
    s, note = idm.Deliverer.resolve_for(_src(session_name="gone", session_cwd_exact="/home/caoscare-1"))
    assert s["name"] == "caoscare-1-05" and s["resolved_by"] == "cwd (exact)"
    # pinning a worker by name does not make it eligible either
    s, note = idm.Deliverer.resolve_for(_src(session_name="worker-print"))
    assert s is None and "worker-print rejected: headless/worker command line" in note


def test_coordinator_card_wording_when_no_name_is_pinned(monkeypatch, tmp_path):
    """A project configured with only a working directory must not read 'pinned name None is not live'."""
    from desktop_agent.control.service import Service
    from desktop_agent.control import intake_delivery as idm
    live = {"name": "caoscare-integration-b2", "pid": 1, "cwd": "/home/caoscare-1/CAOSCARE-INTEGRATION", "status": "busy", "updatedAt": 1, "tmux": None, "session_id": "s"}
    monkeypatch.setattr(idm, "_session_records", lambda: [{"pid": 1, "name": live["name"], "cwd": live["cwd"], "status": "busy", "updatedAt": 1, "sessionId": "s", "kind": "interactive", "entrypoint": "cli"}])
    monkeypatch.setattr(idm, "_alive", lambda pid: True); monkeypatch.setattr(idm, "_cmdline", lambda pid: ["claude"])
    svc = Service.__new__(Service)
    from desktop_agent.control.store import Store
    svc.store = Store(tmp_path)
    class Pkg: name = "caoscare"; intake = {"coordinator": {"kind": "claude_peer", "session_cwd": "/home/caoscare-1/CAOSCARE-INTEGRATION"}}
    c = svc.coordinator_state(Pkg())
    assert c["connected"] and "pinned name None" not in c["detail"] and "resolved by cwd (prefix) (no name pinned" in c["detail"]
    Pkg.intake = {"coordinator": {"kind": "claude_peer", "session_name": "gone-20", "session_cwd": "/home/caoscare-1/CAOSCARE-INTEGRATION"}}
    assert "pinned name gone-20 is not live; resolved by cwd (prefix)" in svc.coordinator_state(Pkg())["detail"]
