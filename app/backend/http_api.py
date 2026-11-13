"""HTTP boundary for the Streamlit UI and LiveKit agent."""

from __future__ import annotations

import os
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, status
from livekit import api
from pydantic import BaseModel

from app.backend.api import Backend, get_backend
from app.backend.types import (
    ChatReply,
    ChoiceSelection,
    ConfirmationDecision,
    ConversationState,
)
from app.llm.base import LLMConfigError

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=False)

_AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "ecommerce-support-agent")


@dataclass(frozen=True)
class _ActiveSession:
    state: ConversationState
    room_name: str


class SessionRequest(BaseModel):
    customer_id: str


class DecisionRequest(BaseModel):
    action_id: str
    accepted: bool


class ChoiceRequest(BaseModel):
    prompt_id: str
    option_id: str


class TurnRequest(BaseModel):
    message: str | None = None
    decision: DecisionRequest | None = None
    choice: ChoiceRequest | None = None


class SessionTurnRequest(TurnRequest):
    session_id: str


class AgentTurnRequest(TurnRequest):
    room_name: str


class CloseSessionRequest(BaseModel):
    session_id: str


class SessionResponse(BaseModel):
    session_id: str
    url: str
    room_name: str
    token: str
    greeting: str


_sessions: dict[str, _ActiveSession] = {}
_session_by_room: dict[str, str] = {}
_sessions_lock = threading.RLock()

app = FastAPI(title="E-commerce support API")


def _configured_livekit() -> tuple[str, str, str]:
    names = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"LiveKit is not configured: missing {', '.join(missing)}.",
        )
    return (
        values["LIVEKIT_URL"],
        values["LIVEKIT_API_KEY"],
        values["LIVEKIT_API_SECRET"],
    )


def _configured_backend() -> Backend:
    try:
        return get_backend()
    except LLMConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The support backend is not configured.",
        ) from exc


def _create_room_token(
    *,
    api_key: str,
    api_secret: str,
    room_name: str,
    participant_identity: str,
) -> str:
    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(participant_identity)
        .with_name("Customer")
        .with_ttl(timedelta(minutes=15))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )


async def _dispatch_agent(
    *,
    url: str,
    api_key: str,
    api_secret: str,
    room_name: str,
) -> None:
    client = api.LiveKitAPI(url, api_key, api_secret)
    try:
        await client.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=_AGENT_NAME,
                room=room_name,
            )
        )
    finally:
        await client.aclose()


def _lookup_session(session_id: str) -> _ActiveSession:
    with _sessions_lock:
        active = _sessions.get(session_id)
    if active is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The support session is unavailable.",
        )
    return active


def _remove_session(session_id: str) -> _ActiveSession | None:
    with _sessions_lock:
        active = _sessions.pop(session_id, None)
        if active is not None:
            _session_by_room.pop(active.room_name, None)
    return active


def _turn_arguments(request: TurnRequest) -> tuple[
    str | None, ConfirmationDecision | None, ChoiceSelection | None
]:
    decision = (
        ConfirmationDecision(
            action_id=request.decision.action_id,
            accepted=request.decision.accepted,
        )
        if request.decision is not None
        else None
    )
    choice = (
        ChoiceSelection(
            prompt_id=request.choice.prompt_id,
            option_id=request.choice.option_id,
        )
        if request.choice is not None
        else None
    )
    return request.message, decision, choice


def _serialize_reply(reply: ChatReply) -> dict[str, object]:
    choice = None
    if reply.choice is not None:
        choice = {
            "prompt_id": reply.choice.prompt_id,
            "question": reply.choice.question,
            "options": [asdict(option) for option in reply.choice.options],
        }

    return {
        "text": reply.text,
        "status": reply.status,
        "error_code": reply.error_code,
        "pending": asdict(reply.pending) if reply.pending is not None else None,
        "choice": choice,
        "citations": [asdict(citation) for citation in reply.citations],
        "request_id": reply.request_id,
    }


def _handle_turn(active: _ActiveSession, request: TurnRequest) -> dict[str, object]:
    backend = _configured_backend()
    message, decision, choice = _turn_arguments(request)
    reply = backend.handle_message(
        active.state,
        message,
        decision=decision,
        choice=choice,
    )
    if reply.error_code == "invalid_session":
        backend.end_session(active.state)
        with _sessions_lock:
            session_id = _session_by_room.get(active.room_name)
        if session_id is not None:
            _remove_session(session_id)
    return _serialize_reply(reply)


@app.get("/api/login-options")
def login_options() -> list[dict[str, str]]:
    options = _configured_backend().list_login_options()
    return [asdict(option) for option in options]


@app.post("/api/sessions", response_model=SessionResponse)
async def create_livekit_session(request: SessionRequest) -> SessionResponse:
    url, api_key, api_secret = _configured_livekit()
    room_name = f"support-{uuid.uuid4().hex}"
    token = _create_room_token(
        api_key=api_key,
        api_secret=api_secret,
        room_name=room_name,
        participant_identity=f"customer-{uuid.uuid4().hex}",
    )

    backend = _configured_backend()
    result = backend.start_session(request.customer_id)
    if not result.ok or result.state is None:
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if result.error_code == "unavailable"
            else status.HTTP_404_NOT_FOUND
        )
        raise HTTPException(status_code=code, detail=result.greeting)

    session_id = secrets.token_urlsafe(32)
    active = _ActiveSession(state=result.state, room_name=room_name)
    with _sessions_lock:
        _sessions[session_id] = active
        _session_by_room[room_name] = session_id

    dispatched = False
    try:
        await _dispatch_agent(
            url=url,
            api_key=api_key,
            api_secret=api_secret,
            room_name=room_name,
        )
        dispatched = True
    finally:
        if not dispatched:
            _remove_session(session_id)
            backend.end_session(result.state)

    return SessionResponse(
        session_id=session_id,
        url=url,
        room_name=room_name,
        token=token,
        greeting=result.greeting,
    )


@app.post("/api/sessions/turn")
def session_turn(request: SessionTurnRequest) -> dict[str, object]:
    return _handle_turn(_lookup_session(request.session_id), request)


@app.post("/api/agent/turn")
def agent_turn(request: AgentTurnRequest) -> dict[str, object]:
    """Internal agent endpoint; keep this API off the public host interface."""
    with _sessions_lock:
        session_id = _session_by_room.get(request.room_name)
        active = _sessions.get(session_id) if session_id is not None else None
    if active is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The voice session is unavailable.",
        )
    return _handle_turn(active, request)


@app.post("/api/sessions/close", status_code=status.HTTP_204_NO_CONTENT)
def close_session(request: CloseSessionRequest) -> Response:
    active = _remove_session(request.session_id)
    if active is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The support session is unavailable.",
        )
    _configured_backend().end_session(active.state)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
