"""Optional HTTP provider adapters (standard library only).

**Not verified against a live API**: this project has no provider
credentials. The adapters are exercised only with a mocked transport in
the unit tests (``tests/test_providers.py``); no live request has ever
been made. They translate to and from each provider's documented
JSON shape, send no more than the conversation text, never log keys, and
never retry. Tool exchanges are flattened to plain text turns so the
same code works across providers.

Selection is by environment: ``LLM_PROVIDER`` (``rule_based`` default,
``openai`` or ``anthropic``), ``LLM_MODEL`` (required for a real
provider; no default model is assumed), the provider key variable,
optional ``*_BASE_URL`` and ``LLM_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

from app.llm.base import LLMClient, LLMConfigError, LLMError, LLMResult, ToolCall
from app.llm.rule_based import RuleBasedLLM

_DEFAULT_TIMEOUT = 20.0


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any],
               timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise LLMError("the LLM provider request failed") from exc
    except UnicodeDecodeError as exc:
        raise LLMError("the LLM provider returned undecodable text") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise LLMError("the LLM provider returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise LLMError("the LLM provider returned an unexpected shape")
    return data


def _flatten(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Turn role/tool messages into alternating plain user/assistant text."""
    flat: list[dict[str, str]] = []
    for message in messages:
        role = message["role"]
        content = str(message.get("content", ""))
        if role == "tool":
            role, content = "user", f"[tool result: {message.get('name', '')}]\n{content}"
        elif role == "assistant" and message.get("tool_calls"):
            content = f"[tool call: {json.dumps(message['tool_calls'])}]"
        if flat and flat[-1]["role"] == role:
            flat[-1]["content"] += "\n\n" + content
        else:
            flat.append({"role": role, "content": content})
    if not flat or flat[0]["role"] != "user":
        flat.insert(0, {"role": "user", "content": "(conversation start)"})
    return flat


class OpenAICompatibleClient:
    """Chat Completions style endpoint (``/chat/completions``)."""

    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float) -> None:
        self._key, self._model, self._base, self._timeout = api_key, model, base_url.rstrip("/"), timeout

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _post_json(f"{self._base}/chat/completions",
                          {"Authorization": f"Bearer {self._key}"}, payload, self._timeout)

    def chat(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             *, max_output_tokens: int) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self._model, "max_tokens": max_output_tokens,
            "messages": [{"role": "system", "content": system}, *_flatten(messages)],
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["parameters"]}} for t in tools]
        data = self._call(payload)
        try:
            message = data["choices"][0]["message"]
            calls = []
            for call in message.get("tool_calls") or []:
                arguments = json.loads(call["function"].get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments not an object")
                calls.append(ToolCall(call["function"]["name"], arguments, str(call.get("id", ""))))
            return LLMResult(text=message.get("content"), tool_calls=tuple(calls))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise LLMError("unusable LLM provider response") from exc

    def complete(self, system: str, prompt: str, *, max_output_tokens: int) -> str:
        result = self.chat(system, [{"role": "user", "content": prompt}], [],
                           max_output_tokens=max_output_tokens)
        if not isinstance(result.text, str):
            raise LLMError("unusable LLM provider response")
        return result.text


class AnthropicClient:
    """Messages API (``/v1/messages``)."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float) -> None:
        self._key, self._model, self._base, self._timeout = api_key, model, base_url.rstrip("/"), timeout

    def chat(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             *, max_output_tokens: int) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self._model, "max_tokens": max_output_tokens,
            "system": system, "messages": _flatten(messages),
        }
        if tools:
            payload["tools"] = [{"name": t["name"], "description": t["description"],
                                 "input_schema": t["parameters"]} for t in tools]
        data = _post_json(f"{self._base}/v1/messages",
                          {"x-api-key": self._key, "anthropic-version": "2023-06-01"},
                          payload, self._timeout)
        try:
            texts, calls = [], []
            for block in data["content"]:
                if block.get("type") == "text":
                    texts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    if not isinstance(block["input"], dict):
                        raise ValueError("input not an object")
                    calls.append(ToolCall(block["name"], block["input"], str(block.get("id", ""))))
            return LLMResult(text="".join(texts) or None, tool_calls=tuple(calls))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise LLMError("unusable LLM provider response") from exc

    def complete(self, system: str, prompt: str, *, max_output_tokens: int) -> str:
        result = self.chat(system, [{"role": "user", "content": prompt}], [],
                           max_output_tokens=max_output_tokens)
        if not isinstance(result.text, str):
            raise LLMError("unusable LLM provider response")
        return result.text


def build_llm_client(env: Optional[Mapping[str, str]] = None) -> LLMClient:
    """Build the configured client. Defaults to the offline rule-based client.

    Raises:
        LLMConfigError: a real provider was requested without its key or
            ``LLM_MODEL``. The caller decides the fallback and must not
            present the fallback as the real provider.
    """
    env = os.environ if env is None else env
    provider = (env.get("LLM_PROVIDER") or "rule_based").strip().lower()
    if provider in ("rule_based", "offline", "fake", ""):
        return RuleBasedLLM()
    try:
        timeout = float(env.get("LLM_TIMEOUT_SECONDS") or _DEFAULT_TIMEOUT)
    except ValueError as exc:
        raise LLMConfigError("LLM_TIMEOUT_SECONDS is not a number") from exc
    model = env.get("LLM_MODEL")
    if provider == "openai":
        key = env.get("OPENAI_API_KEY")
        if not key or not model:
            raise LLMConfigError("OPENAI_API_KEY and LLM_MODEL are required for the openai provider")
        return OpenAICompatibleClient(key, model, env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1", timeout)
    if provider == "anthropic":
        key = env.get("ANTHROPIC_API_KEY")
        if not key or not model:
            raise LLMConfigError("ANTHROPIC_API_KEY and LLM_MODEL are required for the anthropic provider")
        return AnthropicClient(key, model, env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com", timeout)
    raise LLMConfigError("unknown LLM_PROVIDER")


_PROVIDER_VARIABLES = {
    "openai": ("OPENAI_API_KEY", "LLM_MODEL"),
    "anthropic": ("ANTHROPIC_API_KEY", "LLM_MODEL"),
}


def readiness(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Report whether the configured provider can be built, without any network call.

    Returns the provider name, whether it is configured, and the *names* of
    missing variables. No value (and so no key) is ever included.
    """
    env = os.environ if env is None else env
    provider = (env.get("LLM_PROVIDER") or "rule_based").strip().lower()
    if provider in ("rule_based", "offline", "fake", ""):
        return {"provider": "rule_based", "configured": True, "missing": [], "live": False}
    required = _PROVIDER_VARIABLES.get(provider)
    if required is None:
        return {"provider": provider, "configured": False, "missing": ["LLM_PROVIDER (unknown provider)"],
                "live": False}
    missing = [name for name in required if not env.get(name)]
    try:
        float(env.get("LLM_TIMEOUT_SECONDS") or _DEFAULT_TIMEOUT)
    except ValueError:
        missing.append("LLM_TIMEOUT_SECONDS (not a number)")
    return {"provider": provider, "configured": not missing, "missing": missing, "live": not missing}
