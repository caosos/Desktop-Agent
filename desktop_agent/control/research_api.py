"""HTTP surface for Ask Aria research jobs (docs/RESEARCH_BRIEF.md §6)."""
from __future__ import annotations

import uuid

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field


class ResearchIn(BaseModel):
    question: str = Field(max_length=1000)
    project: str


def research_routes(service, auth) -> APIRouter:
    router = APIRouter()
    jobs = service.research

    @router.post("/v0/research", status_code=201)
    async def create_job(body: ResearchIn, request: Request, _: str = Depends(auth)):
        try:
            return jobs.request("api-" + uuid.uuid4().hex[:12], body.question, body.project, request.headers.get("X-Source", "panel"))
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/v0/research")
    async def list_jobs(_: str = Depends(auth)):
        return {"jobs": jobs.list()}

    @router.get("/v0/research/{job_id}")
    async def get_job(job_id: str, _: str = Depends(auth)):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such research job")
        return job

    def _published(job_id: str, kind: str) -> Path:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such research job")
        pub = job.get("published") or {}
        if kind == "pdf" and not (pub.get("pdf") or {}).get("ok"):
            raise HTTPException(404, f"no PDF for {job_id}: {(pub.get('pdf') or {}).get('reason', 'job not VERIFIED yet')}")
        path = Path((pub.get("pdf") or {}).get("pdf_path", "") if kind == "pdf" else pub.get("report", ""))
        if not path.is_file():
            raise HTTPException(404, f"no {kind} file for {job_id} ({'job not VERIFIED yet' if job['state'] != 'VERIFIED' else 'file missing'})")
        return path

    @router.get("/v0/research/{job_id}/report")
    async def get_report(job_id: str, _: str = Depends(auth)):
        return PlainTextResponse(_published(job_id, "report").read_text(encoding="utf-8"), media_type="text/markdown")

    @router.get("/v0/research/{job_id}/pdf")
    async def get_pdf(job_id: str, _: str = Depends(auth)):
        return FileResponse(_published(job_id, "pdf"), media_type="application/pdf", filename=f"{job_id}.pdf")

    @router.post("/v0/research/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request, _: str = Depends(auth)):
        try:
            return await jobs.cancel(job_id, request.headers.get("X-Source", "panel"))
        except KeyError:
            raise HTTPException(404, "no such research job")

    return router
