"""HTTP surface for the ETA-driven checks: list them, or arm one directly with the receipt fields (the same schema and
validation as a coordinator's GitHub receipt). Mounted by api.py."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .checks_receipt import validate


class ArmIn(BaseModel):
    item_id: str
    status: str = "WORKING"
    project: str
    task: str
    responsible: str
    stage: str
    evidence: str
    eta: str
    eta_basis: str
    next_check_at: str
    acceptance: str
    blocker: str = "NONE"


def checks_routes(service, auth) -> APIRouter:
    router = APIRouter()
    checks = service.watchdog.checks

    @router.get("/v0/checks")
    async def list_checks(_: str = Depends(auth)):
        return {"loaded_head": checks.loaded_head, "checks": checks.list()}

    @router.post("/v0/checks", status_code=201)
    async def arm(body: ArmIn, _: str = Depends(auth)):
        item = service.store.get_intake(body.item_id)
        if not item:
            raise HTTPException(404, "no such instruction item")
        fields = {k: v for k, v in body.model_dump().items() if k not in ("item_id", "status")}
        problems, _norm = validate(fields, time.time())
        if problems:
            raise HTTPException(400, "; ".join(problems))
        text = "\n".join(f"{k}: {v}" for k, v in fields.items())
        rec = checks.observe(item, body.status.upper(), text, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "api")
        return rec

    return router
