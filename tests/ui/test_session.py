from app.ui.session import (
    AUTHENTICATED_CUSTOMER_ID_KEY,
    CHOICE_VIEW_KEY,
    CONVERSATION_STATE_KEY,
    MESSAGES_KEY,
    PENDING_VIEW_KEY,
    REQUEST_ID_KEY,
    UI_ERROR_KEY,
    UI_NOTICE_KEY,
    append_message,
    clear_conversation,
    initialize_session,
    is_chat_enabled,
    set_authenticated_customer_id,
    set_conversation_state,
)


def test_initialize_session_sets_defaults_without_resetting_existing_values():
    messages = [{"role": "user", "content": "hello"}]
    session = {MESSAGES_KEY: messages}

    initialize_session(session)
    initialize_session(session)

    assert session[AUTHENTICATED_CUSTOMER_ID_KEY] is None
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
    session = {}
    initialize_session(session)
    assert not is_chat_enabled(session)

    set_authenticated_customer_id(session, "C101")
    assert not is_chat_enabled(session)

    state = object()
    set_conversation_state(session, state)
    assert is_chat_enabled(session)

    set_authenticated_customer_id(session, "  ")
    assert not is_chat_enabled(session)


def test_append_message_stores_plain_role_and_content():
    session = {}

    append_message(session, "assistant", "A demo reply")

    assert session[MESSAGES_KEY] == [
        {"role": "assistant", "content": "A demo reply"}
    ]
