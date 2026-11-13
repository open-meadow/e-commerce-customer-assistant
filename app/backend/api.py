"""Public backend entry points (docs/03 Section 4.3).

The frontend imports only this module and ``app.backend.types``. Nothing
here returns database rows, repository dictionaries or internal
exceptions. ``text`` in every reply is untrusted: render it as plain text.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Mapping, Optional

from app.backend import clock
from app.backend.config import Settings
from app.backend.sessions import SessionRegistry
from app.backend.types import (BackendHealth, ChatReply, ChoiceSelection, ConfirmationDecision,
                               ConversationState, LoginOption, StartSessionResult)
from app.db import repository
from app.db.errors import DatabaseError
from app.db.health import check_database_health
from app.llm.base import LLMClient, LLMError
from app.llm.providers import build_llm_client
from app.orchestrator.engine import Engine, Turn
from app.rag import embedding_configuration

log = logging.getLogger("app.backend")

_UNAVAILABLE_TEXT = "The service is temporarily unavailable. Please try again shortly."
_INVALID_SESSION_TEXT = "Your session has ended or is no longer valid. Please sign in again."


class Backend:
    """One backend instance: registry, engine, LLM client and settings."""

    def __init__(self, llm: Optional[LLMClient] = None, settings: Optional[Settings] = None,
                 env: Optional[Mapping[str, str]] = None):
        self.settings = settings or Settings()
        self.llm = llm if llm is not None else build_llm_client(env if env is not None else os.environ)
        self.registry = SessionRegistry(self.settings)
        self.engine = Engine(self.registry, self.llm, self.settings)

    # -- login ----------------------------------------------------------

    def list_login_options(self) -> list[LoginOption]:
        try:
            rows = repository.list_customers_for_login()
        except DatabaseError:
            log.warning("login options unavailable")
            return []
        return [LoginOption(r["customer_id"], r["name"]) for r in rows]

    def start_session(self, customer_id: str) -> StartSessionResult:
        if not isinstance(customer_id, str):
            return StartSessionResult(False, None, "That customer isn't available.", "unknown_customer")
        try:
            rows = repository.list_customers_for_login()
        except DatabaseError:
            return StartSessionResult(False, None, _UNAVAILABLE_TEXT, "unavailable")
        match = next((r for r in rows if r["customer_id"] == customer_id), None)
        if match is None:
            return StartSessionResult(False, None, "That customer isn't available.", "unknown_customer")
        session = self.registry.create(match["customer_id"], match["name"])
        log.info("session started ref=%s", session.session_ref)
        greeting = (f"Hi {match['name']}! I can check order status, answer refund and policy questions, "
                    "look up products, start return requests and raise support tickets. "
                    "What can I help you with?")
        return StartSessionResult(True, ConversationState(session.handle), greeting)

    def end_session(self, state: ConversationState) -> None:
        if isinstance(state, ConversationState):
            self.registry.end(state._handle)

    # -- chat -----------------------------------------------------------

    def handle_message(self, state: ConversationState, message: Optional[str] = None, *,
                       decision: Optional[ConfirmationDecision] = None,
                       choice: Optional[ChoiceSelection] = None) -> ChatReply:
        if not isinstance(state, ConversationState):
            raise TypeError("state must be the ConversationState returned by start_session")
        request_id = uuid.uuid4().hex[:12]

        def reply(turn: Turn) -> ChatReply:
            return ChatReply(turn.text, state, turn.status, turn.error_code, turn.pending, turn.choice,
                             turn.citations, request_id)

        def error(code: str, text: str) -> ChatReply:
            return reply(Turn(text, "error", code))

        supplied = [x is not None for x in (message, decision, choice)]
        if sum(supplied) != 1:
            return error("invalid_request", "Send exactly one of a message, a confirmation or a choice.")
        session = self.registry.get(state._handle)
        if session is None:
            return error("invalid_session", _INVALID_SESSION_TEXT)
        with session.turn_lock:
            if self.registry.get(state._handle) is None:  # ended or expired while waiting
                return error("invalid_session", _INVALID_SESSION_TEXT)
            try:
                if message is not None:
                    if not isinstance(message, str) or not message.strip():
                        return error("invalid_request", "Please type a message.")
                    if len(message) > self.settings.max_message_chars:
                        return error("message_too_long",
                                     f"That message is too long. Please keep it under "
                                     f"{self.settings.max_message_chars} characters.")
                    if session.message_count >= self.settings.max_messages_per_session:
                        return error("rate_limited", "You've reached the message limit for this session. "
                                                     "Please start a new session.")
                    session.message_count += 1
                    turn = self.engine.handle_text(session, message)
                elif decision is not None:
                    turn = self.engine.handle_decision(session, decision)
                else:
                    turn = self.engine.handle_choice(session, choice)
            except Exception:
                log.exception("request failed ref=%s request=%s", session.session_ref, request_id)
                return error("internal", "Something went wrong. Please try again.")
        return reply(turn)

    # -- health ---------------------------------------------------------

    def check_backend(self) -> BackendHealth:
        health = check_database_health()
        # Retrieval has its own provider requirement, separate from the LLM: say which embeddings
        # are configured (production OpenAI embeddings need OPENAI_API_KEY) without calling them.
        notes = (f"Policy retrieval uses a persistent Chroma index. Embeddings: {embedding_configuration()}.",)
        if getattr(self.llm, "name", "") == "rule_based":
            notes += ("Using the offline rule-based assistant, not a language model.",)
        return BackendHealth(health.status, health.reason, getattr(self.llm, "name", "unknown"), notes)


# ---------------------------------------------------------------------------
# Module-level API used by the frontend
# ---------------------------------------------------------------------------

_backend: Optional[Backend] = None
_backend_lock = threading.Lock()


def get_backend() -> Backend:
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = Backend()
        return _backend


def set_backend(backend: Optional[Backend]) -> None:
    """Install a backend (tests, or a configured one). ``None`` resets."""
    global _backend
    with _backend_lock:
        _backend = backend


def list_login_options() -> list[LoginOption]:
    return get_backend().list_login_options()


def start_session(customer_id: str) -> StartSessionResult:
    return get_backend().start_session(customer_id)


def end_session(state: ConversationState) -> None:
    get_backend().end_session(state)


def handle_message(state: ConversationState, message: Optional[str] = None, *,
                   decision: Optional[ConfirmationDecision] = None,
                   choice: Optional[ChoiceSelection] = None) -> ChatReply:
    return get_backend().handle_message(state, message, decision=decision, choice=choice)


def check_backend() -> BackendHealth:
    return get_backend().check_backend()
