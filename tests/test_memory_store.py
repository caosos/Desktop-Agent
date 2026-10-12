"""Private memory store, importer and retrieval (da-c6d1e57152 §A–§E): file safety, containers, atomic records,
correction supersedes with history kept, expiry, family-vs-finance retrieval, backup/restore. Synthetic facts only."""
import os
import time
from pathlib import Path

import pytest

from desktop_agent.memory.importer import parse_markdown, seed_status, write_seed
from desktop_agent.memory.model import CONTAINERS, new_record
from desktop_agent.memory.retrieval import retrieve, route
from desktop_agent.memory.safety import UnsafeFile, check_private_file
from desktop_agent.memory.service import MemoryService
from desktop_agent.memory.store import MemoryStore

SEED = Path(__file__).parent / "fixtures" / "memory_seed_synthetic.md"


def test_private_files_are_0600_and_unsafe_files_are_refused(tmp_path):
    d = tmp_path / "private"
    s = MemoryStore(d)
    assert oct(d.stat().st_mode & 0o777) == "0o700" and oct(s.path.stat().st_mode & 0o777) == "0o600"
    loose = tmp_path / "loose.md"; loose.write_text("x"); os.chmod(loose, 0o644)
    with pytest.raises(UnsafeFile, match="group/other"):
        check_private_file(loose)
    link = tmp_path / "link.md"; link.symlink_to(loose)
    with pytest.raises(UnsafeFile, match="symlink"):
        check_private_file(link)
    big = tmp_path / "big.md"; big.write_bytes(b"x" * 300 * 1024); os.chmod(big, 0o600)
    with pytest.raises(UnsafeFile, match="KiB limit"):
        check_private_file(big)
    with pytest.raises(UnsafeFile, match="missing"):
        check_private_file(tmp_path / "absent.md")
    with pytest.raises(ValueError, match="KiB limit"):
        write_seed(d, b"y" * (256 * 1024 + 1))
    assert seed_status(d)["loaded"] is False
    bad = tmp_path / "p2"; bad.mkdir(); (bad / "memory.sqlite").write_bytes(b""); os.chmod(bad / "memory.sqlite", 0o644)
    assert "group/other" in MemoryService(bad).reason


def test_import_routes_sections_to_containers_and_reimport_confirms_or_retires(tmp_path):
    m = MemoryService(tmp_path / "private")
    r = m.import_markdown(SEED.read_bytes())
    assert r["created"] == 17 and r["retired"] == 0 and set(r["by_container"]) <= set(CONTAINERS)
    assert r["by_container"]["people_and_relationships"] == 2 and r["by_container"]["finances"] == 2 and r["by_container"]["health_and_wellbeing"] == 2
    st = m.status()
    assert st["loaded"] and st["seed"]["loaded"] and st["seed"]["sha256"] == r["seed_sha256"] and st["seed"]["bytes"] == 993 and st["backups"] == 1
    assert "text" not in st and not any("Zelphina" in str(v) for v in st.values())
    health = m.store.list(container="health_and_wellbeing")
    assert all(h["sensitivity"] == "high" and h["created_by"] == "import" and h["source_ref"].startswith("seed:") for h in health)
    keyed = next(x for x in m.store.list(container="people_and_relationships") if x["key"] == "Sister")
    assert keyed["text"].startswith("Zelphina") and "Sister:" not in keyed["text"]
    text = SEED.read_text().replace("- Dog: Biscuit, a beagle, 4 years old\n", "- Cat: Marmalade, orange, 2 years old\n")
    r2 = m.import_markdown(text.encode())
    assert r2["confirmed"] == 16 and r2["created"] == 1 and r2["retired"] == 1
    assert m.store.list(status="superseded")[0]["text"].startswith("Biscuit") and m.status()["seed"]["sha256"] == r2["seed_sha256"] != r["seed_sha256"]
    assert parse_markdown("# Finances (sensitive)\n- Rule: think a day\n")[0]["sensitivity"] == "high"


def test_family_question_retrieves_people_not_finance_or_health_and_project_question_gets_workflows(tmp_path):
    m = MemoryService(tmp_path / "private")
    m.import_markdown(SEED.read_bytes())
    fam = m.context_for("Where does my sister live these days?")
    assert "people_and_relationships" in fam["containers"] and "finances" not in fam["containers"] and "health_and_wellbeing" not in fam["containers"]
    assert "Zelphina" in fam["text"] and "4242" not in fam["text"] and "penicillin" not in fam["text"]
    assert "CORE:" in fam["text"] and "Orlando Fixtureman" in fam["text"] and "not live project status" in fam["text"]
    prj = m.context_for("How is the CAOSCare project going and what did we decide about deploys?")
    assert "Tuesdays" in prj["text"] and "integration branch" in prj["text"] and "4242" not in prj["text"]
    assert "health_and_wellbeing" in route("what's my allergy?") and "penicillin" in m.context_for("what's my allergy?")["text"]
    assert m.context_for("pause")["text"].startswith("PRIVATE MEMORY") and m.store.uses(fam["ids"][0])[0]["action"] == "retrieved"
    assert m.status()["last_retrieval_at"] is not None


def test_correction_supersedes_keeping_history_and_temporary_context_expires(tmp_path):
    s = MemoryStore(tmp_path / "private")
    old = s.add(new_record(container="people_and_relationships", subject="Zelphina", key="city", text="Zelphina lives in Tulsa"))
    new = s.supersede(old["id"], new_record(container="people_and_relationships", subject="Zelphina", key="city", text="Zelphina lives in Austin"))
    got = s.get(old["id"])
    assert got["status"] == "superseded" and got["superseded_by"] == new["id"] and s.get(new["id"])["supersedes"] == old["id"]
    assert [h["text"] for h in s.search("where does Zelphina live", ["people_and_relationships"])] == ["Zelphina lives in Austin"]
    assert [h["action"] for h in s.history(old["id"])] == ["created", "superseded"] and s.list(status="superseded")[0]["id"] == old["id"]
    tmp = s.add(new_record(container="current_working_context", subject="this week", text="finishing the investor deck", valid_until=time.time() + 0.2))
    assert retrieve(s, "what am I working on this week")["ids"] and s.get(tmp["id"])["status"] == "active"
    time.sleep(0.25)
    assert retrieve(s, "what am I working on this week")["ids"] == [] and s.get(tmp["id"])["status"] == "expired"
    assert s.add(new_record(container="people_and_relationships", subject="Zelphina", key="city", text="Zelphina lives in Austin"))["duplicate"]
    rev = s.revision(); b = s.backup()
    s.set_status(new["id"], "deleted")
    assert s.restore(b["name"])["records"] == rev["records"] and s.get(new["id"])["status"] == "active" and s.backups()[0]["name"] == b["name"]
    s.set_capture("finances", False)
    assert s.capture_on("finances") is False and s.capture_on("people_and_relationships") is True
