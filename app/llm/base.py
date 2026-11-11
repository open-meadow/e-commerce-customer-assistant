"""Provider-agnostic LLM interface.

The LLM is **not** an authority for identity, authorization, eligibility
or state. It proposes text, tool calls and labels. The orchestrator
validates everything it returns. No client here retries automatically:
a failure raises ``LLMError`` and the caller applies the approved
fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


class LLMError(Exception):
    """The provider failed, timed out or returned unusable output."""


class LLMConfigError(LLMError):
    """The requested provider is not configured (for example no API key)."""


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation proposed by the model (arguments are untrusted)."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True)
class LLMResult:
    """Either final text, tool calls, or both."""

    text: Optional[str] = None
    tool_calls: tuple[ToolCall, ...] = ()


class LLMClient(Protocol):
    """What the orchestrator needs from any model provider."""

    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_output_tokens: int,
    ) -> LLMResult:
        """One tool-capable turn. ``messages`` use roles user, assistant, tool."""

    def complete(self, system: str, prompt: str, *, max_output_tokens: int) -> str:
        """A tool-free completion (classification and summarization)."""
