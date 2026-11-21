"""Backend limits (docs/03 Sections 5.3, 8, 11, 13) and H3 session and resource protection.

Values marked proposed in the design are defaults here and can be
overridden per backend instance (tests do this) or, for deployment, through the
``SUPPORT_*`` environment variables read by ``Settings.from_env`` (documented in
docs/session_resource_protection.md and ``.env.example``). The approved fixed
values are the D4 text limits in ``app.db.validation``.

``Settings.from_env`` REJECTS an invalid value (unparsable, not finite, below its minimum or above its maximum) and an
inconsistent combination, listing every problem at once, so a mistake fails at startup instead of silently weakening a
protection. A direct ``Settings(...)`` is not validated (tests build deliberately tiny limits).

Every limit here is enforced per process: see docs/session_resource_protection.md for what a
multi-instance deployment would need.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Mapping, Optional


class ConfigError(ValueError):
    """The environment holds an invalid ``SUPPORT_*`` value. The message names the variable, never a secret."""


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
    session_creates_per_client_per_minute: int = 10     # SUPPORT_SESSIONS_PER_CLIENT_PER_MINUTE (per browser or peer)
    session_creates_per_minute: int = 60                # SUPPORT_SESSIONS_PER_MINUTE (all clients; also covers unidentified callers)
    max_session_age: timedelta = timedelta(hours=8)     # SUPPORT_MAX_SESSION_AGE_SECONDS (absolute; activity does not extend it)
    max_requests_per_session: int = 300                 # SUPPORT_MAX_REQUESTS_PER_SESSION (messages, decisions and choices)
    session_requests_per_minute: int = 30               # SUPPORT_REQUESTS_PER_SESSION_PER_MINUTE
    turn_lock_timeout_seconds: float = 40.0             # SUPPORT_TURN_LOCK_TIMEOUT_SECONDS
    provider_calls_per_session: int = 300               # SUPPORT_PROVIDER_CALLS_PER_SESSION (model calls)
    provider_calls_per_minute: int = 240                # SUPPORT_PROVIDER_CALLS_PER_MINUTE (all sessions together)
    client_requests_per_minute: int = 120               # SUPPORT_REQUESTS_PER_CLIENT_PER_MINUTE (HTTP, per browser or peer)
    max_body_bytes: int = 16384                         # SUPPORT_MAX_BODY_BYTES (HTTP request body)
    livekit_timeout_seconds: float = 10.0               # SUPPORT_LIVEKIT_TIMEOUT_SECONDS (one dispatch or room release)
    sweep_interval_seconds: float = 60.0                # SUPPORT_SWEEP_INTERVAL_SECONDS (background cleanup)
    shutdown_timeout_seconds: float = 20.0              # SUPPORT_SHUTDOWN_TIMEOUT_SECONDS (all room releases at shutdown)
    # Service-to-service secrets. Never printed: ``repr=False`` keeps them out of logs and error messages.
    agent_token: str = field(default="", repr=False)    # SUPPORT_AGENT_TOKEN (voice worker -> /api/agent/*)
    ui_token: str = field(default="", repr=False)       # SUPPORT_UI_TOKEN (Streamlit -> API: its browser ids are trusted)

    def cap_for(self, kind: str) -> int:
        return {"return_request": self.max_returns_per_session,
                "support_ticket": self.max_tickets_per_session}[kind]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """Settings with the ``SUPPORT_*`` overrides applied; raises ``ConfigError`` listing every invalid value."""
        problems: list[str] = []
        chosen: dict[str, object] = {}
        for name, variable, kind, low, high in _BOUNDS:
            raw = (env.get(variable) or "").strip()
            if not raw:
                continue
            try:
                value = int(raw) if kind == "int" else float(raw)
            except ValueError:
                problems.append(f"{variable} is not a number")
                continue
            if isinstance(value, float) and not math.isfinite(value):
                problems.append(f"{variable} is not a finite number")
                continue
            if not low <= value <= high:
                problems.append(f"{variable} must be between {_num(low)} and {_num(high)}")
                continue
            chosen[name] = timedelta(seconds=value) if kind == "seconds" else value
        for name, variable in (("agent_token", "SUPPORT_AGENT_TOKEN"), ("ui_token", "SUPPORT_UI_TOKEN")):
            raw = env.get(variable)
            if raw is None or raw == "":
                continue
            if not valid_token(raw):
                problems.append(f"{variable} must be 16 to 256 printable characters with no spaces")
                continue
            chosen[name] = raw
        settings = cls(**chosen)  # type: ignore[arg-type]
        problems += settings.consistency_problems()
        if problems:
            raise ConfigError("Invalid configuration: " + "; ".join(problems))
        return settings

    def consistency_problems(self) -> list[str]:
        """Combinations that would silently defeat a protection (checked for environment settings)."""
        out: list[str] = []
        if self.idle_timeout > self.max_session_age:
            out.append("SUPPORT_IDLE_TIMEOUT_SECONDS must not exceed SUPPORT_MAX_SESSION_AGE_SECONDS")
        if timedelta(seconds=self.sweep_interval_seconds) > self.idle_timeout:
            out.append("SUPPORT_SWEEP_INTERVAL_SECONDS must not exceed SUPPORT_IDLE_TIMEOUT_SECONDS")
        if self.session_creates_per_client_per_minute > self.session_creates_per_minute:
            out.append("SUPPORT_SESSIONS_PER_CLIENT_PER_MINUTE must not exceed SUPPORT_SESSIONS_PER_MINUTE")
        if self.max_body_bytes < self.max_message_chars * 6 + 1024:
            out.append("SUPPORT_MAX_BODY_BYTES is too small for a maximum-length message (JSON may escape each character to 6 bytes)")
        if self.agent_token and self.agent_token == self.ui_token:
            out.append("SUPPORT_AGENT_TOKEN and SUPPORT_UI_TOKEN must be different secrets")
        return out


# name, environment variable, kind, minimum, maximum (inclusive). ``seconds`` is parsed as a number and stored as a timedelta.
_BOUNDS = (
    ("max_active_sessions", "SUPPORT_MAX_SESSIONS", "int", 1, 10000),
    ("session_creates_per_client_per_minute", "SUPPORT_SESSIONS_PER_CLIENT_PER_MINUTE", "int", 1, 600),
    ("session_creates_per_minute", "SUPPORT_SESSIONS_PER_MINUTE", "int", 1, 6000),
    ("idle_timeout", "SUPPORT_IDLE_TIMEOUT_SECONDS", "seconds", 60, 86400),
    ("max_session_age", "SUPPORT_MAX_SESSION_AGE_SECONDS", "seconds", 300, 604800),
    ("max_requests_per_session", "SUPPORT_MAX_REQUESTS_PER_SESSION", "int", 10, 5000),
    ("session_requests_per_minute", "SUPPORT_REQUESTS_PER_SESSION_PER_MINUTE", "int", 1, 600),
    ("turn_lock_timeout_seconds", "SUPPORT_TURN_LOCK_TIMEOUT_SECONDS", "float", 1, 300),
    ("provider_calls_per_session", "SUPPORT_PROVIDER_CALLS_PER_SESSION", "int", 10, 10000),
    ("provider_calls_per_minute", "SUPPORT_PROVIDER_CALLS_PER_MINUTE", "int", 10, 100000),
    ("client_requests_per_minute", "SUPPORT_REQUESTS_PER_CLIENT_PER_MINUTE", "int", 1, 6000),
    ("max_body_bytes", "SUPPORT_MAX_BODY_BYTES", "int", 4096, 1048576),
    ("livekit_timeout_seconds", "SUPPORT_LIVEKIT_TIMEOUT_SECONDS", "float", 1, 60),
    ("sweep_interval_seconds", "SUPPORT_SWEEP_INTERVAL_SECONDS", "float", 1, 3600),
    ("shutdown_timeout_seconds", "SUPPORT_SHUTDOWN_TIMEOUT_SECONDS", "float", 1, 300),
)


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def valid_token(value: str) -> bool:
    """A service secret: 16 to 256 printable ASCII characters, no spaces. (Strength beyond that is the operator's job.)"""
    return 16 <= len(value) <= 256 and value.isascii() and value.isprintable() and not any(c.isspace() for c in value)


# The voice worker's own limits (read by the worker process, which does not build a Backend).
DEFAULT_VOICE_MAX_SECONDS = 1800.0
DEFAULT_VOICE_IDLE_SECONDS = 300.0
_VOICE_BOUNDS = (
    ("SUPPORT_VOICE_MAX_SECONDS", DEFAULT_VOICE_MAX_SECONDS, 30.0, 14400.0),
    ("SUPPORT_VOICE_IDLE_SECONDS", DEFAULT_VOICE_IDLE_SECONDS, 10.0, 3600.0),
)


def voice_limits_from_env(env: Mapping[str, str]) -> tuple[float, float]:
    """``(max_seconds, idle_seconds)`` for a voice call. Raises ``ConfigError`` for an invalid value."""
    values, problems = [], []
    for variable, default, low, high in _VOICE_BOUNDS:
        raw = (env.get(variable) or "").strip()
        if not raw:
            values.append(default)
            continue
        try:
            value = float(raw)
        except ValueError:
            problems.append(f"{variable} is not a number")
            values.append(default)
            continue
        if not math.isfinite(value) or not low <= value <= high:
            problems.append(f"{variable} must be between {_num(low)} and {_num(high)}")
        values.append(value)
    if not problems and values[1] > values[0]:
        problems.append("SUPPORT_VOICE_IDLE_SECONDS must not exceed SUPPORT_VOICE_MAX_SECONDS")
    if problems:
        raise ConfigError("Invalid configuration: " + "; ".join(problems))
    return values[0], values[1]


def token_from_env(env: Mapping[str, str], variable: str) -> Optional[str]:
    """A configured service secret, ``None`` if unset; raises ``ConfigError`` (without the value) if malformed."""
    raw = env.get(variable)
    if raw is None or raw == "":
        return None
    if not valid_token(raw):
        raise ConfigError(f"Invalid configuration: {variable} must be 16 to 256 printable characters with no spaces")
    return raw
