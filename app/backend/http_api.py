"""HTTP boundary for the Streamlit UI and LiveKit agent.

H3 resource protection at this boundary (all limits are per process; see docs/session_resource_protection.md):

* The request body is size-limited before it is parsed, and identifier fields have a length limit.
* Each peer address has a request rate and a session-creation rate. The peer is the TCP peer of the connection;
  ``X-Forwarded-For`` and similar headers are NOT trusted (behind a reverse proxy every client shares the proxy's address,
  so put a real limiter at the proxy too). A customer ID chosen in the UI is never used as a client identity.
* The session maps in this module are cleaned in the same step as the backend registry: when the registry drops a session
  (closed, idle, too old, shutdown) a listener removes the map entries and releases the LiveKit room.
* LiveKit calls (agent dispatch, room release) are time-bounded; a failure is cleaned up and reported without details.
* A background sweeper and the application's shutdown end every session and release every room.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from livekit import api
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.backend.api import Backend, get_backend
from app.backend.config import Settings
from app.backend.limits import SlidingWindowLimiter
from app.backend.types import (
    ChatReply,
    ChoiceSelection,
    ConfirmationDecision,
    ConversationState,
)
from app.llm.base import LLMConfigError

log = logging.getLogger("app.http")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=False)

_AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "ecommerce-support-agent")
_ID_MAX = 128


@dataclass(frozen=True)
class _ActiveSession:
    state: ConversationState
    room_name: str


class SessionRequest(BaseModel):
    customer_id: str = Field(max_length=64)


class DecisionRequest(BaseModel):
    action_id: str = Field(max_length=_ID_MAX)
    accepted: bool


class ChoiceRequest(BaseModel):
    prompt_id: str = Field(max_length=_ID_MAX)
    option_id: str = Field(max_length=_ID_MAX)


class TurnRequest(BaseModel):
    message: str | None = None
    decision: DecisionRequest | None = None
    choice: ChoiceRequest | None = None


class SessionTurnRequest(TurnRequest):
    session_id: str = Field(max_length=_ID_MAX)


class AgentTurnRequest(TurnRequest):
    room_name: str = Field(max_length=_ID_MAX)


class CloseSessionRequest(BaseModel):
    session_id: str = Field(max_length=_ID_MAX)


class AgentCloseRequest(BaseModel):
    room_name: str = Field(max_length=_ID_MAX)


class SessionResponse(BaseModel):
    session_id: str
    url: str
    room_name: str
    token: str
    greeting: str


_sessions: dict[str, _ActiveSession] = {}
_session_by_room: dict[str, str] = {}
_session_by_handle: dict[str, str] = {}
_sessions_lock = threading.RLock()
_state: dict[str, object] = {"backend": None, "client_limiter": None, "timeout": Settings().livekit_timeout_seconds}


def _reset_state() -> None:
    """Forget every HTTP-side session and limiter (tests, and a clean start)."""
    with _sessions_lock:
        _sessions.clear()
        _session_by_room.clear()
        _session_by_handle.clear()
    _state.update(backend=None, client_limiter=None, timeout=Settings().livekit_timeout_seconds)


# ---------------------------------------------------------------------------
# Request body limit (an ASGI middleware, so the body is never buffered past the limit)
# ---------------------------------------------------------------------------


class _BodyTooLarge(HTTPException):
    """Raised from the wrapped ``receive``. It is an HTTPException so FastAPI answers 413 instead of turning it into a 422."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="The request is too large.")


def _body_limit() -> int:
    try:
        return get_backend().settings.max_body_bytes
    except Exception:  # an unconfigured backend must not make the limit disappear
        return Settings().max_body_bytes


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _body_limit()
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = True
            if too_big:
                await self._reject(send)
                return
        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b'{"detail":"The request is too large."}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Application, cleanup wiring and lifespan
# ---------------------------------------------------------------------------


def _run_blocking(factory, timeout: float) -> None:
    """Run a coroutine to completion with a time limit from synchronous code (never inside a running loop)."""
    def go() -> None:
        asyncio.run(asyncio.wait_for(factory(), timeout))

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(go).result(timeout + 5)


def _livekit_values() -> tuple[str, str, str] | None:
    values = tuple(os.environ.get(name, "").strip() for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
    return values if all(values) else None


def _release_room_blocking(room_name: str) -> None:
    """Best effort: delete the LiveKit room, which disconnects its agent. Bounded; a failure is logged, never raised."""
    values = _livekit_values()
    if values is None:
        return
    url, api_key, api_secret = values
    try:
        _run_blocking(lambda: _delete_room(url=url, api_key=api_key, api_secret=api_secret, room_name=room_name),
                      float(_state["timeout"]))
    except Exception as exc:
        log.warning("could not release LiveKit room %s (%s)", room_name, type(exc).__name__)


async def _release_room(room_name: str) -> None:
    values = _livekit_values()
    if values is None:
        return
    url, api_key, api_secret = values
    try:
        await asyncio.wait_for(_delete_room(url=url, api_key=api_key, api_secret=api_secret, room_name=room_name),
                               float(_state["timeout"]))
    except Exception as exc:
        log.warning("could not release LiveKit room %s (%s)", room_name, type(exc).__name__)


def _on_session_dropped(session, reason: str) -> None:
    """The backend registry dropped a session (idle, too old, ended elsewhere or shutdown): release what this layer holds."""
    with _sessions_lock:
        session_id = _session_by_handle.pop(session.handle, None)
        active = _sessions.pop(session_id, None) if session_id is not None else None
        if active is not None:
            _session_by_room.pop(active.room_name, None)
    if active is not None:
        log.info("http session released reason=%s ref=%s", reason, session.session_ref)
        _release_room_blocking(active.room_name)


def _install(backend: Backend) -> None:
    """Wire this module to the backend in use (once per backend): cleanup listener, client limiter, timeout."""
    if _state["backend"] is backend:
        return
    backend.registry.add_drop_listener(_on_session_dropped)
    _state.update(backend=backend,
                  client_limiter=SlidingWindowLimiter(backend.settings.client_requests_per_minute, 60, max_keys=10000),
                  timeout=backend.settings.livekit_timeout_seconds)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    backend = None
    try:
        backend = get_backend()
        _install(backend)
        backend.registry.start_sweeper()
    except LLMConfigError:
        log.warning("backend not configured: the session sweeper is not running")
    try:
        yield
    finally:
        if backend is not None:
            await asyncio.to_thread(backend.registry.shutdown)  # ends every session; the listener releases every room


app = FastAPI(title="E-commerce support API", lifespan=_lifespan)
app.add_middleware(BodySizeLimitMiddleware)


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
        backend = get_backend()
    except LLMConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The support backend is not configured.",
        ) from exc
    _install(backend)
    return backend


def _client_key(request: Request) -> str:
    """The TCP peer of this connection. Forwarded headers are deliberately ignored: anyone can set them."""
    return request.client.host if request.client else "unknown"


def _too_many(detail: str, retry_after: int, code: int = status.HTTP_429_TOO_MANY_REQUESTS) -> HTTPException:
    return HTTPException(status_code=code, detail=detail, headers={"Retry-After": str(max(1, int(retry_after)))})


def _client_rate_limit(request: Request) -> None:
    """Dependency for the public endpoints: at most N requests a minute per peer address."""
    _configured_backend()
    limiter: SlidingWindowLimiter = _state["client_limiter"]  # type: ignore[assignment]
    key = _client_key(request)
    if not limiter.allow(key):
        raise _too_many("Too many requests. Please wait a moment and try again.", limiter.retry_after(key))


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


async def _delete_room(
    *,
    url: str,
    api_key: str,
    api_secret: str,
    room_name: str,
) -> None:
    """Deleting a room disconnects everyone in it, the voice agent included, which ends the agent's job."""
    client = api.LiveKitAPI(url, api_key, api_secret)
    try:
        await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
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
            _session_by_handle.pop(active.state._handle, None)
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
            _release_room_blocking(active.room_name)
    return _serialize_reply(reply)


@app.get("/api/login-options", dependencies=[Depends(_client_rate_limit)])
def login_options() -> list[dict[str, str]]:
    options = _configured_backend().list_login_options()
    return [asdict(option) for option in options]


@app.post("/api/sessions", response_model=SessionResponse, dependencies=[Depends(_client_rate_limit)])
async def create_livekit_session(request: Request, body: SessionRequest) -> SessionResponse:
    url, api_key, api_secret = _configured_livekit()
    backend = _configured_backend()
    # The registry enforces the global cap and the creation rates atomically; nothing is allocated before it says yes.
    result = await asyncio.to_thread(backend.start_session, body.customer_id, _client_key(request))
    if not result.ok or result.state is None:
        if result.error_code == "capacity":
            raise _too_many(result.greeting, result.retry_after or 30, status.HTTP_503_SERVICE_UNAVAILABLE)
        if result.error_code == "rate_limited":
            raise _too_many(result.greeting, result.retry_after or 30)
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if result.error_code == "unavailable"
            else status.HTTP_404_NOT_FOUND
        )
        raise HTTPException(status_code=code, detail=result.greeting)

    room_name = f"support-{uuid.uuid4().hex}"
    session_id = secrets.token_urlsafe(32)
    active = _ActiveSession(state=result.state, room_name=room_name)
    with _sessions_lock:
        _sessions[session_id] = active
        _session_by_room[room_name] = session_id
        _session_by_handle[result.state._handle] = session_id

    async def abort(code: int, detail: str) -> HTTPException:
        _remove_session(session_id)
        await asyncio.to_thread(backend.end_session, result.state)
        await _release_room(room_name)
        return HTTPException(status_code=code, detail=detail)

    try:
        token = _create_room_token(
            api_key=api_key,
            api_secret=api_secret,
            room_name=room_name,
            participant_identity=f"customer-{uuid.uuid4().hex}",
        )
        await asyncio.wait_for(
            _dispatch_agent(url=url, api_key=api_key, api_secret=api_secret, room_name=room_name),
            backend.settings.livekit_timeout_seconds,
        )
    except asyncio.TimeoutError:
        log.warning("LiveKit agent dispatch timed out")
        raise await abort(status.HTTP_504_GATEWAY_TIMEOUT, "The voice service did not respond in time.")
    except Exception as exc:
        log.warning("LiveKit session setup failed (%s)", type(exc).__name__)
        raise await abort(status.HTTP_502_BAD_GATEWAY, "The voice service is unavailable.")

    return SessionResponse(
        session_id=session_id,
        url=url,
        room_name=room_name,
        token=token,
        greeting=result.greeting,
    )


@app.post("/api/sessions/turn", dependencies=[Depends(_client_rate_limit)])
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


@app.post("/api/agent/close", status_code=status.HTTP_204_NO_CONTENT)
def agent_close(request: AgentCloseRequest) -> Response:
    """Internal: the voice agent's job ended (the customer left or a limit was reached); release the session and room."""
    with _sessions_lock:
        session_id = _session_by_room.get(request.room_name)
    active = _remove_session(session_id) if session_id is not None else None
    if active is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The voice session is unavailable.",
        )
    _configured_backend().end_session(active.state)
    _release_room_blocking(active.room_name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/sessions/close", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_client_rate_limit)])
def close_session(request: CloseSessionRequest) -> Response:
    active = _remove_session(request.session_id)
    if active is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The support session is unavailable.",
        )
    _configured_backend().end_session(active.state)
    _release_room_blocking(active.room_name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
