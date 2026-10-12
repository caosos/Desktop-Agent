"""HTTP surface for the one owner budget: the Daily maximum paid API spend (owner directive da-8708eb5ec7)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field


class MaxIn(BaseModel):
    daily_max_usd: float = Field(ge=0, le=10000)


class BillingIn(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    usd: float = Field(ge=0)
    reference: str = Field(min_length=1, max_length=120)      # the provider's invoice / usage-export line id
    day: str | None = None
    covers: list[str] = []


def paid_routes(service, auth) -> APIRouter:
    router = APIRouter()
    paid = service.paid

    @router.get("/v0/paid-budget")
    async def get_budget(_: str = Depends(auth)):
        return paid.summary()

    @router.post("/v0/paid-budget")
    async def set_budget(body: MaxIn, request: Request, _: str = Depends(auth)):
        try:
            paid.set_maximum(body.daily_max_usd, request.headers.get("X-Source", "panel"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return paid.summary()

    @router.post("/v0/paid-budget/billing", status_code=201)
    async def billing(body: BillingIn, _: str = Depends(auth)):
        rec = paid.record_billing(body.provider, body.usd, body.reference, body.day, body.covers)
        return {"recorded": rec is not None, "duplicate": rec is None, **paid.summary()}

    return router
