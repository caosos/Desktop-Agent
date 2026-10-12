"""Receipt event bridge (da-de4a88e609): one PR comment per source receipt, exact prefix, no repeats, transport only."""
import asyncio
from pathlib import Path

from desktop_agent.control import intake as intake_mod
from desktop_agent.control.bridge import Bridge, render
from desktop_agent.control.intake import Intake, IntakeSource, item_id_for
from desktop_agent.control.store import Store
from tests.test_intake import FakeGH, comment

SCHEMA = ("project: desktop_agent\ntask: da-x bounded task\nresponsible: coordinator\nstage: building\nevidence: tests and queue row\n"
          "eta: 2026-10-11T22:00:00-05:00\neta_basis: tests plus one restart\nnext_check_at: 2026-10-11T21:30:00-05:00\nacceptance: gate\nblocker: NONE")


class Rig:
    def __init__(self, tmp_path: Path, pr=77):
        self.store = Store(tmp_path); self.gh = FakeGH(); self.posted = []; intake_mod.gh_api = self.gh
        async def deliver(src, item): return {"ok": True, "status": "DELIVERED", "note": "n", "evidence": ["e"]}
        async def post(path, fields): self.posted.append((path, fields["body"])); return {"html_url": f"https://x/pr#{len(self.posted)}"}
        self.src = IntakeSource(project="desktop_agent", repo="x/y", issues=[117], coordinator={"kind": "claude_peer", "session_cwd": "/n"}, bridge_pr=pr)
        self.intake = Intake(self.store, [self.src], deliver, poll_sec=1); self.intake.bridge = Bridge(self.store, post, "abc123def456")
        self.iid = item_id_for("x/y", 117, "issue", 9001); asyncio.run(self.intake.poll_once())

    def say(self, cid, first, extra=""):
        self.gh.comments.append(comment(cid, "o", f"<!-- da:coordinator -->\n{first} {self.iid}\n{extra}", f"2026-10-12T01:{cid % 100:02d}:00Z"))
        asyncio.run(self.intake.poll_once())


def test_each_receipt_is_mirrored_once_with_the_exact_prefix_and_fields(tmp_path):
    r = Rig(tmp_path)
    r.say(7001, "ACK", SCHEMA)
    assert len(r.posted) == 1 and r.posted[0][0] == "repos/x/y/issues/77/comments"
    body = r.posted[0][1]
    assert body.startswith("DESKTOP AGENT RECEIPT: ACK\n") and "next_check_at: 2026-10-11T21:30:00-05:00" in body and "blocker: NONE" in body
    assert "abc123def456" in body and "host observation: UNKNOWN" in body and "task: da-x bounded task" in body
    assert "project: desktop_agent" in body and "responsible: coordinator" in body and "stage: building" in body and "acceptance: gate" in body
    for _ in range(3): asyncio.run(r.intake.poll_once())                            # replays of the same comment
    assert len(r.posted) == 1
    r.say(7002, "WORKING", SCHEMA); r.say(7003, "DONE", "done"); r.say(7004, "BLOCKED", "needs the owner")
    assert [b.split("\n")[0] for _, b in r.posted] == ["DESKTOP AGENT RECEIPT: ACK", "DESKTOP AGENT RECEIPT: WORKING", "DESKTOP AGENT RECEIPT: DONE", "DESKTOP AGENT RECEIPT: NEED"]


def test_restart_and_unchanged_state_mirror_nothing_more(tmp_path):
    r = Rig(tmp_path)
    r.say(7001, "WORKING", SCHEMA)
    r.say(7002, "WORKING", SCHEMA.replace("building", "testing"))                    # a new receipt id with the same state: still one per source receipt
    n = len(r.posted)
    r2 = Rig.__new__(Rig); r2.store = r.store; r2.posted = []; r2.gh = r.gh; intake_mod.gh_api = r.gh
    async def post(path, fields): r2.posted.append(path); return {}
    r2.intake = Intake(r.store, [r.src], lambda *a: None, poll_sec=1); r2.intake.bridge = Bridge(r.store, post)    # service restart over the same store
    asyncio.run(r2.intake.poll_once()); asyncio.run(r2.intake.poll_once())
    assert r2.posted == [] and n >= 1


def test_ack_and_working_in_one_comment_is_one_mirror_with_the_stronger_label(tmp_path):
    r = Rig(tmp_path)
    r.gh.comments.append(comment(7010, "o", f"<!-- da:coordinator -->\nACK {r.iid}\nWORKING {r.iid}\n{SCHEMA}", "2026-10-12T01:10:00Z")); asyncio.run(r.intake.poll_once())
    assert len(r.posted) == 1 and r.posted[0][1].startswith("DESKTOP AGENT RECEIPT: WORKING")


def test_without_a_configured_pr_nothing_is_posted_and_a_failed_post_never_blocks_the_intake(tmp_path):
    r = Rig(tmp_path, pr=None); r.say(7001, "ACK", SCHEMA)
    assert r.posted == [] and r.store.get_intake(r.iid)["status"] == "ACKNOWLEDGED"
    r2 = Rig(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    async def boom(path, fields): raise RuntimeError("github down")
    r2.intake.bridge = Bridge(r2.store, boom)
    r2.say(7001, "WORKING", SCHEMA)
    assert r2.store.get_intake(r2.iid)["status"] == "WORKING" and not r2.store.get_kv("bridge:7001")        # not marked mirrored: nothing was posted
    assert any(e.payload.get("action") == "bridge_failed" for e in r2.store.events())


def test_render_states_unknown_rather_than_inventing_fields():
    out = render("ACK", [{"item_id": "da-1"}], "ACK da-1", "u", "", "t")
    assert "eta: not stated" in out and "revision UNKNOWN" in out and out.startswith("DESKTOP AGENT RECEIPT: ACK")


def test_item_ids_are_not_repeated_when_one_comment_carries_two_status_lines():
    it = {"item_id": "da-1"}
    assert "(da-1)" in render("WORKING", [it, it], "ACK da-1\nWORKING da-1", "u", "h", "t")


def test_a_host_reader_gives_a_timestamped_observation_and_a_failing_one_stays_unknown(tmp_path):
    r = Rig(tmp_path)
    r.intake.bridge.host = lambda item: ({"session": "caoscare-1-8c", "session_status": "busy", "service_pid": 7, "service_started": "t0", "loaded_head": "abc", "repo_head": "abc", "tested_vs_loaded": "same revision"}, "LIVE")
    r.say(7001, "ACK", SCHEMA)
    assert "host observation (" in r.posted[0][1] and "caoscare-1-8c busy" in r.posted[0][1] and "same revision" in r.posted[0][1]
    def boom(item): raise RuntimeError("x")
    r.intake.bridge.host = boom; r.say(7002, "WORKING", SCHEMA)
    assert "host observation: UNKNOWN" in r.posted[1][1]


def test_acks_of_supervisor_reports_are_not_mirrored_but_owner_and_work_items_are(tmp_path):
    r = Rig(tmp_path)
    r.store.save_intake({**r.store.get_intake(r.iid), "item_id": "da-0a0b0c0d0e", "gh_id": 5, "kind": "comment", "body": "<!-- da:canonical-checkin source=x -->\nreport", "status": "DELIVERED"})
    r.gh.comments.append(comment(7050, "o", "<!-- da:coordinator -->\nACK da-0a0b0c0d0e\nDONE da-0a0b0c0d0e", "2026-10-12T01:50:00Z")); asyncio.run(r.intake.poll_once())
    assert r.posted == [] and r.store.get_intake("da-0a0b0c0d0e")["status"] == "DONE"                     # state still advances; only the mirror is skipped
    r.say(7051, "ACK", SCHEMA)
    assert len(r.posted) == 1
