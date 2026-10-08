"""Step 1 tests: events, stage derivation, store, receipts, contracts, project loader."""
from pathlib import Path

import pytest

from desktop_agent.control.contracts import compile_contract, TaskContract
from desktop_agent.control.events import Actor, Event, EventType, Provenance, Stage, derive_stage
from desktop_agent.control.project import ProjectPackage
from desktop_agent.control.receipts import ReceiptError, write_receipt, chain, VERIFIED, UNVERIFIED
from desktop_agent.control.store import Store


def ev(t: EventType, task_id="t1", actor=Actor.CONTROL, **payload) -> Event:
    return Event(type=t.value, task_id=task_id, payload=payload,
                 provenance=Provenance(actor=actor.value, source="test", evidence=["unit"]))


def test_stage_derivation_sequence():
    seq = []
    assert derive_stage(seq) == Stage.PLANNING
    seq.append(ev(EventType.TASK_CREATED)); assert derive_stage(seq) == Stage.PLANNING
    seq.append(ev(EventType.WORKER_STARTED)); assert derive_stage(seq) == Stage.READING
    seq.append(ev(EventType.FILE_READ)); assert derive_stage(seq) == Stage.READING
    seq.append(ev(EventType.FILE_CHANGED)); assert derive_stage(seq) == Stage.BUILDING
    seq.append(ev(EventType.TEST_STARTED)); assert derive_stage(seq) == Stage.TESTING
    seq.append(ev(EventType.WORKER_FINISHED)); assert derive_stage(seq) == Stage.REVIEWING
    seq.append(ev(EventType.VERIFY_STARTED)); assert derive_stage(seq) == Stage.TESTING
    seq.append(ev(EventType.VERIFY_PASSED)); assert derive_stage(seq) == Stage.REVIEWING
    seq.append(ev(EventType.PUSHED)); assert derive_stage(seq) == Stage.INTEGRATING
    seq.append(ev(EventType.TASK_DONE)); assert derive_stage(seq) == Stage.DONE


def test_stage_blocked_and_waiting_owner():
    seq = [ev(EventType.WORKER_STARTED), ev(EventType.VERIFY_FAILED)]
    assert derive_stage(seq) == Stage.BLOCKED
    seq = [ev(EventType.WORKER_STARTED), ev(EventType.OWNER_DECISION_REQUESTED)]
    assert derive_stage(seq) == Stage.WAITING_OWNER
    seq.append(ev(EventType.OWNER_DECISION_RECORDED))
    assert derive_stage(seq) == Stage.READING


def test_store_events_roundtrip_and_jsonl(tmp_path: Path):
    s = Store(tmp_path)
    seen = []
    s.subscribe(seen.append)
    e1 = s.append_event(ev(EventType.TASK_CREATED))
    e2 = s.append_event(ev(EventType.WORKER_STARTED))
    assert e1.seq == 1 and e2.seq == 2
    got = s.events("t1")
    assert [g.type for g in got] == ["TASK_CREATED", "WORKER_STARTED"]
    assert got[0].provenance.evidence == ["unit"]
    assert s.events(since_seq=1)[0].id == e2.id
    assert len(seen) == 2
    lines = (tmp_path / "tasks" / "t1" / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_receipt_law(tmp_path: Path):
    s = Store(tmp_path)
    with pytest.raises(ReceiptError):
        write_receipt(s, subject_type="task", subject_id="t1", claim="x", actor=Actor.WORKER.value,
                      source="w", result_label=VERIFIED, evidence=["e"])
    with pytest.raises(ReceiptError):
        write_receipt(s, subject_type="task", subject_id="t1", claim="x", actor=Actor.CONTROL.value,
                      source="c", result_label=VERIFIED, evidence=[])
    r1 = write_receipt(s, subject_type="task", subject_id="t1", claim="tests pass", actor=Actor.WORKER.value,
                       source="claude_headless", result_label=UNVERIFIED, evidence=["final message"],
                       correlation_id="g1")
    r2 = write_receipt(s, subject_type="task", subject_id="t1", claim="tests pass", actor=Actor.CONTROL.value,
                       source="verifier", result_label=VERIFIED, evidence=["exit 0"], correlation_id="g1")
    c = chain(s, "task", "t1")
    assert [x["receipt_id"] for x in c] == [r1["receipt_id"], r2["receipt_id"]]
    assert c[1]["parent_receipt_id"] == r1["receipt_id"]
    assert s.receipts(correlation_id="g1")[0]["claim"] == "tests pass"


def test_tasks_decisions_costs(tmp_path: Path):
    s = Store(tmp_path)
    s.save_task("t1", "g1", "p", "READY", {"a": 1}, "h")
    s.set_task_status("t1", "RUNNING", {"x": 1})
    t = s.get_task("t1")
    assert t["status"] == "RUNNING" and t["contract"] == {"a": 1} and t["result"] == {"x": 1}
    assert s.list_tasks(status="RUNNING")[0]["task_id"] == "t1"
    s.save_decision("d1", "t1", "merge?", ["yes", "no"])
    assert s.open_decisions()[0]["options"] == ["yes", "no"]
    s.answer_decision("d1", "yes")
    assert s.open_decisions() == []
    s.add_cost("t1", "w1", "m", 0.5, 10, 5)
    assert s.cost_summary()["today_usd"] == 0.5
    s.remember("k", {"ok": 1})
    assert s.idempotent("k") == {"ok": 1}


def _project(tmp_path: Path) -> ProjectPackage:
    y = tmp_path / "p.yaml"
    y.write_text(
        "name: demo\nrepo_path: /tmp/demo\nremote_url: https://example.com/demo.git\n"
        "integration_branch: main\nstart_here: START.md\nagents_file: AGENTS.md\n"
        "ready_queue: docs/Q.md\ntest_command: ./test.sh\ntest_env: {PORT: '{port}'}\n"
        "shared_contract_paths: [core.py]\nworker_allowed_tools: [Read]\n"
    )
    return ProjectPackage.load(y)


def test_project_loader_and_contract(tmp_path: Path):
    p = _project(tmp_path)
    assert p.bootloader_files() == ["START.md", "AGENTS.md", "docs/Q.md"]
    assert p.test_env == {"PORT": "{port}"}
    c = compile_contract(project=p, objective="Fix the README typo", why_now="queue",
                         goal_id="g1", task_type="docs", owned_area=["README.md"],
                         model_class="cloud_cheap", budget_usd=1.0, max_turns=10, wall_clock_sec=600)
    assert c.task_id.startswith("fix-the-readme-typo-")
    assert c.read_list[:2] == ["START.md", "AGENTS.md"]
    assert c.acceptance_tests == ["./test.sh"]
    assert c.allowed_tools == ["Read"]
    assert c.branch_name() == f"agent/{c.task_id}"
    h = c.hash()
    c.result = {"anything": True}
    assert c.hash() == h, "result must not change the contract hash"
    assert TaskContract.from_dict(c.to_dict()).hash() == h


def test_project_loader_missing_field(tmp_path: Path):
    y = tmp_path / "bad.yaml"
    y.write_text("name: x\n")
    with pytest.raises(ValueError):
        ProjectPackage.load(y)
