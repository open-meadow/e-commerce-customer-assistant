"""Local demo implementation of the frontend-facing backend contract."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class LoginOption:
    customer_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ConversationState:
    """Opaque frontend handle for the fake backend's demo conversation."""

    customer_id: str
    flow: Literal["idle", "return_choice", "confirmation"] = "idle"
    reason: str | None = None
    action_id: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class StartSessionResult:
    ok: bool
    state: ConversationState | None
    greeting: str
    error_code: Literal["unknown_customer", "unavailable"] | None


@dataclass(frozen=True, slots=True)
class ConfirmationDecision:
    action_id: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class ChoiceSelection:
    prompt_id: str
    option_id: str


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    option_id: str
    label: str


@dataclass(frozen=True, slots=True)
class ChoiceView:
    prompt_id: str
    question: str
    options: tuple[ChoiceOption, ...]


@dataclass(frozen=True, slots=True)
class PendingView:
    action_id: str
    kind: str
    summary: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class Citation:
    source: str
    section: str


@dataclass(frozen=True, slots=True)
class ChatReply:
    text: str
    state: ConversationState
    status: Literal["ok", "needs_choice", "needs_confirmation", "error"]
    error_code: Literal[
        "message_too_long",
        "rate_limited",
        "invalid_session",
        "unavailable",
        "internal",
    ] | None
    pending: PendingView | None
    choice: ChoiceView | None
    citations: tuple[Citation, ...]
    request_id: str


_CUSTOMERS = (
    LoginOption(customer_id="C101", display_name="Customer C101"),
    LoginOption(customer_id="C102", display_name="Customer C102"),
)
_RETURN_PROMPT_ID = "demo-return-reason"
_RETURN_OPTIONS = (
    ChoiceOption("damaged", "Damaged or defective item"),
    ChoiceOption("wrong_product", "Wrong product received"),
    ChoiceOption("standard", "Other / standard return reason"),
)


def list_login_options() -> list[LoginOption]:
    return list(_CUSTOMERS)


def start_session(customer_id: str) -> StartSessionResult:
    if not isinstance(customer_id, str):
        raise TypeError("customer_id must be a string")

    customer = next(
        (option for option in _CUSTOMERS if option.customer_id == customer_id),
        None,
    )
    if customer is None:
        return StartSessionResult(
            ok=False,
            state=None,
            greeting="That simulated customer is not available.",
            error_code="unknown_customer",
        )

    return StartSessionResult(
        ok=True,
        state=ConversationState(customer_id=customer.customer_id),
        greeting=(
            f"Hello, {customer.display_name}. This is a demo; "
            "live account data is not connected."
        ),
        error_code=None,
    )


def handle_message(
    state: ConversationState,
    message: str | None = None,
    *,
    decision: ConfirmationDecision | None = None,
    choice: ChoiceSelection | None = None,
) -> ChatReply:
    supplied_inputs = sum(value is not None for value in (message, decision, choice))
    if supplied_inputs != 1:
        raise ValueError("supply exactly one of message, decision, or choice")
    if not isinstance(state, ConversationState):
        raise TypeError("state must be a ConversationState")
    if message is not None and not isinstance(message, str):
        raise TypeError("message must be a string")
    if decision is not None and not isinstance(decision, ConfirmationDecision):
        raise TypeError("decision must be a ConfirmationDecision")
    if choice is not None and not isinstance(choice, ChoiceSelection):
        raise TypeError("choice must be a ChoiceSelection")

    if decision is not None:
        return _handle_decision(state, decision)
    if choice is not None:
        return _handle_choice(state, choice)
    assert message is not None
    return _handle_text(state, message)


def _handle_text(state: ConversationState, message: str) -> ChatReply:
    normalized = message.strip().casefold()
    if state.flow == "return_choice":
        option = next(
            (
                option
                for option in _RETURN_OPTIONS
                if normalized in (option.option_id, option.label.casefold())
            ),
            None,
        )
        if option is not None:
            return _start_confirmation(
                state,
                reason=option.label,
                summary=(
                    f"Demo only: review a return request for “{option.label}”. "
                    "Confirming will not submit a real request."
                ),
            )
        return _return_choice_reply(state)

    if state.flow == "confirmation":
        if normalized in {"confirm", "yes"}:
            assert state.action_id is not None
            return _handle_decision(
                state,
                ConfirmationDecision(action_id=state.action_id, accepted=True),
            )
        if normalized in {"cancel", "no"}:
            assert state.action_id is not None
            return _handle_decision(
                state,
                ConfirmationDecision(action_id=state.action_id, accepted=False),
            )
        return _confirmation_reply(state)

    if not normalized:
        return _reply(state, text="Please type a question.")
    if normalized in {"demo error", "/demo-error"}:
        return _reply(
            state,
            text="The demo assistant is temporarily unavailable.",
            status="error",
            error_code="unavailable",
        )
    if normalized in {"demo confirmation", "/demo-confirmation"}:
        return _start_confirmation(
            state,
            reason=None,
            summary=(
                "Demo confirmation flow: confirming will not submit "
                "or change anything."
            ),
        )
    if "return" in normalized:
        return _return_choice_reply(
            ConversationState(customer_id=state.customer_id, flow="return_choice")
        )

    return _reply(
        state,
        text=(
            "This is a demo reply. Live order, product, policy, return, "
            "and ticket data are not connected."
        ),
    )


def _handle_choice(state: ConversationState, selection: ChoiceSelection) -> ChatReply:
    if state.flow != "return_choice" or selection.prompt_id != _RETURN_PROMPT_ID:
        return _reply(state, text="That choice is no longer available.")

    option = next(
        (option for option in _RETURN_OPTIONS if option.option_id == selection.option_id),
        None,
    )
    if option is None:
        return _return_choice_reply(state)

    return _start_confirmation(
        state,
        reason=option.label,
        summary=(
            f"Demo only: review a return request for “{option.label}”. "
            "Confirming will not submit a real request."
        ),
    )


def _handle_decision(
    state: ConversationState,
    decision: ConfirmationDecision,
) -> ChatReply:
    if (
        state.flow != "confirmation"
        or state.action_id is None
        or decision.action_id != state.action_id
    ):
        return _reply(state, text="There is no action awaiting confirmation.")

    result = (
        "Demo only: confirmation received; no real request was submitted."
        if decision.accepted
        else "The demo request was cancelled. No real request was submitted."
    )
    return _reply(state=ConversationState(customer_id=state.customer_id), text=result)


def _return_choice_reply(state: ConversationState) -> ChatReply:
    return _reply(
        state,
        text="Choose a return reason to continue the demo.",
        status="needs_choice",
        choice=ChoiceView(
            prompt_id=_RETURN_PROMPT_ID,
            question="What is the reason for the return?",
            options=_RETURN_OPTIONS,
        ),
    )


def _start_confirmation(
    state: ConversationState,
    *,
    reason: str | None,
    summary: str,
) -> ChatReply:
    action_id = f"demo-{uuid4().hex}"
    pending_state = ConversationState(
        customer_id=state.customer_id,
        flow="confirmation",
        reason=reason,
        action_id=action_id,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(
            timespec="seconds"
        ),
    )
    return _confirmation_reply(pending_state, summary=summary)


def _confirmation_reply(
    state: ConversationState,
    *,
    summary: str | None = None,
) -> ChatReply:
    assert state.action_id is not None
    assert state.expires_at is not None
    if summary is None:
        summary = (
            "Demo confirmation flow: confirming will not submit or change anything."
            if state.reason is None
            else (
                f"Demo only: review a return request for “{state.reason}”. "
                "Confirming will not submit a real request."
            )
        )
    return _reply(
        state,
        text="Review the demo action below. No real action will be submitted.",
        status="needs_confirmation",
        pending=PendingView(
            action_id=state.action_id,
            kind="demo",
            summary=summary,
            expires_at=state.expires_at,
        ),
    )


def _reply(
    state: ConversationState,
    *,
    text: str,
    status: Literal["ok", "needs_choice", "needs_confirmation", "error"] = "ok",
    error_code: Literal[
        "message_too_long",
        "rate_limited",
        "invalid_session",
        "unavailable",
        "internal",
    ] | None = None,
    pending: PendingView | None = None,
    choice: ChoiceView | None = None,
) -> ChatReply:
    return ChatReply(
        text=text,
        state=state,
        status=status,
        error_code=error_code,
        pending=pending,
        choice=choice,
        citations=(),
        request_id=f"fake-{uuid4().hex[:12]}",
    )
