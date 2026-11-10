"""Adapter exposing the frontend API from the selected backend implementation."""

if __package__:
    from .fake_backend import (
        ChatReply,
        ChoiceOption,
        ChoiceSelection,
        ChoiceView,
        Citation,
        ConfirmationDecision,
        ConversationState,
        LoginOption,
        PendingView,
        StartSessionResult,
        handle_message,
        list_login_options,
        start_session,
    )
else:
    from fake_backend import (
        ChatReply,
        ChoiceOption,
        ChoiceSelection,
        ChoiceView,
        Citation,
        ConfirmationDecision,
        ConversationState,
        LoginOption,
        PendingView,
        StartSessionResult,
        handle_message,
        list_login_options,
        start_session,
    )


def handle_turn(
    authenticated_customer_id: str,
    message: str,
    history: list[dict[str, str]],
) -> str:
    """Compatibility bridge for the UI until it adopts the session API."""
    del history
    session = start_session(authenticated_customer_id)
    if not session.ok or session.state is None:
        return session.greeting
    return handle_message(session.state, message).text


__all__ = [
    "ChatReply",
    "ChoiceOption",
    "ChoiceSelection",
    "ChoiceView",
    "Citation",
    "ConfirmationDecision",
    "ConversationState",
    "LoginOption",
    "PendingView",
    "StartSessionResult",
    "handle_message",
    "handle_turn",
    "list_login_options",
    "start_session",
]
