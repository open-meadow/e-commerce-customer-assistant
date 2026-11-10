import pytest

from app.ui.fake_backend import (
    ChoiceSelection,
    ConfirmationDecision,
    ConversationState,
    handle_message,
    list_login_options,
    start_session,
)


def _session_state() -> ConversationState:
    result = start_session("C101")
    assert result.ok
    assert result.state is not None
    return result.state


def test_login_options_and_session_start():
    assert [option.customer_id for option in list_login_options()] == ["C101", "C102"]

    result = start_session("C101")

    assert result.ok
    assert result.error_code is None
    assert result.state == ConversationState(customer_id="C101")
    assert "Customer C101" in result.greeting


def test_unknown_customer_cannot_start_session():
    result = start_session("C999")

    assert not result.ok
    assert result.state is None
    assert result.error_code == "unknown_customer"


@pytest.mark.parametrize(
    ("message", "decision", "choice"),
    [
        (None, None, None),
        ("hello", ConfirmationDecision("action", True), None),
        ("hello", None, ChoiceSelection("prompt", "option")),
        (None, ConfirmationDecision("action", True), ChoiceSelection("prompt", "option")),
    ],
)
def test_handle_message_requires_exactly_one_input(message, decision, choice):
    with pytest.raises(ValueError, match="exactly one"):
        handle_message(
            _session_state(),
            message,
            decision=decision,
            choice=choice,
        )


def test_return_choice_selection_creates_confirmation_without_mutating_old_state():
    initial_state = _session_state()
    choice_reply = handle_message(initial_state, "I want to return an item")

    assert choice_reply.status == "needs_choice"
    assert choice_reply.choice is not None
    assert choice_reply.pending is None
    assert [option.label for option in choice_reply.choice.options] == [
        "Damaged or defective item",
        "Wrong product received",
        "Other / standard return reason",
    ]
    assert initial_state.flow == "idle"

    confirmation = handle_message(
        choice_reply.state,
        choice=ChoiceSelection(choice_reply.choice.prompt_id, "wrong_product"),
    )

    assert confirmation.status == "needs_confirmation"
    assert confirmation.choice is None
    assert confirmation.pending is not None
    assert "Wrong product received" in confirmation.pending.summary
    assert "will not submit a real request" in confirmation.pending.summary
    assert choice_reply.state.flow == "return_choice"


@pytest.mark.parametrize(
    ("prompt_id", "option_id", "expected_status"),
    [
        ("invalid-prompt", "damaged", "ok"),
        ("demo-return-reason", "invalid-option", "needs_choice"),
    ],
)
def test_invalid_choice_does_not_create_confirmation(
    prompt_id,
    option_id,
    expected_status,
):
    choice_reply = handle_message(_session_state(), "I want to return an item")

    reply = handle_message(
        choice_reply.state,
        choice=ChoiceSelection(prompt_id, option_id),
    )

    assert reply.status == expected_status
    assert reply.pending is None
    assert (reply.choice is not None) == (expected_status == "needs_choice")


@pytest.mark.parametrize(
    ("accepted", "expected_text"),
    [
        (True, "no real request was submitted"),
        (False, "cancelled"),
    ],
)
def test_confirmation_decision_finishes_demo_action(accepted, expected_text):
    pending = handle_message(_session_state(), "demo confirmation")
    assert pending.pending is not None

    reply = handle_message(
        pending.state,
        decision=ConfirmationDecision(pending.pending.action_id, accepted),
    )

    assert reply.status == "ok"
    assert reply.pending is None
    assert reply.state.flow == "idle"
    assert expected_text in reply.text
    assert pending.state.flow == "confirmation"


def test_confirmation_with_wrong_action_id_does_not_complete_pending_action():
    pending = handle_message(_session_state(), "demo confirmation")
    assert pending.pending is not None

    reply = handle_message(
        pending.state,
        decision=ConfirmationDecision("different-action", True),
    )

    assert reply.status == "ok"
    assert reply.state == pending.state
    assert "no action awaiting confirmation" in reply.text


def test_fake_error_reply_has_error_code_only_for_error_status():
    state = _session_state()

    ordinary = handle_message(state, "hello")
    error = handle_message(state, "/demo-error")

    assert ordinary.status == "ok"
    assert ordinary.error_code is None
    assert error.status == "error"
    assert error.error_code == "unavailable"
    assert error.pending is None
    assert error.choice is None
