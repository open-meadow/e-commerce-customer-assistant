"""Public backend types (docs/03 Section 4.2).

Plain frozen dataclasses of strings, numbers, booleans and tuples. No
database rows, no repository dicts, no internal exceptions. These names
and fields are the contract the Streamlit frontend builds against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True, repr=False)
class ConversationState:
    """Opaque handle to a server-side session.

    The handle is a **bearer credential** for the simulated session:
    whoever holds a valid one can act as that session's customer within
    the demo's capabilities until the session ends or expires. It is not
    non-transferable and is never shown, logged or sent to the LLM.
    ``repr`` deliberately hides it.
    """

    _handle: str

    def __repr__(self) -> str:
        return "ConversationState(<opaque>)"


@dataclass(frozen=True)
class LoginOption:
    customer_id: str
    display_name: str


@dataclass(frozen=True)
class StartSessionResult:
    ok: bool
    state: Optional[ConversationState]
    greeting: str
    error_code: Optional[str] = None  # "unknown_customer" or "unavailable"


@dataclass(frozen=True)
class ConfirmationDecision:
    action_id: str
    accepted: bool


@dataclass(frozen=True)
class ChoiceSelection:
    prompt_id: str
    option_id: str


@dataclass(frozen=True)
class PendingView:
    action_id: str
    kind: str
    summary: str
    expires_at: str


@dataclass(frozen=True)
class ChoiceOption:
    option_id: str
    label: str


@dataclass(frozen=True)
class ChoiceView:
    prompt_id: str
    question: str
    options: tuple[ChoiceOption, ...]


@dataclass(frozen=True)
class Citation:
    source: str
    section: str


@dataclass(frozen=True)
class ChatReply:
    """One backend reply.

    ``status`` is ``ok``, ``needs_choice``, ``needs_confirmation`` or
    ``error``. ``error_code`` is set only for ``error``:
    ``message_too_long``, ``rate_limited``, ``invalid_session``,
    ``unavailable`` or ``internal``. At most one of ``pending`` and
    ``choice`` is set. ``text`` is untrusted text: render it as plain
    text, never as HTML.
    """

    text: str
    state: ConversationState
    status: str
    error_code: Optional[str] = None
    pending: Optional[PendingView] = None
    choice: Optional[ChoiceView] = None
    citations: tuple[Citation, ...] = ()
    request_id: str = ""


@dataclass(frozen=True)
class BackendHealth:
    """Result of ``check_backend``. ``llm_mode`` is ``rule_based`` or the provider name."""

    status: str  # ready | not_initialized | schema_incompatible | unavailable
    reason: Optional[str] = None
    llm_mode: str = "rule_based"
    notes: tuple[str, ...] = field(default_factory=tuple)
