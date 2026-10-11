"""HTTP surface for Ask Aria research jobs (docs/RESEARCH_BRIEF.md §6)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
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

    @router.post("/v0/research/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request, _: str = Depends(auth)):
        try:
            return await jobs.cancel(job_id, request.headers.get("X-Source", "panel"))
        except KeyError:
            raise HTTPException(404, "no such research job")

    return router
