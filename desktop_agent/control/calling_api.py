"""HTTP surface for the SIMULATED calling loop (docs/CALLING_BRIEF.md §3, §5)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field


class CallIn(BaseModel):
    destination_alias: str
    purpose: str = Field(default="", max_length=200)
    command_id: str | None = None
    conversation_ref: str | None = None


def calling_routes(service, auth) -> APIRouter:
    router = APIRouter()
    calls = service.calling

    @router.post("/v0/calls", status_code=201)
    async def create_call(body: CallIn, request: Request, _: str = Depends(auth)):
        try:
            return calls.request(body.command_id, body.destination_alias, body.purpose, body.conversation_ref,
                                 request.headers.get("X-Source", "panel"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/v0/calls")
    async def list_calls(_: str = Depends(auth)):
        return {"calls": calls.list()}

    @router.get("/v0/calls/{call_id}")
    async def get_call(call_id: str, _: str = Depends(auth)):
        call = calls.get(call_id)
        if not call:
            raise HTTPException(404, "no such call")
        return call

    @router.post("/v0/calls/{call_id}/cancel")
    async def cancel_call(call_id: str, request: Request, _: str = Depends(auth)):
        try:
            return calls.cancel(call_id, request.headers.get("X-Source", "panel"))
        except KeyError:
            raise HTTPException(404, "no such call")

    return router
