"""HTTP API v0 (IMPLEMENTATION_PLAN §3): the widget's and the panel's whole world."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .service import Service, describe

PANEL = Path(__file__).resolve().parents[2] / "panel" / "index.html"


class GoalIn(BaseModel):
    project: str
    text: str = Field(min_length=3, max_length=4000)
    task_type: str = "code"
    owned_area: list[str] = []
    model_class: str | None = None
    why_now: str = "owner instruction"
    acceptance_tests: list[str] | None = None
    expected_artifacts: list[str] | None = None
    budget_usd: float | None = None
    max_attempts: int = Field(default=3, ge=1, le=3)
    worker_adapter: str = "claude_headless"


class DecisionIn(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


class ControlIn(BaseModel):
    action: str
    task_id: str | None = None


def build_app(service: Service, token: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop_task = asyncio.create_task(service.run_forever())
        try:
            yield
        finally:
            loop_task.cancel()

    app = FastAPI(title="Desktop-Agent control plane", version="0.1.0", lifespan=lifespan)

    def auth(authorization: str | None = Header(default=None), access_token: str | None = None) -> str:
        supplied = None
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        elif access_token:
            supplied = access_token
        if not supplied or not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "missing or invalid token")
        return "owner"

    def source(request: Request) -> str:
        return request.headers.get("X-Source", "panel")

    @app.get("/", response_class=HTMLResponse)
    async def panel():
        return PANEL.read_text() if PANEL.exists() else "<h1>Desktop-Agent</h1><p>panel missing</p>"

    @app.get("/v0/health")
    async def health():
        return {"ok": True, "uptime_sec": round(time.time() - service.started_at)}

    @app.get("/v0/projects")
    async def projects(_: str = Depends(auth)):
        return {"projects": [p.summary() for p in service.projects.values()]}

    @app.get("/v0/state")
    async def state(_: str = Depends(auth)):
        return service.state()

    @app.get("/v0/events")
    async def events(request: Request, since: int = 0, task_id: str | None = None, follow: bool = True,
                     _: str = Depends(auth)):
        """SSE stream: replay from `since`, then follow live. `follow=false` returns the replay only."""
        async def gen():
            last = since
            for ev in service.store.events(task_id, since_seq=since):
                last = ev.seq or last
                yield _sse(ev)
            if not follow:
                return
            q = service.subscribe()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if task_id and ev.task_id != task_id:
                        continue
                    if ev.seq and ev.seq <= last:
                        continue
                    last = ev.seq or last
                    yield _sse(ev)
            finally:
                service.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/v0/goals", status_code=201)
    async def goals(body: GoalIn, request: Request, _: str = Depends(auth),
                    idempotency_key: str | None = Header(default=None)):
        if idempotency_key:
            prior = service.store.idempotent("goal:" + idempotency_key)
            if prior:
                return prior
        try:
            res = service.submit_goal(project=body.project, text=body.text, source=source(request),
                                      task_type=body.task_type, owned_area=body.owned_area, model_class=body.model_class,
                                      why_now=body.why_now, acceptance_tests=body.acceptance_tests,
                                      expected_artifacts=body.expected_artifacts, budget_usd=body.budget_usd,
                                      max_attempts=body.max_attempts, worker_adapter=body.worker_adapter)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if idempotency_key:
            service.store.remember("goal:" + idempotency_key, res)
        return res

    @app.get("/v0/tasks/{task_id}")
    async def task(task_id: str, _: str = Depends(auth)):
        view = await service.task_detail(task_id)
        if not view:
            raise HTTPException(404, "no such task")
        return view

    @app.post("/v0/decisions/{decision_id}")
    async def decide(decision_id: str, body: DecisionIn, request: Request, _: str = Depends(auth)):
        try:
            return service.answer_decision(decision_id, body.answer, source(request))
        except KeyError:
            raise HTTPException(404, "no such decision")

    @app.post("/v0/control")
    async def control(body: ControlIn, request: Request, _: str = Depends(auth)):
        try:
            return await service.control(body.action, body.task_id, source(request))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/v0/receipts")
    async def receipts(task_id: str | None = None, goal_id: str | None = None, _: str = Depends(auth)):
        if task_id:
            return {"receipts": service.store.receipts("task", task_id)}
        if goal_id:
            return {"receipts": service.store.receipts(correlation_id=goal_id)}
        return {"receipts": service.store.receipts()[-100:]}

    return app


def _sse(ev) -> str:
    d = ev.to_dict()
    d["text"] = describe(ev)
    return f"id: {ev.seq}\nevent: {ev.type}\ndata: {json.dumps(d, default=str)}\n\n"
