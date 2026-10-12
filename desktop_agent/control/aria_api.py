"""HTTP surface for the owner's conversation and approvals: widget pairing, the approval packet, Ask Aria
(greeting, status with the dictation disclosure, history, chat) and project drilldowns. Mounted by api.py."""
from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .events import Actor, Event, EventType as ET, Provenance
from .intake_api import source


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reset: bool = False
    client_id: str | None = Field(default=None, max_length=64)      # the browser's id for this send: the persisted owner turn carries it, so a reload shows it once


class LookIn(BaseModel):
    image_b64: str = Field(min_length=8, max_length=9_000_000)
    mime: str = "image/jpeg"
    question: str = Field(default="", max_length=1000)
    client_id: str | None = Field(default=None, max_length=64)
    captured_at: str = Field(default="", max_length=60)
    width: int = Field(default=0, ge=0, le=20000)
    height: int = Field(default=0, ge=0, le=20000)


class AnswerIn(BaseModel):
    decision_id: str
    answer: str = Field(min_length=1, max_length=2000)


class PacketIn(BaseModel):
    answers: list[AnswerIn] = Field(max_length=50)


class PairIn(BaseModel):
    code: str = Field(min_length=6, max_length=6)


def aria_routes(service, auth, token: str) -> APIRouter:
    router = APIRouter()

    pairing: dict[str, float] = {}
    
    @router.post("/v0/pair/new")
    async def pair_new(_: str = Depends(auth)):
        """Owner (already signed in on the panel) mints a one-time 6-character pairing code, valid 10 minutes,
        so the widget in another login can obtain the token without anyone handling it by hand."""
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        pairing[code] = time.time() + 600
        service.store.append_event(Event(type=ET.CONTROL.value, task_id=None, payload={"action": "pairing_code_issued", "expires_in_sec": 600},
                                         provenance=Provenance(actor=Actor.HUMAN.value, source="panel", evidence=["pairing"])))
        return {"code": code, "expires_in_sec": 600}
    
    @router.post("/v0/pair/claim")
    async def pair_claim(body: PairIn, request: Request):
        """No auth: the widget presents the code once; the code is consumed. Localhost only."""
        exp = pairing.pop(body.code.strip().upper(), None)
        if not exp or exp < time.time():
            raise HTTPException(403, "invalid or expired pairing code")
        service.store.append_event(Event(type=ET.CONTROL.value, task_id=None, payload={"action": "widget_paired", "client": request.client.host if request.client else None},
                                         provenance=Provenance(actor=Actor.HUMAN.value, source="widget", evidence=["pairing code consumed"])))
        return {"url": f"http://{service.cfg.api_host}:{service.cfg.api_port}", "token": token}
    
    @router.get("/v0/approvals")
    async def approvals(_: str = Depends(auth)):
        return service.approval_packet()
    
    @router.post("/v0/approvals/submit")
    async def approvals_submit(body: PacketIn, request: Request, _: str = Depends(auth)):
        return await service.submit_packet([a.model_dump() for a in body.answers], source(request))
    
    @router.get("/v0/aria/greeting")
    async def aria_greeting(_: str = Depends(auth)):
        return service.aria.greeting()
    
    @router.get("/v0/aria/status")
    async def aria_status(_: str = Depends(auth)):
        """Is a model configured and permitted for conversation, which, how served; plus the dictation disclosure."""
        return service.aria.status() | {"dictation": {
            "mode": "browser_speech",
            "disclosure": "Dictation uses your browser's speech recognition. In Chrome the audio is sent to Google's speech servers for "
                          "recognition; it is not processed on this computer and this panel stores no audio. The microphone is opened only "
                          "when you click the mic and closed when you click Stop. Text is never sent to Aria until you press Send.",
            "requirements": "Chrome (or another browser with SpeechRecognition); microphone permission granted on click; the page on localhost "
                            "or HTTPS (a localhost SSH tunnel qualifies).",
            "on_device_alternative": "not installed: an offline speech model would be new software on this host (one owner decision)",
            "metered_alternative": "widget/voice.py path (OpenAI transcription) stays off; OpenAI is disabled"}}
    
    @router.get("/v0/aria/history")
    async def aria_history(_: str = Depends(auth)):
        return {"turns": service.aria.transcript()}
    
    @router.get("/v0/projects/{name}/drilldown")
    async def project_drilldown(name: str, _: str = Depends(auth)):
        """Owner: one project's mission, coordinator vs workers, declared-vs-live roster, tasks, instructions, freshness."""
        d = service.drill.build(name)
        if not d:
            raise HTTPException(404, "no such project")
        return d
    
    @router.post("/v0/aria/chat")
    async def aria_chat(body: ChatIn, _: str = Depends(auth)):
        if body.reset:
            service.aria.reset()
        try:
            return await service.aria.chat(body.text, body.client_id)
        except Exception as exc:          # the owner turn and an error turn are already in the store; the client gets a classified, readable failure
            raise HTTPException(500, f"Ask Aria failed inside the control plane: {type(exc).__name__}: {str(exc)[:200]}")

    @router.post("/v0/aria/look")
    async def aria_look(body: LookIn, _: str = Depends(auth)):
        """Explicit owner request only: one screenshot in, one grounded explanation out. Looking only; no input is ever injected."""
        from .aria_look import look
        try:
            return await look(service, image_b64=body.image_b64, mime=body.mime, question=body.question, client_id=body.client_id or "",
                              captured_at=body.captured_at, width=body.width, height=body.height)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    return router
