"""Denial/leakage proofs for the private memory (da-c6d1e57152 §J): fixture facts reach the model only as private system
context, and never the prompt text, aria:history, the control store, the events log, receipts, API state/status, the
worker prompt template or log output. Also the Memory Center API round trip. Synthetic facts only."""
import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from desktop_agent.control.api import build_app
from desktop_agent.control.config import RuntimeConfig
from desktop_agent.control.llm import Completion
from desktop_agent.control.service import Service

SEED = Path(__file__).parent / "fixtures" / "memory_seed_synthetic.md"
MARKERS = ("Zelphina", "Quorrington", "4242", "penicillin", "Orlando Fixtureman")


class SystemCapturingLLM:
    def __init__(self, answers): self.answers = list(answers); self.prompts = []; self.systems = []; self.backend = "fake"
    def model_for(self, mc): return "fake-model"
    def available(self, backend): return True
    def refresh_backend(self): return self.backend
    async def complete(self, prompt, schema, model_class="cloud_cheap", system=None, timeout=0, purpose=""):
        self.prompts.append(prompt); self.systems.append(system or "")
        return Completion(data=self.answers.pop(0), model="fake-model", backend="fake", cost_usd=None, input_tokens=5, output_tokens=5, duration_ms=1)


def _service(tmp_path: Path) -> Service:
    repo = tmp_path / "alpha"; repo.mkdir(); (repo / "S").write_text("s"); (repo / "A").write_text("a")
    y = tmp_path / "a.yaml"; y.write_text(f"name: alpha\nrepo_path: {repo}\nremote_url: x\nintegration_branch: main\nstart_here: S\nagents_file: A\n")
    s = Service(RuntimeConfig(data_dir=tmp_path / "d", workspaces_dir=tmp_path / "w", token_file=tmp_path / "cfg" / "t", project_files=[y]))
    s.scheduler.paused = True
    return s


def _no_marker(blob: str, where: str) -> None:
    for m in MARKERS:
        assert m not in blob, f"memory content leaked into {where}: {m}"


def test_memory_reaches_only_the_private_system_context(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    s = _service(tmp_path)
    assert s.memory.enabled and str(s.memory.dir).startswith(str(tmp_path / "cfg")) and s.memory.dir.name == "private"
    s.memory.import_markdown(SEED.read_bytes())
    llm = SystemCapturingLLM([{"reply": "Checking.", "action": {"name": "get_state", "args": {}}}, {"reply": "Your sister lives in Tulsa.", "action": None}])
    s.aria.llm = llm
    r = asyncio.run(s.aria.chat("Where does my sister live these days?"))
    assert r["reply"].startswith("Your sister") and len(llm.systems) == 2
    assert "Zelphina" in llm.systems[0] and "PRIVATE MEMORY" in llm.systems[0] and "4242" not in llm.systems[0] and "penicillin" not in llm.systems[0]
    for p in llm.prompts:
        _no_marker(p, "the prompt text")
    _no_marker(json.dumps(s.aria.history) + json.dumps(s.aria.transcript()) + json.dumps(s.store.get_kv("aria:history")), "aria history")
    con = sqlite3.connect(str(tmp_path / "d" / "control.sqlite"))
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        _no_marker(json.dumps([list(map(str, row)) for row in con.execute(f"SELECT * FROM {table}").fetchall()]), f"control store table {table}")
    for f in (tmp_path / "d").rglob("*"):
        if f.is_file() and f.suffix in (".jsonl", ".json", ".log", ".txt", ".md"):
            _no_marker(f.read_text(errors="replace"), f"data file {f.name}")
    _no_marker("\n".join(rec.getMessage() for rec in caplog.records), "log output")
    assert not any(str(s.memory.dir) in str(p) for p in (tmp_path / "d").rglob("*"))         # the private dir is not under the data dir
    with TestClient(build_app(s, "tok")) as c:
        h = {"Authorization": "Bearer tok"}
        for path in ("/v0/state", "/v0/aria/status", "/v0/aria/history", "/v0/receipts", "/v0/memory/status", "/v0/events?follow=false", "/v0/approvals"):
            resp = c.get(path, headers=h)
            assert resp.status_code == 200, path
            _no_marker(resp.text, f"API {path}")
        st = c.get("/v0/memory/status", headers=h).json()
        assert st["enabled"] and st["loaded"] and st["records"]["active"] == 17 and st["seed"]["loaded"] and len(st["seed"]["sha256"]) == 12
        assert st["conversation"]["retention"].startswith("for life") and st["dot_aria"]["synced"] is False
    tpl = (Path(__file__).resolve().parents[1] / "config" / "worker_prompt.md").read_text()
    src = "".join((Path(__file__).resolve().parents[1] / "desktop_agent" / "control" / n).read_text() for n in ("launcher.py", "contracts.py", "workspace.py", "sandbox.py"))
    assert "memory" not in tpl.lower() and "memory." not in src and "private_dir" not in src     # workers have no path to the private store


def test_memory_center_api_round_trip(tmp_path):
    s = _service(tmp_path)
    with TestClient(build_app(s, "tok")) as c:
        h = {"Authorization": "Bearer tok"}
        assert c.get("/memory").status_code == 200 and "Memory Center" in c.get("/memory").text and "loadStatus" in c.get("/memory.js").text
        assert c.get("/v0/memory/status").status_code == 401
        r = c.post("/v0/memory/import", json={"markdown": SEED.read_text()}, headers=h).json()
        assert r["created"] == 17 and r["chunks"] == 17
        people = c.get("/v0/memory/records?container=people_and_relationships", headers=h).json()["records"]
        sis = next(x for x in people if x["key"] == "Sister")
        fixed = c.patch(f"/v0/memory/records/{sis['id']}", json={"text": "Zelphina Quorrington, lives in Austin, calls on Sundays", "correct": True}, headers=h).json()
        assert fixed["supersedes"] == sis["id"] and c.get(f"/v0/memory/records/{sis['id']}", headers=h).json()["record"]["status"] == "superseded"
        why = c.get(f"/v0/memory/records/{fixed['id']}", headers=h).json()
        assert why["supersedes"]["id"] == sis["id"] and [x["action"] for x in why["history"]][0] == "created"
        new = c.post("/v0/memory/records", json={"container": "routines_and_workflows", "subject": "Fridays", "text": "Invoices go out on Friday afternoon"}, headers=h)
        assert new.status_code == 201 and new.json()["created_by"] == "owner_explicit"
        assert c.get("/v0/memory/records?q=invoices", headers=h).json()["records"][0]["id"] == new.json()["id"]
        assert c.delete(f"/v0/memory/records/{new.json()['id']}", headers=h).json()["status"] == "deleted"
        assert c.post(f"/v0/memory/records/{new.json()['id']}/restore", headers=h).json()["status"] == "active"
        s.memory.after_turn("I think my dog Biscuit is maybe 5 now", "t1")
        pend = c.get("/v0/memory/records?status=pending", headers=h).json()["records"]
        assert len(pend) == 1 and c.post(f"/v0/memory/records/{pend[0]['id']}/approve", headers=h).json()["status"] == "active"
        old = pend[0]["tags"][0].split(":")[1]
        assert c.get(f"/v0/memory/records/{old}", headers=h).json()["record"]["status"] == "superseded"
        cap = c.post("/v0/memory/capture", json={"container": "finances", "on": False}, headers=h).json()["capture"]
        assert cap["finances"] is False and cap["people_and_relationships"] is True
        assert c.post("/v0/memory/import", json={"markdown": "x" * (256 * 1024 + 1)}, headers=h).status_code == 422
        b = c.get("/v0/memory/export", headers=h).json()
        assert b["kind"].endswith("bundle") and len(b["records"]) >= 19
        assert c.post("/v0/memory/import-bundle", json={"bundle": b}, headers=h).json()["unchanged"] == len(b["records"])
        bk = c.post("/v0/memory/backup", headers=h).json()
        assert bk["name"] in [x["name"] for x in c.get("/v0/memory/backups", headers=h).json()["backups"]]
        assert c.post("/v0/memory/restore", json={"name": bk["name"]}, headers=h).json()["records"] >= 19
        assert c.get("/v0/memory/records?container=nope", headers=h).status_code == 400
