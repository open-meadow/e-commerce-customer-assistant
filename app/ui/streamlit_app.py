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
else:
    from client import (
        ChoiceSelection,
        ConfirmationDecision,
        handle_message,
        list_login_options,
        start_session,
    )
    from session import (
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

LOGGER = logging.getLogger(__name__)

_ERROR_MESSAGES = {
    "message_too_long": "Please shorten your message and try again.",
    "rate_limited": "Please wait a moment before sending another message.",
    "unavailable": "The assistant is temporarily unavailable.",
    "internal": "Something went wrong. Please try again.",
}


def _begin_session(customer_id: str) -> None:
    clear_conversation(st.session_state)
    st.session_state[UI_ERROR_KEY] = None

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

    set_conversation_state(st.session_state, result.state)
    append_message(st.session_state, "assistant", result.greeting)


def _set_customer_from_sidebar() -> None:
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

    if selected_customer is not None and st.sidebar.button(
        "Start new session",
        key="start_new_session",
    ):
        _begin_session(selected_customer)


def _show_backend_error(reply_error_code: str | None, request_id: str) -> None:
    if reply_error_code == "invalid_session":
        customer_id = st.session_state.get(AUTHENTICATED_CUSTOMER_ID_KEY)
        if isinstance(customer_id, str):
            _begin_session(customer_id)
            if st.session_state.get(CONVERSATION_STATE_KEY) is not None:
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
    supplied_inputs = sum(value is not None for value in (message, decision, choice))
    if supplied_inputs != 1:
        raise ValueError("supply exactly one backend request input")

    state = st.session_state.get(CONVERSATION_STATE_KEY)
    if state is None:
        st.session_state[UI_ERROR_KEY] = (
            "No active chat session. Please start a new session."
        )
        return

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

    set_conversation_state(st.session_state, reply.state)
    append_message(st.session_state, "assistant", reply.text)
    st.session_state[PENDING_VIEW_KEY] = reply.pending
    st.session_state[CHOICE_VIEW_KEY] = reply.choice
    _show_backend_error(reply.error_code, reply.request_id)


def _render_actions() -> None:
    pending = st.session_state.get(PENDING_VIEW_KEY)
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

    choice = st.session_state.get(CHOICE_VIEW_KEY)
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
        return

    customer_id = st.session_state[AUTHENTICATED_CUSTOMER_ID_KEY]
    st.subheader(f"Logged in as {customer_id}")

    for chat_message in st.session_state[MESSAGES_KEY]:
        with st.chat_message(chat_message["role"]):
            st.text(chat_message["content"])

    _render_actions()
    prompt = st.chat_input("Ask about an order, return, policy, or product...")
    if prompt is not None:
        append_message(st.session_state, "user", prompt)
        _send_backend_request(message=prompt)
        st.rerun()


def main() -> None:
    initialize_session(st.session_state)
    _set_customer_from_sidebar()
    _render_chat()


if __name__ == "__main__":
    main()
