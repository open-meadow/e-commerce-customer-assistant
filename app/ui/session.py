from collections.abc import MutableMapping
from typing import Any

SessionState = MutableMapping[Any, Any]

AUTHENTICATED_CUSTOMER_ID_KEY = "authenticated_customer_id"
MESSAGES_KEY = "messages"
CONVERSATION_STATE_KEY = "conversation_state"
PENDING_VIEW_KEY = "pending_view"
CHOICE_VIEW_KEY = "choice_view"
UI_ERROR_KEY = "ui_error"
UI_NOTICE_KEY = "ui_notice"
REQUEST_ID_KEY = "request_id"

def initialize_session(session_state: SessionState) -> None:
    session_state.setdefault(AUTHENTICATED_CUSTOMER_ID_KEY, None)
    session_state.setdefault(MESSAGES_KEY, [])
    session_state.setdefault(CONVERSATION_STATE_KEY, None)
    session_state.setdefault(PENDING_VIEW_KEY, None)
    session_state.setdefault(CHOICE_VIEW_KEY, None)
    session_state.setdefault(UI_ERROR_KEY, None)
    session_state.setdefault(UI_NOTICE_KEY, None)

def clear_conversation(session_state: SessionState) -> None:
    initialize_session(session_state=session_state)
    session_state[CONVERSATION_STATE_KEY] = None
    session_state[MESSAGES_KEY] = []
    session_state[PENDING_VIEW_KEY] = None
    session_state[CHOICE_VIEW_KEY] = None
    session_state[UI_ERROR_KEY] = None
    session_state[UI_NOTICE_KEY] = None
    session_state.pop(REQUEST_ID_KEY, None)

def set_authenticated_customer_id(
        session_state: SessionState,
        customer_id: str | None,
) -> bool:
    initialize_session(session_state=session_state)

    if session_state[AUTHENTICATED_CUSTOMER_ID_KEY] != customer_id:
        session_state[AUTHENTICATED_CUSTOMER_ID_KEY] = customer_id
        clear_conversation(session_state)
        return True
    return False

def append_message(
        session_state: SessionState,
        role: str,
        content: str,
) -> None:
    initialize_session(session_state=session_state)
    messages = session_state[MESSAGES_KEY]
    messages.append({"role": role, "content": content})

def set_conversation_state(
        session_state: SessionState,
        conversation_state: object | None,
) -> None:
    initialize_session(session_state=session_state)
    session_state[CONVERSATION_STATE_KEY] = conversation_state

def is_chat_enabled(session_state: SessionState) -> bool:
    customer_id = session_state.get(AUTHENTICATED_CUSTOMER_ID_KEY)
    return (
        isinstance(customer_id, str)
        and bool(customer_id.strip())
        and session_state.get(CONVERSATION_STATE_KEY) is not None
    )