"""Optional HTTP provider adapters (standard library only).

**Not verified against a live API**: this project has no provider
credentials. The adapters are exercised only with a mocked transport in
the unit tests (``tests/test_providers.py``); no live request has ever
been made. They translate to and from each provider's documented
JSON shape, send no more than the conversation text, never log keys or
conversation text, and never retry.

* ``openai`` uses the OpenAI **Responses API** (``POST {base}/responses``).
  Conversation state stays in this application (``store: false``, no
  ``previous_response_id``). Tool exchanges are real ``function_call`` and
  ``function_call_output`` items joined by ``call_id``; they are not turned
  into plain text.
* ``anthropic`` uses the Messages API. Its tool exchanges are still
  flattened to plain text turns.

Selection is by environment: ``LLM_PROVIDER`` (``rule_based`` default,
``openai`` or ``anthropic``), ``LLM_MODEL`` (required for a real
provider; no default model is assumed), the provider key variable,
optional ``*_BASE_URL`` and ``LLM_TIMEOUT_SECONDS``.

Failures raise ``LLMError`` with a ``category`` (and an HTTP ``status`` when
there was one). One warning with only safe metadata is logged per failure:
provider, model id, category, status and, for HTTP errors, the short
``type``/``code``/``param`` identifiers of an OpenAI-style error body. Keys,
authorization headers, bodies, conversation text and tool arguments are never
logged.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

from app.llm.base import LLMClient, LLMConfigError, LLMError, LLMResult, ToolCall
from app.llm.rule_based import RuleBasedLLM

log = logging.getLogger("app.llm")

_DEFAULT_TIMEOUT = 20.0
# The Responses API documents a lower bound on ``max_output_tokens``; smaller
# budgets are raised to it instead of being sent and rejected.
_MIN_OUTPUT_TOKENS = 16
_ERROR_BODY_LIMIT = 65536
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


# ---------------------------------------------------------------------------
# Transport and failure reporting
# ---------------------------------------------------------------------------

def _identifier(value: Any) -> Optional[str]:
    """``value`` if it is a short, plain identifier (safe to log), else ``None``."""
    return value if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) else None


def _http_category(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status in (400, 422):
        return "bad_request"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "http_error"


def _network_category(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns"
    return "network"


def _error_identifiers(exc: urllib.error.HTTPError) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(type, code, param)`` of an OpenAI-style error body, each only if it is a short identifier.

    The error ``message`` is never read out: it can echo request content.
    """
    try:
        body = json.loads(exc.read(_ERROR_BODY_LIMIT).decode("utf-8"))
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            return None, None, None
        return _identifier(error.get("type")), _identifier(error.get("code")), _identifier(error.get("param"))
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return None, None, None


def _fail(provider: str, model: str, category: str, *, status: Optional[int] = None,
          identifiers: tuple[Optional[str], Optional[str], Optional[str]] = (None, None, None)) -> LLMError:
    """Log one safe diagnostic line and return the typed error to raise."""
    error_type, error_code, error_param = identifiers
    log.warning(
        "LLM provider call failed: provider=%s model=%s category=%s status=%s "
        "error_type=%s error_code=%s error_param=%s",
        provider, _identifier(model) or "?", category, status if status is not None else "-",
        error_type or "-", error_code or "-", error_param or "-")
    text = f"the LLM provider request failed ({category}" + (f", HTTP {status}" if status is not None else "") + ")"
    return LLMError(text, category=category, status=status)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float,
               *, provider: str, model: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # before URLError: HTTPError is a subclass
        raise _fail(provider, model, _http_category(exc.code), status=exc.code,
                    identifiers=_error_identifiers(exc)) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, BaseException) else exc
        raise _fail(provider, model, _network_category(reason)) from exc
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        raise _fail(provider, model, _network_category(exc)) from exc
    except UnicodeDecodeError as exc:
        raise _fail(provider, model, "invalid_response") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise _fail(provider, model, "invalid_json") from exc
    if not isinstance(data, dict):
        raise _fail(provider, model, "invalid_response")
    return data


# ---------------------------------------------------------------------------
# OpenAI Responses API
# ---------------------------------------------------------------------------

def _responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """One function tool in the Responses API shape (flat, not nested under ``function``).

    ``strict`` is always set explicitly, because the Responses API treats a missing
    value as strict. It is true only when the existing schema already satisfies the
    strict-schema rules (object, ``additionalProperties: false``, every property
    required, simple property schemas). The schema itself is passed through unchanged.
    """
    parameters = tool["parameters"]
    properties = parameters.get("properties")
    strict = (
        parameters.get("type") == "object"
        and parameters.get("additionalProperties") is False
        and isinstance(properties, dict)
        and set(parameters.get("required") or ()) == set(properties)
        and all(isinstance(p, dict) and set(p) <= {"type", "description", "enum"} for p in properties.values())
    )
    return {"type": "function", "name": tool["name"], "description": tool["description"],
            "parameters": parameters, "strict": strict}


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map the orchestrator's messages to Responses API input items.

    * user / assistant text -> role messages;
    * an assistant tool-call message -> the provider items it carried, replayed verbatim and in
      order (the complete output array of the response that proposed the calls), or
      ``function_call`` items rebuilt from its calls when none were carried;
    * a tool message -> a ``function_call_output`` with the same ``call_id``.
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "tool":
            call_id = message.get("call_id")
            if not call_id:
                raise LLMError("a tool result has no call_id", category="invalid_request")
            items.append({"type": "function_call_output", "call_id": call_id,
                          "output": str(message.get("content", ""))})
        elif role == "assistant" and message.get("tool_calls"):
            carried = message.get("provider_items")
            if carried:
                items.extend(carried)
                continue
            for call in message["tool_calls"]:
                if not call.get("call_id"):
                    raise LLMError("a tool call has no call_id", category="invalid_request")
                items.append({"type": "function_call", "call_id": call["call_id"], "name": call["name"],
                              "arguments": json.dumps(call.get("arguments") or {})})
        else:
            items.append({"role": role, "content": str(message.get("content", ""))})
    return items


class OpenAIResponsesClient:
    """OpenAI Responses API (``POST {base}/responses``), stateless (``store: false``)."""

    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float) -> None:
        self._key, self._model, self._base, self._timeout = api_key, model, base_url.rstrip("/"), timeout

    def _unusable(self) -> LLMError:
        return _fail(self.name, self._model, "invalid_response")

    def chat(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             *, max_output_tokens: int) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "instructions": system,
            "input": _responses_input(messages),
            "max_output_tokens": max(_MIN_OUTPUT_TOKENS, int(max_output_tokens)),
            "store": False,
        }
        if tools:
            payload["tools"] = [_responses_tool(t) for t in tools]
        data = _post_json(f"{self._base}/responses", {"Authorization": f"Bearer {self._key}"},
                          payload, self._timeout, provider=self.name, model=self._model)
        return self._parse(data)

    def _parse(self, data: dict[str, Any]) -> LLMResult:
        if data.get("error"):
            raise _fail(self.name, self._model, "provider_error")
        status = data.get("status")
        if status in ("failed", "cancelled"):
            raise _fail(self.name, self._model, "provider_error")
        if status == "incomplete":
            # Checked before any output is looked at. A response that was cut off (for example by the
            # token budget) is never a completed answer, even when it holds partial text or a call
            # whose arguments look complete: nothing in it may be shown or executed.
            raise _fail(self.name, self._model, "incomplete")
        try:
            output = data["output"]
            if not isinstance(output, list):
                raise ValueError("output is not a list")
            texts: list[str] = []
            calls: list[ToolCall] = []
            for item in output:
                kind = item.get("type")
                if kind in ("message", "function_call") and item.get("status") == "incomplete":
                    # An unfinished item is rejected even if the outer status says completed.
                    raise _fail(self.name, self._model, "incomplete")
                if kind == "message":
                    for part in item.get("content") or []:
                        if part.get("type") == "output_text":
                            texts.append(str(part["text"]))
                        elif part.get("type") == "refusal":
                            texts.append(str(part["refusal"]))
                elif kind == "function_call":
                    arguments = json.loads(item.get("arguments") or "{}")
                    name, call_id = item["name"], item["call_id"]
                    if not isinstance(arguments, dict) or not isinstance(name, str) or not name \
                            or not isinstance(call_id, str) or not call_id:
                        raise ValueError("unusable function_call")
                    calls.append(ToolCall(name, arguments, call_id))
                # Reasoning and every other item type are not interpreted here.
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise self._unusable() from exc
        # With calls, the complete output array (reasoning, narration, calls, any other item) is kept
        # verbatim and in order as opaque state for the next request. It is never shown or logged.
        return LLMResult(text="".join(texts) or None, tool_calls=tuple(calls),
                         provider_items=tuple(output) if calls else ())

    def complete(self, system: str, prompt: str, *, max_output_tokens: int) -> str:
        result = self.chat(system, [{"role": "user", "content": prompt}], [],
                           max_output_tokens=max_output_tokens)
        if not isinstance(result.text, str):
            raise self._unusable()
        return result.text


# ---------------------------------------------------------------------------
# Anthropic Messages API (tool exchanges flattened to text)
# ---------------------------------------------------------------------------

def _flatten(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Turn role/tool messages into alternating plain user/assistant text."""
    flat: list[dict[str, str]] = []
    for message in messages:
        role = message["role"]
        content = str(message.get("content", ""))
        if role == "tool":
            role, content = "user", f"[tool result: {message.get('name', '')}]\n{content}"
        elif role == "assistant" and message.get("tool_calls"):
            plain = [{"name": c.get("name"), "arguments": c.get("arguments")} for c in message["tool_calls"]]
            content = f"[tool call: {json.dumps(plain)}]"
        if flat and flat[-1]["role"] == role:
            flat[-1]["content"] += "\n\n" + content
        else:
            flat.append({"role": role, "content": content})
    if not flat or flat[0]["role"] != "user":
        flat.insert(0, {"role": "user", "content": "(conversation start)"})
    return flat


class AnthropicClient:
    """Messages API (``/v1/messages``)."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float) -> None:
        self._key, self._model, self._base, self._timeout = api_key, model, base_url.rstrip("/"), timeout

    def _unusable(self) -> LLMError:
        return _fail(self.name, self._model, "invalid_response")

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
                          payload, self._timeout, provider=self.name, model=self._model)
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
            raise self._unusable() from exc

    def complete(self, system: str, prompt: str, *, max_output_tokens: int) -> str:
        result = self.chat(system, [{"role": "user", "content": prompt}], [],
                           max_output_tokens=max_output_tokens)
        if not isinstance(result.text, str):
            raise self._unusable()
        return result.text


# ---------------------------------------------------------------------------
# Selection and readiness (no network access)
# ---------------------------------------------------------------------------

def build_llm_client(env: Optional[Mapping[str, str]] = None) -> LLMClient:
    """Build the configured client. Defaults to the offline rule-based client.

    Construction never makes a network request.

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
        return OpenAIResponsesClient(key, model, env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1", timeout)
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
