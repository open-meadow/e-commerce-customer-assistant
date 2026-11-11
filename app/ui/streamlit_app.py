import logging

import streamlit as st

if __package__:
    from .client import (
        ChoiceSelection,
        ConfirmationDecision,
        handle_message,
        list_login_options,
        start_session,
    )
    from .session import (
        ACTIVE_CHAT_ID_KEY,
        AUTHENTICATED_CUSTOMER_ID_KEY,
        CHATS_KEY,
        NEW_CHAT_TITLE,
        REQUEST_ID_KEY,
        UI_ERROR_KEY,
        UI_NOTICE_KEY,
        ChatSession,
        append_chat_message,
        create_chat,
        get_active_chat,
        initialize_session,
        is_chat_enabled,
        select_chat,
        set_authenticated_customer_id,
    )
else:
    from client import (
        ChoiceSelection,
        ConfirmationDecision,
        handle_message,
        list_login_options,
        start_session,
    )
    from session import (
        ACTIVE_CHAT_ID_KEY,
        AUTHENTICATED_CUSTOMER_ID_KEY,
        CHATS_KEY,
        NEW_CHAT_TITLE,
        REQUEST_ID_KEY,
        UI_ERROR_KEY,
        UI_NOTICE_KEY,
        ChatSession,
        append_chat_message,
        create_chat,
        get_active_chat,
        initialize_session,
        is_chat_enabled,
        select_chat,
        set_authenticated_customer_id,
    )

LOGGER = logging.getLogger(__name__)

_ERROR_MESSAGES = {
    "message_too_long": "Please shorten your message and try again.",
    "rate_limited": "Please wait a moment before sending another message.",
    "unavailable": "The assistant is temporarily unavailable.",
    "internal": "Something went wrong. Please try again.",
}


def _begin_session(
    customer_id: str,
    *,
    restart_chat: ChatSession | None = None,
) -> None:
    """Start a chat or replace an expired chat session."""
    if restart_chat is not None:
        restart_chat["conversation_state"] = None
        restart_chat["messages"] = []
        restart_chat["title"] = NEW_CHAT_TITLE
        restart_chat["pending_view"] = None
        restart_chat["choice_view"] = None

    st.session_state[UI_ERROR_KEY] = None
    st.session_state[UI_NOTICE_KEY] = None
    st.session_state.pop(REQUEST_ID_KEY, None)

    try:
        with st.spinner("Starting a new chat..."):
            result = start_session(customer_id)
    except Exception:
        LOGGER.exception("Could not start the customer support session")
        st.session_state[UI_ERROR_KEY] = (
            "Could not start a chat session. Please try again."
        )
        return

    if not result.ok or result.state is None:
        st.session_state[UI_ERROR_KEY] = (
            "That customer session is unavailable. Please try again."
            if result.error_code == "unavailable"
            else "The selected customer could not be found."
        )
        return

    if restart_chat is None:
        create_chat(st.session_state, result.state, result.greeting)
    else:
        restart_chat["conversation_state"] = result.state
        restart_chat["messages"] = [
            {"role": "assistant", "content": result.greeting}
        ]


def _set_customer_from_sidebar() -> None:
    """Render the simulated login and its customer-scoped chat list."""
    initialize_session(st.session_state)
    try:
        login_options = list_login_options()
    except Exception:
        LOGGER.exception("Could not load simulated customer login options")
        st.sidebar.error("Customer login options are temporarily unavailable.")
        return

    options_by_id = {option.customer_id: option for option in login_options}
    if not options_by_id:
        st.sidebar.error("No simulated customer login options are available.")
        return

    current_customer = st.session_state.get(AUTHENTICATED_CUSTOMER_ID_KEY)
    current_index = (
        list(options_by_id).index(current_customer)
        if current_customer in options_by_id
        else None
    )
    selected_customer = st.sidebar.selectbox(
        "Simulated login",
        options=list(options_by_id),
        format_func=lambda customer_id: options_by_id[customer_id].display_name,
        index=current_index,
        placeholder="Choose a customer",
        key="customer_selector",
    )

    changed = set_authenticated_customer_id(
        st.session_state,
        selected_customer,
    )
    if selected_customer is not None and changed:
        _begin_session(selected_customer)

    if selected_customer is not None:
        _render_sidebar_chats(selected_customer)


def _render_sidebar_chats(customer_id: str) -> None:
    """Show the current customer's chats and the new-chat action."""
    st.sidebar.subheader("Chats")
    if st.sidebar.button("New chat", key="new_chat"):
        _begin_session(customer_id)
        st.rerun()

    active_chat_id = st.session_state.get(ACTIVE_CHAT_ID_KEY)
    chats: dict[str, ChatSession] = st.session_state[CHATS_KEY]
    for chat_id, chat in chats.items():
        if chat_id == active_chat_id:
            st.sidebar.caption(f"Current: {chat['title']}")
        elif st.sidebar.button(chat["title"], key=f"chat-{chat_id}"):
            select_chat(st.session_state, chat_id)
            st.rerun()


def _show_backend_error(reply_error_code: str | None, request_id: str) -> None:
    """Show a safe error or restart a chat with an expired backend state."""
    if reply_error_code == "invalid_session":
        customer_id = st.session_state.get(AUTHENTICATED_CUSTOMER_ID_KEY)
        if isinstance(customer_id, str):
            chat = get_active_chat(st.session_state)
            _begin_session(customer_id, restart_chat=chat)
            if is_chat_enabled(st.session_state):
                st.session_state[UI_NOTICE_KEY] = (
                    "Your chat session expired. A new session has been started."
                )
            else:
                st.session_state[UI_ERROR_KEY] = (
                    "Your chat session expired and could not be restarted. "
                    "Use Start new session to try again."
                )
        else:
            st.session_state[UI_ERROR_KEY] = (
                "Your chat session expired. Please choose a customer to continue."
            )
        return

    if reply_error_code is not None:
        st.session_state[UI_ERROR_KEY] = _ERROR_MESSAGES.get(
            reply_error_code,
            "The assistant could not complete that request.",
        )
        st.session_state[REQUEST_ID_KEY] = request_id
    else:
        st.session_state[UI_ERROR_KEY] = None
        st.session_state.pop(REQUEST_ID_KEY, None)


def _send_backend_request(
    *,
    message: str | None = None,
    decision: ConfirmationDecision | None = None,
    choice: ChoiceSelection | None = None,
) -> None:
    """Send one input and save the reply in the active chat."""
    supplied_inputs = sum(value is not None for value in (message, decision, choice))
    if supplied_inputs != 1:
        raise ValueError("supply exactly one backend request input")

    chat = get_active_chat(st.session_state)
    if chat is None or chat["conversation_state"] is None:
        st.session_state[UI_ERROR_KEY] = (
            "No active chat session. Please start a new chat."
        )
        return

    state = chat["conversation_state"]
    try:
        with st.spinner("Thinking..."):
            if message is not None:
                reply = handle_message(state, message=message)
            elif decision is not None:
                reply = handle_message(state, decision=decision)
            else:
                assert choice is not None
                reply = handle_message(state, choice=choice)
    except Exception:
        LOGGER.exception("The customer support backend request failed")
        st.session_state[UI_ERROR_KEY] = (
            "The assistant could not complete that request. Please try again."
        )
        return

    if reply.error_code == "invalid_session":
        _show_backend_error(reply.error_code, reply.request_id)
        return

    chat["conversation_state"] = reply.state
    append_chat_message(st.session_state, "assistant", reply.text)
    chat["pending_view"] = reply.pending
    chat["choice_view"] = reply.choice
    _show_backend_error(reply.error_code, reply.request_id)


def _render_actions() -> None:
    """Render confirmation and choice controls for the active chat."""
    chat = get_active_chat(st.session_state)
    if chat is None:
        return

    pending = chat["pending_view"]
    if pending is not None:
        st.text(pending.summary)
        confirm_column, cancel_column = st.columns(2)
        if confirm_column.button("Confirm", key=f"confirm-{pending.action_id}"):
            _send_backend_request(
                decision=ConfirmationDecision(
                    action_id=pending.action_id,
                    accepted=True,
                )
            )
            st.rerun()
        if cancel_column.button("Cancel", key=f"cancel-{pending.action_id}"):
            _send_backend_request(
                decision=ConfirmationDecision(
                    action_id=pending.action_id,
                    accepted=False,
                )
            )
            st.rerun()

    choice = chat["choice_view"]
    if choice is not None:
        st.text(choice.question)
        for option in choice.options:
            if st.button(
                option.label,
                key=f"choice-{choice.prompt_id}-{option.option_id}",
            ):
                _send_backend_request(
                    choice=ChoiceSelection(
                        prompt_id=choice.prompt_id,
                        option_id=option.option_id,
                    )
                )
                st.rerun()


def _render_chat() -> None:
    """Render the active transcript and message composer."""
    st.title("E-commerce customer support")
    st.caption(
        "Synthetic data only. This is a demo login, not a production auth system."
    )

    error_message = st.session_state.get(UI_ERROR_KEY)
    if isinstance(error_message, str):
        st.error(error_message)
        request_id = st.session_state.get(REQUEST_ID_KEY)
        if isinstance(request_id, str):
            st.caption(f"Support reference: {request_id}")

    notice = st.session_state.get(UI_NOTICE_KEY)
    if isinstance(notice, str):
        st.info(notice)
        st.session_state[UI_NOTICE_KEY] = None

    if not is_chat_enabled(st.session_state):
        if not isinstance(
            st.session_state.get(AUTHENTICATED_CUSTOMER_ID_KEY),
            str,
        ):
            st.info("Please choose a customer in the sidebar to begin.")
        else:
            st.info("Start a new chat from the sidebar to continue.")
        return

    customer_id = st.session_state[AUTHENTICATED_CUSTOMER_ID_KEY]
    st.subheader(f"Logged in as {customer_id}")
    chat = get_active_chat(st.session_state)
    assert chat is not None

    for chat_message in chat["messages"]:
        with st.chat_message(chat_message["role"]):
            st.text(chat_message["content"])

    _render_actions()
    prompt = st.chat_input("Ask about an order, return, policy, or product...")
    if prompt is not None:
        append_chat_message(st.session_state, "user", prompt)
        _send_backend_request(message=prompt)
        st.rerun()


def main() -> None:
    """Run one Streamlit app render cycle."""
    initialize_session(st.session_state)
    _set_customer_from_sidebar()
    _render_chat()


if __name__ == "__main__":
    main()
