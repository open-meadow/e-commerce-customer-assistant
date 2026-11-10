import streamlit as st

if __package__:
    from .client import handle_turn
    from .session import (
        AUTHENTICATED_CUSTOMER_ID_KEY,
        MESSAGES_KEY,
        initialize_session,
        is_chat_enabled,
        set_authenticated_customer_id,
    )
else:
    from client import handle_turn
    from session import (
        AUTHENTICATED_CUSTOMER_ID_KEY,
        MESSAGES_KEY,
        initialize_session,
        is_chat_enabled,
        set_authenticated_customer_id,
    )

SIMULATED_CUSTOMERS = {
    "C101": "Customer C101",
    "C102": "Customer C102",
}

def _set_customer_from_sidebar() -> None:
    initialize_session(st.session_state)
    current = st.session_state.get(AUTHENTICATED_CUSTOMER_ID_KEY)
    selected = st.sidebar.selectbox(
        "Simulated login",
        options=list(SIMULATED_CUSTOMERS.keys()),
        format_func=lambda customer_id: SIMULATED_CUSTOMERS[customer_id],
        index=(
            list(SIMULATED_CUSTOMERS.keys()).index(current)
            if current in SIMULATED_CUSTOMERS
            else None
        ),
        placeholder="Choose a customer",
        key="customer_selector",
    )
    set_authenticated_customer_id(st.session_state, selected)
    st.sidebar.caption(
        f"Active customer_id: {selected if selected is not None else 'None'}"
    )

def _render_chat() -> None:
    initialize_session(st.session_state)

    st.title("E-commerce customer support")
    st.caption("Synthetic data only. This is a demo login, not a production auth system.")

    if not is_chat_enabled(st.session_state):
        st.info("Please choose a customer in the sidebar to begin.")
        st.stop()

    customer_id = st.session_state[AUTHENTICATED_CUSTOMER_ID_KEY]
    st.subheader(f"Logged in as {customer_id}")

    for message in st.session_state.get(MESSAGES_KEY, []):
        with st.chat_message(message["role"]):
            st.write(message["content"])

    if prompt := st.chat_input(
        "Ask about an order, return, policy, or product...",
        disabled=not is_chat_enabled(st.session_state),
    ):
        messages = st.session_state.setdefault(MESSAGES_KEY, [])
        messages.append({"role": "user", "content": prompt})
        prior_messages = messages[:-1]

        try:
            reply = handle_turn(
                authenticated_customer_id=customer_id,
                message=prompt,
                history=prior_messages,
            )
        except Exception:
            messages.append({"role": "assistant", "content": "Sorry, the chat stub failed."})
        else:
            messages.append({"role": "assistant", "content": reply})

        st.rerun()

def main() -> None:
    _set_customer_from_sidebar()
    _render_chat()

if __name__ == "__main__":
    main()