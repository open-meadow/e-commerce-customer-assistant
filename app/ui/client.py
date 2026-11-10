def handle_turn(
        authenticated_customer_id: str,
        message: str,
        history: list[dict[str, str]]
) -> str:
    return (
        f"[Demo only; authenticated customer: {authenticated_customer_id}] "
        f"I received: {message!r}. "
        f"There are {len(history)} earlier message(s) in this chat. "
        "Live order, return, policy, and ticket answers are not connected yet."
    )