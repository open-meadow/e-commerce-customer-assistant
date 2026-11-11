"""Support-issue lifecycle (docs/03 Section 8.10).

At most two tracked issues: one active and one suspended. All state
changes are made by orchestrator code. The LLM only proposes a relation
label, which is validated before use.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

_MAX_DESCRIPTION = 2000
_ELLIPSIS = "…"


@dataclass
class Issue:
    """One tracked support issue and only its own messages."""

    issue_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    messages: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.messages)


@dataclass
class IssueBook:
    active: Optional[Issue] = None
    suspended: Optional[Issue] = None

    def clear(self) -> None:
        self.active = None
        self.suspended = None

    def start(self, message: str) -> Issue:
        """Start a new active issue. The slot must be free (callers check)."""
        self.active = Issue(messages=[message])
        return self.active

    def suspend_active_and_start(self, message: str) -> Issue:
        """Suspend the active issue and make a new one active."""
        self.suspended = self.active
        return self.start(message)

    def swap_to_suspended(self) -> Optional[Issue]:
        """Suspend the active issue and promote the suspended one."""
        if self.suspended is None:
            return None
        self.active, self.suspended = self.suspended, self.active
        return self.active

    def abandon(self, which: str) -> None:
        """Remove only the chosen issue (``active`` or ``suspended``)."""
        if which == "active":
            self.active = None
        elif which == "suspended":
            self.suspended = None

    def end_active(self) -> Optional[Issue]:
        """End the active issue and promote the suspended one, if any."""
        self.active, self.suspended = self.suspended, None
        return self.active

    @property
    def full(self) -> bool:
        return self.active is not None and self.suspended is not None


def _trim_message(text: str) -> str:
    if len(text) <= _MAX_DESCRIPTION:
        return text
    return text[:999] + _ELLIPSIS + text[-1000:]


def build_description(messages: list[str]) -> str:
    """Join the issue's customer messages into a ticket description.

    Exact text in chronological order, one newline between messages, no
    labels and no blank lines. If the total exceeds 2,000 code points,
    the most recent complete messages are kept; a single over-long
    message keeps its first 999 code points, one ellipsis and its last
    1,000 code points (2,000 in total).
    """
    chosen: list[str] = []
    used = 0
    for message in reversed(messages):
        piece = _trim_message(message)
        cost = len(piece) + (1 if chosen else 0)
        if chosen and used + cost > _MAX_DESCRIPTION:
            break
        chosen.append(piece)
        used += cost
    return "\n".join(reversed(chosen))
