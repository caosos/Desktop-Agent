"""Slack ingress decision logic: genuine owner messages only, one ACK per event, routing through existing paths, no network."""
import asyncio

from desktop_agent.control.slack_ingress import ACK_MARK, SlackIngress, classify, route_for
from desktop_agent.control.store import Store

CH, ME = "C0C81K9QNGK", "U0C82H6G9NZ"


def ev(text="DESKTOP AGENT: hello", ts="1.1", **kw):
    return {"type": "message", "channel": CH, "user": ME, "text": text, "ts": ts, **kw}


class FakeIntake:
    def __init__(self): self.sent = []
    async def send_direction(self, project, text, source):
        if project == "michael_business_os": raise ValueError("no coordinator")
        self.sent.append((project, text, source)); return {"item_id": f"da-{project[:4]}"}


class Rig:
    def __init__(self, tmp_path):
        self.store, self.intake, self.posts = Store(tmp_path), FakeIntake(), []
        async def post(ch, thread, text): self.posts.append((ch, thread, text))
        self.ing = SlackIngress(self.store, self.intake, post, CH, ME)
    def run(self, e): return asyncio.run(self.ing.handle(e))


def test_only_genuine_owner_messages_are_accepted():
    assert classify(ev(), CH, ME)[0] and classify(ev(thread_ts="1.0"), CH, ME)[0] and classify(ev(subtype="thread_broadcast"), CH, ME)[0]
    for bad in (ev(bot_id="B1"), ev(app_id="A1"), ev(bot_profile={"name": "x"}), ev(subtype="bot_message"), ev(subtype="message_changed"), ev(user="U999"),
                ev(text=""), ev(text=f"{ACK_MARK}: received"), ev(channel="C-other"), {"type": "reaction_added"}, ev(subtype="channel_join")):
        assert not classify(bad, CH, ME)[0], bad


def test_one_ack_per_event_survives_redelivery_and_restart_and_never_acks_itself(tmp_path):
    r = Rig(tmp_path)
    assert r.run(ev("DESKTOP AGENT: TEST SLACK INGRESS — DESKTOP AGENT"))["handled"]
    assert len(r.posts) == 1 and r.posts[0][2].startswith(ACK_MARK) and "desktop_agent" in r.posts[0][2] and r.posts[0][1] == "1.1"
    for _ in range(3): assert not r.run(ev("DESKTOP AGENT: TEST SLACK INGRESS — DESKTOP AGENT"))["handled"]
    r2 = Rig.__new__(Rig); r2.store, r2.intake, r2.posts = r.store, FakeIntake(), []
    async def post(ch, thread, text): r2.posts.append(text)
    r2.ing = SlackIngress(r.store, r2.intake, post, CH, ME)                       # restart over the same store
    assert not r2.run(ev("DESKTOP AGENT: TEST SLACK INGRESS — DESKTOP AGENT"))["handled"] and r2.posts == []
    assert not r.run(ev(r.posts[0][2], ts="2.2"))["handled"] and len(r.posts) == 1   # our own ACK echoed back: no loop


def test_routing_labels_use_the_shared_inbox_and_keep_projects_isolated(tmp_path):
    assert route_for("caoscare: x")[1] == ["caoscare"] and route_for("Deal Sniffer: x")[1] == ["michael_business_os"] and route_for("DOT ARIA: x") == ("DOT ARIA", [])
    assert route_for("all projects: x")[0] == "ALL PROJECTS" and route_for("hello")[0] is None and route_for("DESKTOP AGENTS: x")[0] is None
    r = Rig(tmp_path)
    out = r.run(ev("CAOSCARE: please look", ts="3.3", thread_ts="3.0"))
    assert out["items"] == ["da-caos"] and r.intake.sent == [("caoscare", "CAOSCARE: please look", "slack")] and r.posts[0][1] == "3.0"   # replied in the thread
    out = r.run(ev("ALL PROJECTS: status", ts="4.4"))                              # one project has no coordinator: the others still get it, ACK says so
    assert [p for p, _, _ in r.intake.sent[1:]] == ["desktop_agent", "caoscare"] and out["errors"] and "Not delivered: michael_business_os" in r.posts[1][2]
    r.run(ev("DOT ARIA: note", ts="5.5")); assert len(r.intake.sent) == 3 and "no project work started" in r.posts[2][2]


def test_visibility_question_gets_a_direct_answer_not_a_status_dump(tmp_path):
    r = Rig(tmp_path); out = r.run(ev("can anyone see this message?", ts="6.6")); assert r.run(ev("anyone listening??", ts="6.7"))["handled"] and "yes, I can see this message" in r.posts[1][2]
    assert out["handled"] and r.intake.sent == [] and "yes, I can see this message" in r.posts[0][2] and "desktop_agent" not in r.posts[0][2]


def test_every_handled_event_has_a_receipt_with_provenance_and_unlabelled_is_recorded_only(tmp_path):
    r = Rig(tmp_path); r.run(ev("just thinking", ts="7.7"))
    rec = r.store.receipts("slack", "7.7")[0]
    assert rec["actor"] == "control" and rec["source"] == "slack_ingress" and f"channel={CH}" in rec["evidence"] and rec["after_state"]["label"] is None
    assert r.intake.sent == [] and "No routing label" in r.posts[0][2]
