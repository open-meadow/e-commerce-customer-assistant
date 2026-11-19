"""Trusted customer context (docs/03 Section 3).

Created only by the backend from a valid server-side session. It carries
the authenticated customer ID and a short, non-reversible ``session_ref``
that is safe to log. It does **not** carry the session handle, which is a
bearer credential. It is never an argument the LLM can supply.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CustomerContext:
    """Immutable identity passed to every customer-scoped tool."""

    customer_id: str
    session_ref: str
    # The customer's language, decided by code from their own message ("en", "hinglish" or "hi"). Tools may use it
    # to return ready-made customer-facing wording; it carries no authority.
    language: str = "en"

    def __post_init__(self) -> None:
        if not isinstance(self.customer_id, str) or not self.customer_id.strip():
            raise ValueError("customer_id must be a non-blank string")
        if not isinstance(self.session_ref, str):
            raise TypeError("session_ref must be a string")
        if self.language not in ("en", "hinglish", "hi"):
            raise ValueError("language must be en, hinglish or hi")
