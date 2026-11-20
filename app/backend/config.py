"""Backend limits (docs/03 Sections 5.3, 8, 11, 13) and H3 session and resource protection.

Values marked proposed in the design are defaults here and can be
overridden per backend instance (tests do this) or, for deployment, through the
``SUPPORT_*`` environment variables read by ``Settings.from_env`` (documented in
docs/session_resource_protection.md and ``.env.example``). The approved fixed
values are the D4 text limits in ``app.db.validation``.

Every limit here is enforced per process: see docs/session_resource_protection.md for what a
multi-instance deployment would need.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import timedelta
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    max_message_chars: int = 2000
    max_tool_calls_per_turn: int = 6
    max_messages_per_session: int = 60
    max_tickets_per_session: int = 3
    max_returns_per_session: int = 5
    idle_timeout: timedelta = timedelta(minutes=60)      # SUPPORT_IDLE_TIMEOUT_SECONDS
    pending_ttl: timedelta = timedelta(minutes=10)
    history_turns: int = 10
    clarification_max_messages: int = 3
    clarification_ttl: timedelta = timedelta(minutes=10)
    max_output_tokens: int = 512
    write_cap_kinds: tuple[str, ...] = ("return_request", "support_ticket")

    # --- H3: session and resource protection (all process-local) -------------------------------------------
    max_active_sessions: int = 200                      # SUPPORT_MAX_SESSIONS
    session_creates_per_client_per_minute: int = 10     # SUPPORT_SESSIONS_PER_CLIENT_PER_MINUTE
    session_creates_per_minute: int = 60                # SUPPORT_SESSIONS_PER_MINUTE (all clients together)
    max_session_age: timedelta = timedelta(hours=8)     # SUPPORT_MAX_SESSION_AGE_SECONDS (absolute; activity does not extend it)
    max_requests_per_session: int = 300                 # SUPPORT_MAX_REQUESTS_PER_SESSION (messages, decisions and choices)
    session_requests_per_minute: int = 30               # SUPPORT_REQUESTS_PER_SESSION_PER_MINUTE
    turn_lock_timeout_seconds: float = 40.0             # SUPPORT_TURN_LOCK_TIMEOUT_SECONDS
    provider_calls_per_session: int = 300               # SUPPORT_PROVIDER_CALLS_PER_SESSION (model calls)
    provider_calls_per_minute: int = 240                # SUPPORT_PROVIDER_CALLS_PER_MINUTE (all sessions together)
    client_requests_per_minute: int = 120               # SUPPORT_REQUESTS_PER_CLIENT_PER_MINUTE (HTTP, per peer address)
    max_body_bytes: int = 16384                         # SUPPORT_MAX_BODY_BYTES (HTTP request body)
    livekit_timeout_seconds: float = 10.0               # SUPPORT_LIVEKIT_TIMEOUT_SECONDS (dispatch and room release)
    sweep_interval_seconds: float = 60.0                # SUPPORT_SWEEP_INTERVAL_SECONDS (background cleanup)

    def cap_for(self, kind: str) -> int:
        return {"return_request": self.max_returns_per_session,
                "support_ticket": self.max_tickets_per_session}[kind]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """Settings with the ``SUPPORT_*`` overrides applied. An unset, unparsable or non-positive value keeps the default."""

        def number(name: str, kind=int):
            raw = (env.get(name) or "").strip()
            if not raw:
                return None
            try:
                value = kind(raw)
            except ValueError:
                return None
            return value if value > 0 else None

        def seconds(name: str):
            value = number(name, float)
            return None if value is None else timedelta(seconds=value)

        chosen = {
            "max_active_sessions": number("SUPPORT_MAX_SESSIONS"),
            "session_creates_per_client_per_minute": number("SUPPORT_SESSIONS_PER_CLIENT_PER_MINUTE"),
            "session_creates_per_minute": number("SUPPORT_SESSIONS_PER_MINUTE"),
            "idle_timeout": seconds("SUPPORT_IDLE_TIMEOUT_SECONDS"),
            "max_session_age": seconds("SUPPORT_MAX_SESSION_AGE_SECONDS"),
            "max_requests_per_session": number("SUPPORT_MAX_REQUESTS_PER_SESSION"),
            "session_requests_per_minute": number("SUPPORT_REQUESTS_PER_SESSION_PER_MINUTE"),
            "turn_lock_timeout_seconds": number("SUPPORT_TURN_LOCK_TIMEOUT_SECONDS", float),
            "provider_calls_per_session": number("SUPPORT_PROVIDER_CALLS_PER_SESSION"),
            "provider_calls_per_minute": number("SUPPORT_PROVIDER_CALLS_PER_MINUTE"),
            "client_requests_per_minute": number("SUPPORT_REQUESTS_PER_CLIENT_PER_MINUTE"),
            "max_body_bytes": number("SUPPORT_MAX_BODY_BYTES"),
            "livekit_timeout_seconds": number("SUPPORT_LIVEKIT_TIMEOUT_SECONDS", float),
            "sweep_interval_seconds": number("SUPPORT_SWEEP_INTERVAL_SECONDS", float),
        }
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in chosen.items() if v is not None and k in known})
