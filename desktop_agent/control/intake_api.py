"""HTTP surface for the owner-instruction intake: summary, manual poll, directions to a coordinator, owner actions on
an item and coordinator status posts. Mounted by api.py; the Service holds the state."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field


class IntakeStatusIn(BaseModel):
    status: str
    note: str = Field(default="", max_length=1000)


class IntakeActionIn(BaseModel):
    action: str
    note: str = Field(default="", max_length=4000)


class DirectionIn(BaseModel):
    project: str
    text: str = Field(min_length=3, max_length=6000)


def source(request: Request) -> str:
    return request.headers.get("X-Source", "panel")


def intake_routes(service, auth) -> APIRouter:
    router = APIRouter()

    @router.get("/v0/intake")
    async def intake(_: str = Depends(auth)):
        return service.intake.summary()
    
    @router.post("/v0/intake/poll")
    async def intake_poll(_: str = Depends(auth)):
        return await service.intake.poll_once()
    
    @router.post("/v0/directions", status_code=201)
    async def direction(body: DirectionIn, request: Request, _: str = Depends(auth)):
        """Owner → coordinator: posted to the project's intake issue, then ingested and delivered."""
        try:
            return await service.intake.send_direction(body.project, body.text, source(request))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    
    @router.post("/v0/intake/{item_id}/action")
    async def intake_action(item_id: str, body: IntakeActionIn, request: Request, _: str = Depends(auth)):
        try:
            return await service.intake.owner_action(item_id, body.action, body.note, source(request))
        except KeyError:
            raise HTTPException(404, "no such instruction")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    
    @router.post("/v0/intake/{item_id}/status")
    async def intake_status(item_id: str, body: IntakeStatusIn, request: Request, _: str = Depends(auth)):
        item = service.store.get_intake(item_id)
        if not item:
            raise HTTPException(404, "no such instruction")
        if body.status not in ("ACKNOWLEDGED", "WORKING", "BLOCKED", "DONE"):
            raise HTTPException(400, "status must be ACKNOWLEDGED|WORKING|BLOCKED|DONE")
        service.intake._set(item, body.status, f"{source(request)}: {body.note}", [source(request), body.note[:100]])
        return service.store.get_intake(item_id)

    return router
