"""Tool result envelope (docs/03 Section 5.2)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

STATUSES = ("ok", "not_found", "rejected", "invalid_input", "needs_choice",
            "needs_confirmation", "unsupported", "unavailable")


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool call.

    ``data`` holds small display-ready fields, never raw repository rows
    and never stored free text written by customers or the LLM. ``note``
    is a short factual hint for the model, never database text.
    """

    status: str
    code: str
    data: dict[str, Any] = field(default_factory=dict)
    note: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(
            {"status": self.status, "code": self.code, "data": self.data, "note": self.note},
            ensure_ascii=False, default=str,
        )
