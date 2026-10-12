"""Memory Center HTTP surface (owner-local, authenticated like every other route): content-free status, record listing
and search, correct/edit/delete/restore, approve/reject pending, per-container capture switches, provenance ("why"),
seed import (atomic, 0600), private bundle export/import, backup/restore. Request bodies are never logged; errors name
a rule, never a memory. Mounted by api.py."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .model import CONTAINERS, STATUSES, new_record
from .safety import SEED_MAX_BYTES, UnsafeFile

PAGE = Path(__file__).resolve().parents[2] / "panel" / "memory.html"
SCRIPT = Path(__file__).resolve().parents[2] / "panel" / "memory.js"


class RecordIn(BaseModel):
    container: str
    text: str = Field(min_length=1, max_length=2000)
    subject: str = Field(default="", max_length=200)
    key: str | None = Field(default=None, max_length=120)
    confidence: float = Field(default=0.9, ge=0, le=1)
    sensitivity: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    valid_until: float | None = None


class EditIn(BaseModel):
    text: str | None = Field(default=None, max_length=2000)
    subject: str | None = Field(default=None, max_length=200)
    key: str | None = Field(default=None, max_length=120)
    confidence: float | None = Field(default=None, ge=0, le=1)
    sensitivity: str | None = None
    tags: list[str] | None = None
    container: str | None = None
    valid_until: float | None = None
    correct: bool = False        # True: the edit is a correction → a new record supersedes this one (history kept)


class ImportIn(BaseModel):
    markdown: str = Field(min_length=1, max_length=SEED_MAX_BYTES)


class CaptureIn(BaseModel):
    container: str
    on: bool


class BundleIn(BaseModel):
    bundle: dict


class RestoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def memory_routes(service, auth) -> APIRouter:
    router = APIRouter()

    def mem():
        m = service.memory
        if not m.enabled:
            raise HTTPException(503, m.reason or "private memory disabled")
        return m

    @router.get("/memory", response_class=HTMLResponse)
    async def page():
        return PAGE.read_text() if PAGE.exists() else "<h1>Memory Center</h1><p>page missing</p>"

    @router.get("/memory.js", response_class=PlainTextResponse)
    async def script():
        return PlainTextResponse(SCRIPT.read_text() if SCRIPT.exists() else "", media_type="text/javascript")

    @router.get("/v0/memory/status")
    async def status(_: str = Depends(auth)):
        return service.memory.status()

    @router.get("/v0/memory/records")
    async def records(container: str | None = None, status: str | None = None, q: str = "", limit: int = 100, offset: int = 0, _: str = Depends(auth)):
        if container and container not in CONTAINERS or status and status not in STATUSES:
            raise HTTPException(400, "unknown container or status")
        return {"records": mem().store.list(container, status, q[:200], min(max(limit, 1), 500), max(offset, 0))}

    @router.post("/v0/memory/records", status_code=201)
    async def create(body: RecordIn, _: str = Depends(auth)):
        try:
            rec = new_record(container=body.container, text=body.text, subject=body.subject, key=body.key, created_by="owner_explicit", source_type="memory_center",
                             source_ref="owner", confidence=body.confidence, sensitivity=body.sensitivity, tags=body.tags, valid_until=body.valid_until)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return mem().store.add(rec, actor="owner")

    @router.get("/v0/memory/records/{record_id}")
    async def one(record_id: str, _: str = Depends(auth)):
        st = mem().store
        rec = st.get(record_id)
        if rec is None:
            raise HTTPException(404, "no such memory")
        return {"record": rec, "history": st.history(record_id), "uses": st.uses(record_id),
                "supersedes": st.get(rec["supersedes"]) if rec.get("supersedes") else None,
                "superseded_by": st.get(rec["superseded_by"]) if rec.get("superseded_by") and rec["superseded_by"].startswith("m-") else rec.get("superseded_by")}

    @router.patch("/v0/memory/records/{record_id}")
    async def edit(record_id: str, body: EditIn, _: str = Depends(auth)):
        st = mem().store
        cur = st.get(record_id)
        if cur is None:
            raise HTTPException(404, "no such memory")
        fields = {k: v for k, v in body.model_dump().items() if k != "correct" and v is not None}
        try:
            if body.correct:
                rec = new_record(container=fields.get("container", cur["container"]), text=fields.get("text", cur["text"]), subject=fields.get("subject", cur["subject"]),
                                 key=fields.get("key", cur["key"]), created_by="owner_explicit", source_type="memory_center", source_ref=f"corrects:{record_id}",
                                 confidence=fields.get("confidence", 0.95), sensitivity=fields.get("sensitivity", cur["sensitivity"]),
                                 tags=fields.get("tags", cur["tags"]) + ["correction"], valid_until=fields.get("valid_until"))
                return st.supersede(record_id, rec, actor="owner")
            return st.update(record_id, fields, actor="owner")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.delete("/v0/memory/records/{record_id}")
    async def delete(record_id: str, _: str = Depends(auth)):
        return _status(record_id, "deleted", "deleted by owner")

    @router.post("/v0/memory/records/{record_id}/restore")
    async def restore_record(record_id: str, _: str = Depends(auth)):
        return _status(record_id, "active", "restored by owner")

    @router.post("/v0/memory/records/{record_id}/approve")
    async def approve(record_id: str, _: str = Depends(auth)):
        st = mem().store
        rec = st.get(record_id)
        if rec is None:
            raise HTTPException(404, "no such memory")
        old = next((t.split(":", 1)[1] for t in rec["tags"] if t.startswith("conflict:")), None)
        if old and st.get(old) and st.get(old)["status"] == "active":          # approving the new truth supersedes the old one, history kept
            st.set_status(old, "superseded", actor="owner", note=f"superseded by approved {record_id}")
            st.update(old, {"tags": st.get(old)["tags"]}, actor="owner")
        return _status(record_id, "active", "approved by owner")

    @router.post("/v0/memory/records/{record_id}/reject")
    async def reject(record_id: str, _: str = Depends(auth)):
        return _status(record_id, "deleted", "rejected by owner")

    def _status(record_id: str, status: str, note: str) -> dict:
        rec = mem().store.set_status(record_id, status, actor="owner", note=note)
        if rec is None:
            raise HTTPException(404, "no such memory")
        return rec

    @router.post("/v0/memory/capture")
    async def capture(body: CaptureIn, _: str = Depends(auth)):
        try:
            mem().store.set_capture(body.container, body.on)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"capture": {c: service.memory.store.capture_on(c) for c in CONTAINERS}}

    @router.post("/v0/memory/import")
    async def import_seed(body: ImportIn, _: str = Depends(auth)):
        """Owner-local import of the private Markdown seed: written atomically at 0600, parsed into records; metadata only comes back."""
        try:
            return mem().import_markdown(body.markdown.encode("utf-8"))
        except (UnsafeFile, ValueError) as exc:
            raise HTTPException(400, str(exc)[:200])

    @router.get("/v0/memory/export")
    async def export(_: str = Depends(auth)):
        return mem().export_bundle()

    @router.post("/v0/memory/import-bundle")
    async def import_bundle(body: BundleIn, _: str = Depends(auth)):
        try:
            return mem().import_bundle(body.bundle)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)[:200])

    @router.get("/v0/memory/backups")
    async def backups(_: str = Depends(auth)):
        return {"backups": mem().store.backups()}

    @router.post("/v0/memory/backup")
    async def backup(_: str = Depends(auth)):
        return mem().store.backup()

    @router.post("/v0/memory/restore")
    async def restore(body: RestoreIn, _: str = Depends(auth)):
        try:
            return mem().store.restore(body.name)
        except (UnsafeFile, ValueError) as exc:
            raise HTTPException(400, str(exc)[:200])

    return router
