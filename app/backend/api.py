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
from dataclasses import replace
from typing import Mapping, Optional

from app.backend import clock
from app.backend.config import Settings
from app.backend.limits import BudgetedLLM, ProviderBudget, SessionLimitError
from app.backend.sessions import SessionClosedError, SessionRegistry
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
_CAPACITY_TEXT = "The assistant is busy right now. Please try again in a few minutes."
_CREATION_RATE_TEXT = "Too many chats were started in a short time. Please wait a moment and try again."
_BUSY_TEXT = "Your previous request is still being processed. Please wait a moment and try again."
_REQUEST_LIMIT_TEXT = "You've reached the request limit for this session. Please start a new session."
_REQUEST_RATE_TEXT = "You're sending requests too quickly. Please wait a moment and try again."
_SESSION_BUDGET_TEXT = "You've reached the usage limit for this session. Please start a new session."
_WINDOW_BUDGET_TEXT = "The assistant is receiving too many requests right now. Please try again shortly."


class Backend:
    """One backend instance: registry, engine, LLM client and settings."""

    def __init__(self, llm: Optional[LLMClient] = None, settings: Optional[Settings] = None,
                 env: Optional[Mapping[str, str]] = None, interpret_replies: Optional[bool] = None):
        source = env if env is not None else os.environ
        self.settings = settings or Settings.from_env(source)
        self.llm = llm if llm is not None else build_llm_client(source)
        self.registry = SessionRegistry(self.settings)
        # The engine's model calls are charged to the session's budget; ``self.llm`` stays the raw client.
        self.budget = ProviderBudget(self.settings.provider_calls_per_session, self.settings.provider_calls_per_minute)
        self.registry.add_drop_listener(lambda session, reason: self.budget.forget(session.session_ref))
        self.engine = Engine(self.registry, BudgetedLLM(self.llm, self.budget), self.settings, interpret=interpret_replies)

    @property
    def interpretation_active(self) -> bool:
        """True when a language model interprets replies at open prompts (see ``Engine.interpret``)."""
        return self.engine.interpret

    # -- login ----------------------------------------------------------

    def list_login_options(self) -> list[LoginOption]:
        try:
            rows = repository.list_customers_for_login()
        except DatabaseError:
            log.warning("login options unavailable")
            return []
        return [LoginOption(r["customer_id"], r["name"]) for r in rows]

    def close(self) -> None:
        """Stop the background sweeper and end every session (the application is shutting down)."""
        self.registry.shutdown()

    def start_session(self, customer_id: str, client_key: Optional[str] = None) -> StartSessionResult:
        if not isinstance(customer_id, str):
            return StartSessionResult(False, None, "That customer isn't available.", "unknown_customer")
        try:
            rows = repository.list_customers_for_login()
        except DatabaseError:
            return StartSessionResult(False, None, _UNAVAILABLE_TEXT, "unavailable")
        match = next((r for r in rows if r["customer_id"] == customer_id), None)
        if match is None:
            return StartSessionResult(False, None, "That customer isn't available.", "unknown_customer")
        try:
            session = self.registry.create(match["customer_id"], match["name"], client_key=client_key)
        except SessionLimitError as exc:
            if exc.reason == "capacity":
                return StartSessionResult(False, None, _CAPACITY_TEXT, "capacity", exc.retry_after)
            return StartSessionResult(False, None, _CREATION_RATE_TEXT, "rate_limited", exc.retry_after)
        log.info("session started ref=%s", session.session_ref)
        greeting = (f"Hi {match['name']}! I'm the store's automated support assistant. I can check order "
                    "status, answer refund and policy questions, look up products, start return requests "
                    "and raise support tickets. What can I help you with?")
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

        def error(code: str, text: str, terminal: bool = False) -> ChatReply:
            return replace(reply(Turn(text, "error", code)), terminal=terminal)

        supplied = [x is not None for x in (message, decision, choice)]
        if sum(supplied) != 1:
            return error("invalid_request", "Send exactly one of a message, a confirmation or a choice.")
        session = self.registry.get(state._handle)
        if session is None:
            return error("invalid_session", _INVALID_SESSION_TEXT, terminal=True)
        # A request that cannot get the session's turn lock in time is refused (and says so) instead of holding a worker
        # thread for as long as another turn runs. The customer's open card is untouched and can be answered again.
        if not session.turn_lock.acquire(timeout=self.settings.turn_lock_timeout_seconds):
            return error("rate_limited", _BUSY_TEXT)
        accepted = False  # only an accepted request counts as activity: a refused one must not keep a session alive
        try:
            if self.registry.get(state._handle) is None:  # ended or expired while waiting
                return error("invalid_session", _INVALID_SESSION_TEXT, terminal=True)
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
                                                     "Please start a new session.", terminal=True)
                refused = self._admit(session, uses_provider=decision is None)
                if refused is not None:
                    return error("rate_limited", refused[0], terminal=refused[1])
                accepted = True
                self.registry.touch(session)
                with self.budget.session(session.session_ref):
                    if message is not None:
                        session.message_count += 1
                        turn = self.engine.handle_text(session, message)
                    elif decision is not None:
                        turn = self.engine.handle_decision(session, decision)
                    else:
                        turn = self.engine.handle_choice(session, choice)
            except SessionClosedError:
                # The session ended while this turn ran. Nothing was attached to it; a confirmed action that was already
                # claimed has finished on its own (the claim is taken once, before the session could be dropped).
                return error("invalid_session", _INVALID_SESSION_TEXT, terminal=True)
            except Exception:
                log.exception("request failed ref=%s request=%s", session.session_ref, request_id)
                return error("internal", "Something went wrong. Please try again.")
        finally:
            if accepted:
                self.registry.touch(session)
            session.turn_lock.release()
        return reply(turn)

    def _admit(self, session, *, uses_provider: bool):
        """``(refusal text, terminal)`` if this request is over a per-session or provider limit, else ``None``.

        ``terminal`` is true for a hard limit that never recovers (the session's request limit or model-call budget) and false
        for a transient one (a request rate, the shared model-call window).

        A confirmation (``uses_provider`` false) is never refused for the provider budget: it makes no model call, and a
        valid customer operation must not be dropped because of it."""
        why, _wait = self.registry.admit_request(session)
        if why == "limit":
            return _REQUEST_LIMIT_TEXT, True
        if why == "rate":
            return _REQUEST_RATE_TEXT, False
        if uses_provider:
            budget = self.budget.refusal(session.session_ref)
            if budget == "session":
                return _SESSION_BUDGET_TEXT, True
            if budget == "window":
                return _WINDOW_BUDGET_TEXT, False
        return None

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
            _backend.registry.start_sweeper()  # expired sessions are removed even when nobody calls the API
        return _backend


def set_backend(backend: Optional[Backend]) -> None:
    """Install a backend (tests, or a configured one). ``None`` resets."""
    global _backend
    with _backend_lock:
        _backend = backend


def list_login_options() -> list[LoginOption]:
    return get_backend().list_login_options()


def start_session(customer_id: str, client_key: Optional[str] = None) -> StartSessionResult:
    return get_backend().start_session(customer_id, client_key)


def end_session(state: ConversationState) -> None:
    get_backend().end_session(state)


def handle_message(state: ConversationState, message: Optional[str] = None, *,
                   decision: Optional[ConfirmationDecision] = None,
                   choice: Optional[ChoiceSelection] = None) -> ChatReply:
    return get_backend().handle_message(state, message, decision=decision, choice=choice)


def check_backend() -> BackendHealth:
    return get_backend().check_backend()
