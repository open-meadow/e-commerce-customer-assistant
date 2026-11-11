from app.ui.client import ChoiceView, ConversationState, PendingView
from app.ui.session import (
    ACTIVE_CHAT_ID_KEY,
    AUTHENTICATED_CUSTOMER_ID_KEY,
    CHOICE_VIEW_KEY,
    CHATS_KEY,
    CONVERSATION_STATE_KEY,
    MESSAGES_KEY,
    NEW_CHAT_TITLE,
    PENDING_VIEW_KEY,
    REQUEST_ID_KEY,
    UI_ERROR_KEY,
    UI_NOTICE_KEY,
    append_chat_message,
    append_message,
    clear_conversation,
    create_chat,
    get_active_chat,
    initialize_session,
    is_chat_enabled,
    select_chat,
    set_authenticated_customer_id,
)


def test_initialize_session_sets_defaults_without_resetting_existing_values():
    messages = [{"role": "user", "content": "hello"}]
    session = {MESSAGES_KEY: messages}

    initialize_session(session)
    initialize_session(session)

    assert session[AUTHENTICATED_CUSTOMER_ID_KEY] is None
    assert session[CHATS_KEY] == {}
    assert session[ACTIVE_CHAT_ID_KEY] is None
    assert session[MESSAGES_KEY] is messages
    assert session[CONVERSATION_STATE_KEY] is None
    assert session[PENDING_VIEW_KEY] is None
    assert session[CHOICE_VIEW_KEY] is None


def test_changing_customer_clears_conversation_and_transient_ui_state():
    session = {
        AUTHENTICATED_CUSTOMER_ID_KEY: "C101",
        MESSAGES_KEY: [{"role": "user", "content": "private transcript"}],
        CONVERSATION_STATE_KEY: object(),
        PENDING_VIEW_KEY: object(),
        CHOICE_VIEW_KEY: object(),
        UI_ERROR_KEY: "old error",
        UI_NOTICE_KEY: "old notice",
        REQUEST_ID_KEY: "old-request",
    }

    changed = set_authenticated_customer_id(session, "C102")

    assert changed
    assert session[AUTHENTICATED_CUSTOMER_ID_KEY] == "C102"
    assert session[MESSAGES_KEY] == []
    assert session[CONVERSATION_STATE_KEY] is None
    assert session[PENDING_VIEW_KEY] is None
    assert session[CHOICE_VIEW_KEY] is None
    assert session[UI_ERROR_KEY] is None
    assert session[UI_NOTICE_KEY] is None
    assert REQUEST_ID_KEY not in session


def test_setting_same_customer_preserves_conversation():
    state = object()
    session = {
        AUTHENTICATED_CUSTOMER_ID_KEY: "C101",
        CONVERSATION_STATE_KEY: state,
        MESSAGES_KEY: [{"role": "assistant", "content": "hello"}],
    }

    changed = set_authenticated_customer_id(session, "C101")

    assert not changed
    assert session[CONVERSATION_STATE_KEY] is state
    assert session[MESSAGES_KEY] == [{"role": "assistant", "content": "hello"}]


def test_clear_conversation_removes_transcript_state_and_pending_views():
    session = {
        AUTHENTICATED_CUSTOMER_ID_KEY: "C101",
        CONVERSATION_STATE_KEY: object(),
        MESSAGES_KEY: [{"role": "user", "content": "hello"}],
        PENDING_VIEW_KEY: object(),
        CHOICE_VIEW_KEY: object(),
        UI_ERROR_KEY: "error",
        UI_NOTICE_KEY: "notice",
    }

    clear_conversation(session)

    assert session[AUTHENTICATED_CUSTOMER_ID_KEY] == "C101"
    assert session[CONVERSATION_STATE_KEY] is None
    assert session[MESSAGES_KEY] == []
    assert session[PENDING_VIEW_KEY] is None
    assert session[CHOICE_VIEW_KEY] is None
    assert session[UI_ERROR_KEY] is None
    assert session[UI_NOTICE_KEY] is None


def test_chat_is_enabled_only_with_customer_and_conversation_state():
    """Enable chat only when the selected customer has an active conversation."""
    session = {}
    initialize_session(session)
    assert not is_chat_enabled(session)

    set_authenticated_customer_id(session, "C101")
    assert not is_chat_enabled(session)

    state = ConversationState(customer_id="C101")
    create_chat(session, state, "Hello")
    assert is_chat_enabled(session)

    set_authenticated_customer_id(session, "  ")
    assert not is_chat_enabled(session)


def test_append_message_stores_plain_role_and_content():
    session = {}

    append_message(session, "assistant", "A demo reply")

    assert session[MESSAGES_KEY] == [
        {"role": "assistant", "content": "A demo reply"}
    ]


def test_create_and_select_chats_preserves_each_chat_state():
    """Keep backend and action state isolated when selecting chats."""
    first_state = ConversationState(customer_id="C101")
    session = {}

    first_chat_id = create_chat(session, first_state, "First greeting")
    first_chat = get_active_chat(session)
    assert first_chat is not None
    assert first_chat_id == session[ACTIVE_CHAT_ID_KEY]
    assert first_chat["conversation_state"] is first_state
    first_pending = PendingView(
        action_id="first-action",
        kind="demo",
        summary="First action",
        expires_at="later",
    )
    first_choice = ChoiceView(
        prompt_id="first-prompt",
        question="First question",
        options=(),
    )
    first_chat["pending_view"] = first_pending
    first_chat["choice_view"] = first_choice
    assert first_chat["messages"] == [
        {"role": "assistant", "content": "First greeting"}
    ]

    second_state = ConversationState(customer_id="C101")
    second_chat_id = create_chat(session, second_state, "Second greeting")
    second_chat = get_active_chat(session)
    assert second_chat is not None
    assert second_chat_id != first_chat_id
    assert second_chat["conversation_state"] is second_state
    assert second_chat["pending_view"] is None
    assert second_chat["choice_view"] is None

    session[UI_ERROR_KEY] = "old error"
    session[UI_NOTICE_KEY] = "old notice"
    session[REQUEST_ID_KEY] = "old-request"
    assert select_chat(session, first_chat_id)
    restored_chat = get_active_chat(session)
    assert restored_chat is not None
    assert restored_chat is first_chat
    assert restored_chat["conversation_state"] is first_state
    assert restored_chat["pending_view"] is first_pending
    assert restored_chat["choice_view"] is first_choice
    assert session[UI_ERROR_KEY] is None
    assert session[UI_NOTICE_KEY] is None
    assert REQUEST_ID_KEY not in session
    assert not select_chat(session, "missing")
    assert get_active_chat(session) is first_chat


def test_chat_title_comes_from_first_nonempty_user_message():
    """Derive a chat title from its first non-empty user message."""
    session = {}
    create_chat(session, ConversationState(customer_id="C101"), "Welcome")

    chat = get_active_chat(session)
    assert chat is not None
    assert chat["title"] == NEW_CHAT_TITLE

    append_chat_message(session, "assistant", "A reply")
    append_chat_message(session, "user", "  Where is my order?  ")
    append_chat_message(session, "user", "Another question")

    chat = get_active_chat(session)
    assert chat is not None
    assert chat["title"] == "Where is my order?"
    assert chat["messages"][-2:] == [
        {"role": "user", "content": "  Where is my order?  "},
        {"role": "user", "content": "Another question"},
    ]

    new_chat_id = create_chat(
        session,
        ConversationState(customer_id="C101"),
        "Welcome again",
    )
    new_chat = get_active_chat(session)
    assert new_chat is not None
    assert session[ACTIVE_CHAT_ID_KEY] == new_chat_id
    assert new_chat["title"] == NEW_CHAT_TITLE


def test_changing_customer_clears_all_chats():
    """Clear the chat list when switching simulated customers."""
    session = {}
    set_authenticated_customer_id(session, "C101")
    create_chat(session, ConversationState(customer_id="C101"), "First chat")
    create_chat(session, ConversationState(customer_id="C101"), "Second chat")

    changed = set_authenticated_customer_id(session, "C102")

    assert changed
    assert session[CHATS_KEY] == {}
    assert session[ACTIVE_CHAT_ID_KEY] is None
    assert get_active_chat(session) is None
