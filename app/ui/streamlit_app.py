import logging

import streamlit as st

st.set_page_config(
    page_title="Customer support",
    page_icon=":material/support_agent:",
    layout="centered",
    initial_sidebar_state="expanded",
)

if __package__:
    from .client import (
        ChoiceSelection,
        ConfirmationDecision,
        backend_status,
        handle_message,
        list_login_options,
        start_session,
    )
    from .voice import VoiceTranscriptionError, transcribe_audio
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
        backend_status,
        handle_message,
        list_login_options,
        start_session,
    )
    from voice import VoiceTranscriptionError, transcribe_audio
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

_SUGGESTIONS = {
    ":material/local_shipping: Check an order": "Where is my order?",
    ":material/policy: Return policy": "What is your return policy?",
    ":material/inventory_2: Product details": (
        "Tell me about the wireless earbuds"
    ),
}

# Fallbacks only: a backend error reply carries its own customer-safe text, which
# is shown in preference to these.
_ERROR_MESSAGES = {
    "invalid_request": "That request could not be processed. Please try again.",
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


def _require_backend() -> None:
    """Stop the page with a clear message when the backend cannot serve requests.

    Runs on every rerun before the login selector, so a missing database or a
    misconfigured provider is explained instead of showing an empty selector.
    """
    status = backend_status()
    if status.ready:
        return

    st.title("E-commerce customer support")
    st.error(status.message)
    if status.code == "unavailable":
        st.button("Try again")  # any click reruns the script, which checks again
    else:
        st.caption("Fix the problem above, then reload this page.")
    st.stop()


def _set_customer_from_sidebar() -> None:
    """Render the simulated login and its customer-scoped chat list."""
    initialize_session(st.session_state)
    st.sidebar.subheader("Demo customer")
    st.sidebar.caption("Synthetic data · simulated login")
    try:
        login_options = list_login_options()
    except Exception:
        LOGGER.exception("Could not load simulated customer login options")
        st.sidebar.error("Customer login options are temporarily unavailable.")
        return

    options_by_id = {option.customer_id: option for option in login_options}
    if not options_by_id:
        status = backend_status()
        st.sidebar.error(
            status.message
            if not status.ready
            else "No simulated customer login options are available."
        )
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
    st.sidebar.subheader("Your chats")
    if st.sidebar.button(
        "New chat",
        key="new_chat",
        icon=":material/add_comment:",
        width="stretch",
    ):
        _begin_session(customer_id)
        st.rerun()

    active_chat_id = st.session_state.get(ACTIVE_CHAT_ID_KEY)
    chats: dict[str, ChatSession] = st.session_state[CHATS_KEY]
    for chat_id, chat in chats.items():
        if st.sidebar.button(
            chat["title"],
            key=f"chat-{chat_id}",
            type="primary" if chat_id == active_chat_id else "secondary",
            icon=":material/chat_bubble_outline:",
            width="stretch",
        ):
            select_chat(st.session_state, chat_id)
            st.rerun()


def _show_backend_error(
    reply_error_code: str | None,
    request_id: str,
    reply_text: str | None = None,
) -> None:
    """Show a safe error or restart a chat with an expired backend state.

    ``reply_text`` is the backend's own customer-safe error text. It is shown in
    preference to the fixed fallback messages.
    """
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
        st.session_state[UI_ERROR_KEY] = reply_text or _ERROR_MESSAGES.get(
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
    if reply.status == "error":
        # An error reply changes nothing on the backend: keep any open confirmation
        # or choice on screen, and show the message as a banner, not as a chat turn.
        _show_backend_error(reply.error_code, reply.request_id, reply.text)
        return

    append_chat_message(st.session_state, "assistant", reply.text)
    chat["pending_view"] = reply.pending
    chat["choice_view"] = reply.choice
    _show_backend_error(None, reply.request_id)


def _render_actions() -> None:
    """Render confirmation and choice controls for the active chat."""
    chat = get_active_chat(st.session_state)
    if chat is None:
        return

    pending = chat["pending_view"]
    if pending is not None:
        with st.container(border=True):
            st.markdown("**Confirm this request**")
            st.write(pending.summary)
            st.caption(f"Expires at {pending.expires_at} UTC")
            with st.container(horizontal=True, vertical_alignment="center"):
                if st.button(
                    "Confirm",
                    key=f"confirm-{pending.action_id}",
                    type="primary",
                    icon=":material/check:",
                ):
                    _send_backend_request(
                        decision=ConfirmationDecision(
                            action_id=pending.action_id,
                            accepted=True,
                        )
                    )
                    st.rerun()
                if st.button(
                    "Cancel",
                    key=f"cancel-{pending.action_id}",
                    icon=":material/close:",
                ):
                    _send_backend_request(
                        decision=ConfirmationDecision(
                            action_id=pending.action_id,
                            accepted=False,
                        )
                    )
                    st.rerun()

    choice = chat["choice_view"]
    if choice is not None:
        with st.container(border=True):
            st.write(choice.question)
            with st.container(horizontal=True):
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
    st.title("Customer support", icon=":material/support_agent:")
    st.caption("Order, return, product, and policy help · Demo with synthetic data")

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
            st.subheader("How can we help?")
            st.write("Choose a demo customer in the sidebar to start a chat.")
        else:
            st.caption("Start a new chat from the sidebar to continue.")
        return

    chat = get_active_chat(st.session_state)
    assert chat is not None
    has_user_message = any(
        message["role"] == "user" for message in chat["messages"]
    )
    if not has_user_message:
        st.subheader("How can I help?")

    for chat_message in chat["messages"]:
        avatar = (
            ":material/support_agent:"
            if chat_message["role"] == "assistant"
            else ":material/person:"
        )
        with st.chat_message(chat_message["role"], avatar=avatar):
            st.text(chat_message["content"])

    if not has_user_message:
        suggestion = st.pills(
            "Try asking",
            options=list(_SUGGESTIONS),
            key=f"suggestions-{st.session_state[ACTIVE_CHAT_ID_KEY]}",
        )
    else:
        suggestion = None

    _render_actions()
    st.caption(
        "Voice recordings are sent to OpenAI for transcription. If both voice "
        "and text are submitted, voice takes priority."
    )
    prompt = st.chat_input(
        "Ask about an order, return, product, or policy",
        accept_audio=True,
        key="chat_input",
    )
    if suggestion is not None:
        message = _SUGGESTIONS[suggestion]
        audio = None
    elif prompt is None:
        message = None
        audio = None
    else:
        message = prompt.text
        audio = prompt.audio

    if audio is not None:
        try:
            with st.spinner("Transcribing voice message..."):
                message = transcribe_audio(audio)
        except VoiceTranscriptionError as exc:
            st.error(str(exc))
            return

    if message:
        append_chat_message(st.session_state, "user", message)
        _send_backend_request(message=message)
        st.rerun()


def main() -> None:
    """Run one Streamlit app render cycle."""
    initialize_session(st.session_state)
    _require_backend()
    _set_customer_from_sidebar()
    _render_chat()


if __name__ == "__main__":
    main()
