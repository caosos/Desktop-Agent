from pathlib import Path

from desktop_agent.control.events import Actor, Event, EventType as ET, Provenance
from desktop_agent.control.metrics import choose_class, effective_ceiling, feedback, outcomes
from desktop_agent.control.router import LADDER
from desktop_agent.control.store import Store


def _task(store: Store, tid: str, status: str, ttype: str, mclass: str, attempt: int = 1, usd: float | None = None):
    store.save_task(tid, "g", "p", status, {"task_type": ttype, "model_class": mclass, "attempt": attempt}, "h")
    if usd is not None:
        store.add_cost(tid, "w", "m", usd)


def _ev(store: Store, tid: str, t: ET, **payload):
    store.append_event(Event(type=t.value, task_id=tid, payload=payload,
                             provenance=Provenance(actor=Actor.CONTROL.value, source="test", evidence=["x"])))


def test_outcomes_feedback_and_choice(tmp_path: Path):
    s = Store(tmp_path)
    for i in range(3):
        _task(s, f"d{i}", "DONE", "docs", "cloud_cheap", usd=0.5); _ev(s, f"d{i}", ET.VERIFY_PASSED)
    _task(s, "c1", "DONE", "code", "cloud_strong", usd=2.0); _ev(s, "c1", ET.VERIFY_PASSED)
    for i in range(3):
        _task(s, f"f{i}", "FAILED", "code", "cloud_cheap", attempt=2); _ev(s, f"f{i}", ET.VERIFY_FAILED, bounds_violation=False)
    _task(s, "b1", "BLOCKED", "code", "cloud_strong"); _ev(s, "b1", ET.VERIFY_FAILED, bounds_violation=True)
    _task(s, "u1", "DONE", "code", "cloud_strong")       # subscription run: no cost row
    _task(s, "r1", "RUNNING", "code", "cloud_strong")
    st = outcomes(s)
    assert st[("docs", "cloud_cheap")]["success_rate"] == 1.0 and st[("code", "cloud_cheap")]["success_rate"] == 0.0
    assert ("code", "cloud_strong") in st and st[("code", "cloud_strong")]["total"] == 3
    fb = feedback(s)
    assert fb["verified_tasks"] == 5 and fb["known_usd"] == 3.5 and fb["verified_per_dollar"] == round(5 / 3.5, 2)
    assert fb["unknown_cost_tasks"] == ["u1"] and fb["verified_per_hour"] > 0
    assert fb["verifications"] == 8 and fb["conflict_rate"] == round(1 / 8, 2) and fb["retry_rate"] == round(3 / 10, 2)
    # routing: docs proposed strong → steps down to cheap on evidence; code proposed cheap → steps up
    assert choose_class("docs", "cloud_strong", st, LADDER)[0] == "cloud_cheap"
    assert choose_class("code", "cloud_cheap", st, LADDER)[0] == "cloud_strong"
    assert choose_class("config", "cloud_cheap", st, LADDER) == ("cloud_cheap", None)   # no samples: keep
    assert effective_ceiling(2, fb) == (2, None)
    assert effective_ceiling(2, {"verifications": 5, "conflict_rate": 0.6})[0] == 1
    assert effective_ceiling(1, {"verifications": 5, "conflict_rate": 0.6})[0] == 1
