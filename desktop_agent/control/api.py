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

from .events import Actor, Event, EventType as ET, Provenance
from .service import Service, describe

PANEL = Path(__file__).resolve().parents[2] / "panel" / "index.html"


class GoalIn(BaseModel):
    project: str | None = None          # may be omitted when plan=true; the planner identifies it
    text: str = Field(min_length=3, max_length=4000)
    plan: bool = True                   # plain-language request → planner; false = explicit single contract
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


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reset: bool = False


class IntakeStatusIn(BaseModel):
    status: str
    note: str = Field(default="", max_length=1000)


class IntakeActionIn(BaseModel):
    action: str
    note: str = Field(default="", max_length=4000)


class DirectionIn(BaseModel):
    project: str
    text: str = Field(min_length=3, max_length=6000)


class AskIn(BaseModel):
    question: str = Field(min_length=5, max_length=1000)
    options: list[str] = []
    why: str = Field(min_length=5, max_length=2000)
    recommendation: str | None = None
    task_id: str | None = None
    goal_id: str | None = None


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
        explicit = bool(body.owned_area) or body.acceptance_tests is not None or not body.plan
        try:
            if not explicit:
                res = await service.plan_goal(text=body.text, source=source(request), project_hint=body.project,
                                              worker_adapter=body.worker_adapter)
            elif not body.project:
                raise ValueError("project is required for an explicit contract")
            else:
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

    @app.get("/v0/intake")
    async def intake(_: str = Depends(auth)):
        return service.intake.summary()

    @app.post("/v0/intake/poll")
    async def intake_poll(_: str = Depends(auth)):
        return await service.intake.poll_once()

    @app.post("/v0/directions", status_code=201)
    async def direction(body: DirectionIn, request: Request, _: str = Depends(auth)):
        """Owner → coordinator: posted to the project's intake issue, then ingested and delivered."""
        try:
            return await service.intake.send_direction(body.project, body.text, source(request))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/v0/intake/{item_id}/action")
    async def intake_action(item_id: str, body: IntakeActionIn, request: Request, _: str = Depends(auth)):
        try:
            return await service.intake.owner_action(item_id, body.action, body.note, source(request))
        except KeyError:
            raise HTTPException(404, "no such instruction")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/v0/intake/{item_id}/status")
    async def intake_status(item_id: str, body: IntakeStatusIn, request: Request, _: str = Depends(auth)):
        item = service.store.get_intake(item_id)
        if not item:
            raise HTTPException(404, "no such instruction")
        if body.status not in ("ACKNOWLEDGED", "WORKING", "BLOCKED", "DONE"):
            raise HTTPException(400, "status must be ACKNOWLEDGED|WORKING|BLOCKED|DONE")
        service.intake._set(item, body.status, f"{source(request)}: {body.note}", [source(request), body.note[:100]])
        return service.store.get_intake(item_id)

    @app.get("/v0/aria/greeting")
    async def aria_greeting(_: str = Depends(auth)):
        return service.aria.greeting()

    @app.post("/v0/aria/chat")
    async def aria_chat(body: ChatIn, _: str = Depends(auth)):
        if body.reset:
            service.aria.reset()
        return await service.aria.chat(body.text)

    @app.post("/v0/decisions", status_code=201)
    async def ask(body: AskIn, request: Request, _: str = Depends(auth)):
        return service.ask_owner(question=body.question, options=body.options, why=body.why, source=source(request),
                                 recommendation=body.recommendation, task_id=body.task_id, goal_id=body.goal_id)

    @app.post("/v0/tasks/{task_id}/archive")
    async def archive_task(task_id: str, body: IntakeStatusIn, request: Request, _: str = Depends(auth)):
        """Coordinator/owner: a BLOCKED or FAILED task that no longer needs attention (with a reason)."""
        row = service.store.get_task(task_id)
        if not row:
            raise HTTPException(404, "no such task")
        if row["status"] not in ("BLOCKED", "FAILED"):
            raise HTTPException(400, "only BLOCKED or FAILED tasks can be archived")
        service.store.set_task_status(task_id, "ARCHIVED", {"reason": f"{source(request)}: {body.note or 'archived'}"})
        service.store.append_event(Event(type=ET.CONTROL.value, task_id=task_id, payload={"action": "archived", "note": body.note[:200]},
                                         provenance=Provenance(actor=Actor.HUMAN.value if source(request) in ("panel", "widget:aria") else Actor.CONTROL.value,
                                                               source=source(request), evidence=[body.note[:100] or "archived"])))
        return service.store.get_task(task_id)

    @app.post("/v0/decisions/{decision_id}")
    async def decide(decision_id: str, body: DecisionIn, request: Request, _: str = Depends(auth)):
        try:
            return await service.answer_decision(decision_id, body.answer, source(request))
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
