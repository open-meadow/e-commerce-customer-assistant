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
    """The provider failed, timed out or returned unusable output.

    The message is short, generic and safe to log: it never contains a key,
    a request or response body, or customer text. ``category`` and ``status``
    say what kind of failure it was so callers and logs can tell them apart:

    ``auth`` (HTTP 401/403), ``bad_request`` (400/422), ``not_found`` (404),
    ``rate_limited`` (429), ``server_error`` (5xx), ``http_error`` (other
    HTTP statuses), ``timeout``, ``dns``, ``network``, ``invalid_json``,
    ``invalid_response``, ``incomplete`` and ``provider_error``. ``status``
    is the HTTP status code when there was one. ``error`` is the default.
    """

    def __init__(self, message: str = "", *, category: str = "error",
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.category = category
        self.status = status


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
    """Either final text, tool calls, or both.

    ``provider_items`` is opaque provider state that must be handed back
    unchanged and in order with the tool results (for the OpenAI Responses API:
    the complete ``output`` array of a response that asked for tools). The
    orchestrator copies it into the assistant tool-call message and never
    inspects, shows, logs or stores it. Providers that need nothing leave it
    empty.
    """

    text: Optional[str] = None
    tool_calls: tuple[ToolCall, ...] = ()
    provider_items: tuple[dict[str, Any], ...] = field(default=(), repr=False, compare=False)


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
        """One tool-capable turn. ``messages`` use roles user, assistant, tool.

        An assistant message that proposed tools has ``tool_calls`` (each with
        ``name``, ``arguments`` and ``call_id``) and, when the provider returned
        any, ``provider_items``. Each tool message carries the ``call_id`` of
        the call it answers.
        """

    def complete(self, system: str, prompt: str, *, max_output_tokens: int) -> str:
        """A tool-free completion (classification and summarization)."""
