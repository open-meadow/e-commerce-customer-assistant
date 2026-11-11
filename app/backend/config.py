"""Backend limits (docs/03 Sections 5.3, 8, 11, 13).

Values marked proposed in the design are defaults here and can be
overridden per backend instance (tests do this). The approved fixed
values are the D4 text limits in ``app.db.validation``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class Settings:
    max_message_chars: int = 2000
    max_tool_calls_per_turn: int = 6
    max_messages_per_session: int = 60
    max_tickets_per_session: int = 3
    max_returns_per_session: int = 5
    idle_timeout: timedelta = timedelta(minutes=60)
    pending_ttl: timedelta = timedelta(minutes=10)
    history_turns: int = 10
    clarification_max_messages: int = 3
    clarification_ttl: timedelta = timedelta(minutes=10)
    max_output_tokens: int = 512
    write_cap_kinds: tuple[str, ...] = ("return_request", "support_ticket")

    def cap_for(self, kind: str) -> int:
        return {"return_request": self.max_returns_per_session,
                "support_ticket": self.max_tickets_per_session}[kind]
