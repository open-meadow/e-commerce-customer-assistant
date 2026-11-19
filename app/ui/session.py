"""Streamlit session-state helpers (chats, active chat, UI notices).

These helpers only store and move objects around. They never inspect a backend
state, so they work with either backend selected in ``client.py``. The backend
types appear only in annotations and are not imported at runtime.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import uuid4

if TYPE_CHECKING:
    from .client import ChoiceView, ConversationState, PendingView

SessionState = MutableMapping[Any, Any]


class ChatMessage(TypedDict):
    role: str
    content: str


class LiveKitVoiceSession(TypedDict):
    session_id: str
    url: str
    token: str


AUTHENTICATED_CUSTOMER_ID_KEY = "authenticated_customer_id"
CHATS_KEY = "chats"
ACTIVE_CHAT_ID_KEY = "active_chat_id"
MESSAGES_KEY = "messages"
CONVERSATION_STATE_KEY = "conversation_state"
PENDING_VIEW_KEY = "pending_view"
CHOICE_VIEW_KEY = "choice_view"
UI_ERROR_KEY = "ui_error"
UI_NOTICE_KEY = "ui_notice"
REQUEST_ID_KEY = "request_id"
VOICE_SESSION_KEY = "livekit_voice_session"
NEW_CHAT_TITLE = "New chat"


class ChatSession(TypedDict):
    title: str
    messages: list[ChatMessage]
    conversation_state: ConversationState | None
    pending_view: PendingView | None
    choice_view: ChoiceView | None


def initialize_session(session_state: SessionState) -> None:
    session_state.setdefault(AUTHENTICATED_CUSTOMER_ID_KEY, None)
    session_state.setdefault(CHATS_KEY, {})
    session_state.setdefault(ACTIVE_CHAT_ID_KEY, None)
    session_state.setdefault(MESSAGES_KEY, [])
    session_state.setdefault(CONVERSATION_STATE_KEY, None)
    session_state.setdefault(PENDING_VIEW_KEY, None)
    session_state.setdefault(CHOICE_VIEW_KEY, None)
    session_state.setdefault(UI_ERROR_KEY, None)
    session_state.setdefault(UI_NOTICE_KEY, None)
    session_state.setdefault(VOICE_SESSION_KEY, None)

def clear_conversation(session_state: SessionState) -> None:
    initialize_session(session_state=session_state)
    session_state[CONVERSATION_STATE_KEY] = None
    session_state[MESSAGES_KEY] = []
    session_state[PENDING_VIEW_KEY] = None
    session_state[CHOICE_VIEW_KEY] = None
    session_state[UI_ERROR_KEY] = None
    session_state[UI_NOTICE_KEY] = None
    session_state[VOICE_SESSION_KEY] = None
    session_state[CHATS_KEY] = {}
    session_state[ACTIVE_CHAT_ID_KEY] = None
    session_state.pop(REQUEST_ID_KEY, None)


def create_chat(
    session_state: SessionState,
    conversation_state: ConversationState,
    greeting: str,
) -> str:
    """Create and select a chat with its initial greeting."""
    initialize_session(session_state)
    chat_id = uuid4().hex
    chats: dict[str, ChatSession] = session_state[CHATS_KEY]
    chats[chat_id] = {
        "title": NEW_CHAT_TITLE,
        "messages": [{"role": "assistant", "content": greeting}],
        "conversation_state": conversation_state,
        "pending_view": None,
        "choice_view": None,
    }
    select_chat(session_state, chat_id)
    return chat_id


def select_chat(session_state: SessionState, chat_id: str) -> bool:
    initialize_session(session_state)
    chats: dict[str, ChatSession] = session_state[CHATS_KEY]
    if chat_id not in chats:
        return False

    session_state[ACTIVE_CHAT_ID_KEY] = chat_id
    session_state[UI_ERROR_KEY] = None
    session_state[UI_NOTICE_KEY] = None
    session_state.pop(REQUEST_ID_KEY, None)
    return True


def get_active_chat(session_state: SessionState) -> ChatSession | None:
    chat_id = session_state.get(ACTIVE_CHAT_ID_KEY)
    if not isinstance(chat_id, str):
        return None

    chats: dict[str, ChatSession] = session_state.get(CHATS_KEY, {})
    return chats.get(chat_id)


def append_chat_message(
    session_state: SessionState,
    role: str,
    content: str,
) -> None:
    chat = get_active_chat(session_state)
    if chat is None:
        raise RuntimeError("no active chat")

    chat["messages"].append({"role": role, "content": content})
    if role == "user" and chat["title"] == NEW_CHAT_TITLE and content.strip():
        chat["title"] = content.strip()


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
    """Return whether the selected customer has an active chat."""
    customer_id = session_state.get(AUTHENTICATED_CUSTOMER_ID_KEY)
    chat = get_active_chat(session_state)
    return (
        isinstance(customer_id, str)
        and bool(customer_id.strip())
        and chat is not None
        and chat["conversation_state"] is not None
    )
