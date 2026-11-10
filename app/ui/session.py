from collections.abc import MutableMapping
from typing import Any

SessionState = MutableMapping[Any, Any]

AUTHENTICATED_CUSTOMER_ID_KEY = "authenticated_customer_id"
MESSAGES_KEY = "messages"

def initialize_session(session_state: SessionState) -> None:
    session_state.setdefault(AUTHENTICATED_CUSTOMER_ID_KEY, None)
    session_state.setdefault(MESSAGES_KEY, [])

def set_authenticated_customer_id(
        session_state: SessionState,
        customer_id: str | None,
) -> None:
    initialize_session(session_state=session_state)

    if session_state[AUTHENTICATED_CUSTOMER_ID_KEY] != customer_id:
        session_state[AUTHENTICATED_CUSTOMER_ID_KEY] = customer_id
        session_state[MESSAGES_KEY] = []

def is_chat_enabled(session_state: SessionState) -> bool:
    customer_id = session_state.get(AUTHENTICATED_CUSTOMER_ID_KEY)
    return isinstance(customer_id, str) and bool(customer_id.strip())