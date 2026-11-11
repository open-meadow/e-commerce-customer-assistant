"""Shared helpers for the backend tests (not a test module)."""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.backend import Backend
from app.backend.config import Settings
from app.backend.types import ChoiceSelection, ConfirmationDecision
from app.llm.base import LLMResult, ToolCall
from app.llm.rule_based import RuleBasedLLM


class ScriptedLLM:
    """Rule-based by default; tests override ``chat`` or ``complete``."""

    name = "scripted"

    def __init__(self, chat: Optional[Callable] = None, complete: Optional[Callable] = None,
                 labels: Optional[list] = None):
        self.base = RuleBasedLLM()
        self.chat_fn = chat
        self.complete_fn = complete
        self.labels = list(labels or [])
        self.chat_calls = 0
        self.complete_calls: list[str] = []

    def chat(self, system, messages, tools, *, max_output_tokens):
        self.chat_calls += 1
        if self.chat_fn is not None:
            return self.chat_fn(system, messages, tools)
        return self.base.chat(system, messages, tools, max_output_tokens=max_output_tokens)

    def complete(self, system, prompt, *, max_output_tokens):
        self.complete_calls.append(system.split("\n", 1)[0])
        if "TASK: classify_relation" in system and self.labels:
            label = self.labels.pop(0)
            if isinstance(label, Exception):
                raise label
            return label
        if self.complete_fn is not None:
            return self.complete_fn(system, prompt)
        return self.base.complete(system, prompt, max_output_tokens=max_output_tokens)


class Client:
    """A signed-in test user."""

    def __init__(self, backend: Backend, customer_id: str):
        self.backend = backend
        result = backend.start_session(customer_id)
        assert result.ok, result
        self.state = result.state

    def say(self, text: str):
        return self.backend.handle_message(self.state, text)

    def confirm(self, reply, accepted: bool = True):
        return self.backend.handle_message(
            self.state, decision=ConfirmationDecision(reply.pending.action_id, accepted))

    def pick(self, reply, option_id: str):
        return self.backend.handle_message(
            self.state, choice=ChoiceSelection(reply.choice.prompt_id, option_id))


def calls(*specs: tuple[str, dict[str, Any]]) -> LLMResult:
    return LLMResult(tool_calls=tuple(ToolCall(n, a) for n, a in specs))
