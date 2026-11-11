"""Input validation shared by repository functions (docs/01 Section C).

Python validation is authoritative. SQLite ``CHECK`` constraints are a
defensive backstop and are **not** claimed to share Python's Unicode
whitespace semantics.
"""

from __future__ import annotations

import re
from typing import Any

_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")

RETURN_REASON_MAX = 1000
TICKET_TEXT_MAX = 2000

RETURN_CATEGORIES = ("damaged", "wrong_product", "standard")
ISSUE_TYPES = ("order", "return", "payment", "product", "general")


def validate_identifier(value: Any, name: str) -> str:
    """Validate a required identifier: ``str``, not blank, no NUL/surrogate.

    Raises:
        TypeError: if *value* is not a ``str``.
        ValueError: if it is blank, or contains NUL or a lone surrogate.
    """
    if not isinstance(value, str):
        raise TypeError(f"{name!r} must be a string; got {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{name!r} must not be blank")
    if "\x00" in value or _LONE_SURROGATE_RE.search(value):
        raise ValueError(f"{name!r} contains an invalid character")
    return value


def validate_text(value: Any, name: str, max_length: int) -> str:
    """Validate customer-visible free text and return it unchanged.

    Rules: ``str`` (``TypeError`` otherwise); no NUL; no lone surrogate;
    not blank by Python ``.strip()``; at most *max_length* Unicode code
    points. The text is never stripped, normalized or truncated.
    """
    if not isinstance(value, str):
        raise TypeError(f"{name!r} must be a string; got {type(value).__name__}")
    if "\x00" in value:
        raise ValueError(f"{name!r} must not contain NUL")
    if _LONE_SURROGATE_RE.search(value):
        raise ValueError(f"{name!r} must not contain lone surrogates")
    if not value.strip():
        raise ValueError(f"{name!r} must not be blank")
    if len(value) > max_length:
        raise ValueError(f"{name!r} must be at most {max_length} characters")
    return value


def validate_category(value: Any) -> str:
    """Validate ``reason_category``: ``str`` and one of the three values."""
    if not isinstance(value, str):
        raise TypeError(f"'reason_category' must be a string; got {type(value).__name__}")
    if value not in RETURN_CATEGORIES:
        raise ValueError(f"'reason_category' must be one of {RETURN_CATEGORIES}")
    return value


def validate_issue_type(value: Any) -> str:
    """Validate ``issue_type`` against the exact allowlist."""
    if not isinstance(value, str):
        raise TypeError(f"'issue_type' must be a string; got {type(value).__name__}")
    if value not in ISSUE_TYPES:
        raise ValueError(f"'issue_type' must be one of {ISSUE_TYPES}")
    return value
