"""Automatic memory capture (da-c6d1e57152 §F, §H) and the private bundle (§I): a clear durable fact is saved, chatter is
not, an uncertain conflict stays pending, an explicit correction supersedes, temporary context expires, automation
candidates are stored but never scheduled, sensitive inferred facts wait for the owner. Synthetic facts only."""
import time
from pathlib import Path

from desktop_agent.memory.capture import analyse, is_chatter
from desktop_agent.memory.service import MemoryService

SEED = Path(__file__).parent / "fixtures" / "memory_seed_synthetic.md"
ZERO = {"committed": 0, "pending": 0, "superseded": 0, "confirmed": 0, "ignored": 0}


def _mem(tmp_path):
    m = MemoryService(tmp_path / "private")
    m.import_markdown(SEED.read_bytes())
    return m


def test_clear_fact_saved_chatter_rejected_uncertain_conflict_pending(tmp_path):
    m = _mem(tmp_path)
    assert m.after_turn("Remember that my accountant's office closes at 4pm on Fridays", "t1")["committed"] == 1
    for chatter in ("thanks, what's running now?", "pause", "yes", "How is the kiosk doing?", "show me the blocked items"):
        assert is_chatter(chatter) and analyse(chatter) == [] and m.after_turn(chatter, "t2") == ZERO
    r = m.after_turn("I think my dog Biscuit is maybe 5 now", "t3")
    assert r["pending"] == 1 and r["committed"] == 0
    pend = m.store.list(status="pending")
    assert len(pend) == 1 and any(t.startswith("conflict:") for t in pend[0]["tags"])
    assert "ONE PENDING CONFLICT" in m.context_for("how old is my dog?")["text"]
    assert m.after_turn("I prefer imperial units actually", "t4")["pending"] == 1
    assert m.status()["records"]["pending"] == 2 and m.status()["last_capture_at"] is not None and m.last_turn["pending"] == 1


def test_explicit_correction_supersedes_and_only_current_truth_is_retrieved(tmp_path):
    m = _mem(tmp_path)
    assert m.after_turn("Remember that my sister Zelphina moved to Austin last month", "t1")["superseded"] == 1
    lines = [l for l in m.context_for("Where does my sister live?")["text"].splitlines() if "Zelphina" in l]
    assert len(lines) == 1 and "Austin" in lines[0] and "Tulsa" not in lines[0]
    old = m.store.list(status="superseded")[0]
    assert "Tulsa" in old["text"] and old["superseded_by"] and m.store.get(old["superseded_by"])["supersedes"] == old["id"]
    assert m.after_turn("Actually my sister Zelphina lives in Denver now", "t2")["superseded"] == 1
    assert "Denver" in m.context_for("Where does my sister live?")["text"] and m.status()["records"]["superseded"] == 2


def test_temporary_context_expires_automation_is_only_a_candidate_and_sensitive_inferred_waits(tmp_path):
    m = _mem(tmp_path)
    assert m.after_turn("I'm working on the investor deck this week", "t1")["committed"] == 1
    rec = m.store.list(container="current_working_context")[0]
    assert rec["valid_until"] and rec["valid_until"] > time.time() and "investor deck" in m.context_for("what am I doing this week?")["text"]
    m.store.update(rec["id"], {"valid_until": time.time() - 1})
    assert "investor deck" not in m.context_for("what am I doing this week?")["text"] and m.store.get(rec["id"])["status"] == "expired"
    r = m.after_turn("Remind me every Monday to check the kiosk logs", "t2")
    cand = m.store.list(container="automation_candidates")
    assert r["pending"] == 1 and cand[0]["status"] == "pending" and "candidate" in cand[0]["tags"]
    assert m.after_turn("I always pay the bank loan on the 3rd", "t3")["pending"] == 1
    m.store.set_capture("preferences_and_dislikes", False)
    assert m.after_turn("I love espresso in the afternoon", "t4")["ignored"] == 1
    m.store.set_capture("preferences_and_dislikes", True)
    assert m.after_turn("I love espresso in the afternoon", "t5")["committed"] == 1
    assert m.after_turn("I love espresso in the afternoon", "t6")["confirmed"] == 1


def test_private_bundle_round_trip_and_dot_aria_reported_separate(tmp_path):
    a = _mem(tmp_path / "a"); b = MemoryService(tmp_path / "b" / "private")
    a.after_turn("Remember that the kiosk vendor's support line is open 9 to 5", "t1")
    bundle = a.export_bundle()
    assert bundle["kind"].endswith("bundle") and bundle["records"] and bundle["revision"]["sha"] == a.status()["revision"]["sha"]
    r = b.import_bundle(bundle)
    assert r["created"] == len(bundle["records"]) and b.status()["revision"]["sha"] == a.status()["revision"]["sha"]
    assert b.import_bundle(bundle)["unchanged"] == len(bundle["records"])
    assert b.status()["dot_aria"]["synced"] is False and b.status()["dot_aria"]["last_reconciled_bundle"] == bundle["bundle_id"]
    try:
        b.import_bundle({"kind": "other"})
    except ValueError as exc:
        assert "bundle" in str(exc)
