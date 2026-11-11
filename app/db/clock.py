"""Centralized UTC clock and canonical timestamp helpers (docs/01 Section C).

Every business timestamp is canonical UTC text ``YYYY-MM-DDTHH:MM:SSZ``.
Repository code must obtain the current time **only** through
``clock.utc_now()`` (called as a module attribute) so tests can
monkeypatch one place. There is no persistent global override and no
production caller can supply a timestamp.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.db.errors import DatabaseDataIntegrityError

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_CANONICAL_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    Whole seconds only: microseconds are dropped (truncated, never
    rounded), matching the canonical timestamp format.
    """
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_timestamp(value: datetime) -> str:
    """Format an aware ``datetime`` as the canonical UTC timestamp.

    Raises:
        TypeError: if *value* is not a ``datetime``.
        ValueError: if *value* is timezone-naive.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime rejected; supply an aware datetime")
    return value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: Any) -> datetime:
    """Parse an exact canonical timestamp string into an aware UTC datetime.

    Raises:
        TypeError: if *value* is not a ``str``.
        ValueError: if it is not exactly ``YYYY-MM-DDTHH:MM:SSZ`` or is
            not a valid calendar date and time.
    """
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    if _CANONICAL_RE.fullmatch(value) is None:
        raise ValueError(f"not a canonical UTC timestamp: {value!r}")
    try:
        parsed = datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise ValueError(f"invalid calendar timestamp: {value!r}") from exc
    return parsed.replace(tzinfo=timezone.utc)


def validate_persisted_timestamp(
    value: Any, column: str, now: datetime, *, nullable: bool = False
) -> datetime | None:
    """Validate a timestamp read from the database against *now*.

    The captured *now* is the operation's single UTC instant. A value
    equal to *now* is allowed; a later value is a data-integrity error.

    Raises:
        DatabaseDataIntegrityError: if the stored value is NULL where
            not allowed, malformed, not canonical, impossible, or later
            than *now*.
    """
    if value is None:
        if nullable:
            return None
        raise DatabaseDataIntegrityError(f"{column} is NULL")
    try:
        parsed = parse_timestamp(value)
    except (TypeError, ValueError) as exc:
        raise DatabaseDataIntegrityError(f"{column} is not a canonical timestamp") from exc
    if parsed > now:
        raise DatabaseDataIntegrityError(f"{column} is later than the current time")
    return parsed
