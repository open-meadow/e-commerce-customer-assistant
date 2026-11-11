"""OpenAI-compatible and Anthropic adapters against a MOCKED transport.

These tests replace ``urllib.request.urlopen``. They check request
construction, response parsing and error handling. They are **not** live
provider verification: no network call is made and no credentials are
used.
"""

from __future__ import annotations

import http.client
import json
import urllib.error

import pytest

from app.backend import Backend, Settings
from app.llm import providers
from app.llm.base import LLMError
from app.llm.providers import AnthropicClient, OpenAICompatibleClient
from backend_helpers import Client

KEY = "sk-test-SECRET-KEY"
TOOLS = [{"name": "get_order_status", "description": "Order status.",
          "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                         "required": ["order_id"], "additionalProperties": False}}]


class FakeResponse:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Transport:
    """Queue of canned responses or exceptions; records every request."""

    def __init__(self, monkeypatch, *items):
        self.items = list(items)
        self.requests = []
        monkeypatch.setattr(providers.urllib.request, "urlopen", self.urlopen)

    def urlopen(self, request, timeout=None):
        self.requests.append((request, timeout))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)

    @property
    def last(self):
        request, timeout = self.requests[-1]
        return {"url": request.full_url, "headers": {k.lower(): v for k, v in request.header_items()},
                "body": json.loads(request.data.decode("utf-8")), "timeout": timeout,
                "method": request.get_method()}


def openai_client():
    return OpenAICompatibleClient(KEY, "test-model", "https://api.test/v1/", 7.5)


def anthropic_client():
    return AnthropicClient(KEY, "test-model", "https://anthropic.test/", 7.5)


def openai_text(text):
    return {"choices": [{"message": {"content": text}}]}


def openai_call(name, arguments, call_id="c1"):
    return {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": call_id, "function": {"name": name, "arguments": arguments}}]}}]}


def anthropic_blocks(*blocks):
    return {"content": list(blocks)}


HISTORY = [
    {"role": "user", "content": "where is ORD1001"},
    {"role": "assistant", "tool_calls": [{"name": "get_order_status", "arguments": {"order_id": "ORD1001"}}]},
    {"role": "tool", "name": "get_order_status", "content": '{"status":"ok"}'},
]


class TestOpenAIRequest:
    def test_request_shape(self, monkeypatch):
        t = Transport(monkeypatch, openai_text("hi"))
        openai_client().chat("SYSTEM TEXT", HISTORY, TOOLS, max_output_tokens=111)
        sent = t.last
        assert sent["url"] == "https://api.test/v1/chat/completions" and sent["method"] == "POST"
        assert sent["headers"]["authorization"] == f"Bearer {KEY}"
        assert sent["headers"]["content-type"] == "application/json"
        assert sent["timeout"] == 7.5
        body = sent["body"]
        assert body["model"] == "test-model" and body["max_tokens"] == 111
        assert body["messages"][0] == {"role": "system", "content": "SYSTEM TEXT"}
        roles = [m["role"] for m in body["messages"][1:]]
        assert roles == ["user", "assistant", "user"][: len(roles)] and all(
            a != b for a, b in zip(roles, roles[1:]))
        assert body["tools"][0]["type"] == "function"
        assert body["tools"][0]["function"]["name"] == "get_order_status"
        assert body["tools"][0]["function"]["parameters"] == TOOLS[0]["parameters"]

    def test_no_tools_key_when_no_tools(self, monkeypatch):
        t = Transport(monkeypatch, openai_text("hi"))
        openai_client().complete("S", "P", max_output_tokens=5)
        assert "tools" not in t.last["body"]
        assert t.last["body"]["messages"][-1] == {"role": "user", "content": "P"}

    def test_tool_turns_are_flattened_to_text(self, monkeypatch):
        t = Transport(monkeypatch, openai_text("hi"))
        openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=5)
        text = json.dumps(t.last["body"]["messages"])
        assert "tool result: get_order_status" in text and "tool call" in text


class TestOpenAIResponse:
    def test_text(self, monkeypatch):
        Transport(monkeypatch, openai_text("Hello there"))
        result = openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=5)
        assert result.text == "Hello there" and result.tool_calls == ()

    def test_tool_call(self, monkeypatch):
        Transport(monkeypatch, openai_call("get_order_status", '{"order_id": "ORD1001"}'))
        result = openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=5)
        call = result.tool_calls[0]
        assert (call.name, call.arguments, call.call_id) == ("get_order_status", {"order_id": "ORD1001"}, "c1")

    def test_empty_arguments_become_empty_object(self, monkeypatch):
        Transport(monkeypatch, openai_call("list_my_orders", ""))
        assert openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=5).tool_calls[0].arguments == {}

    def test_complete_returns_text(self, monkeypatch):
        Transport(monkeypatch, openai_text("same_issue"))
        assert openai_client().complete("S", "P", max_output_tokens=5) == "same_issue"

    @pytest.mark.parametrize("body", [
        {}, {"choices": []}, {"choices": [{}]}, {"choices": [None]}, {"choices": "x"},
        {"choices": [{"message": "text"}]}, openai_call("t", "{not json"), openai_call("t", "[1, 2]"),
        {"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]},
    ])
    def test_malformed_responses_raise_llm_error(self, monkeypatch, body):
        Transport(monkeypatch, body)
        with pytest.raises(LLMError):
            openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=5)

    def test_complete_without_text_raises(self, monkeypatch):
        Transport(monkeypatch, {"choices": [{"message": {"content": None}}]})
        with pytest.raises(LLMError):
            openai_client().complete("S", "P", max_output_tokens=5)


class TestAnthropicRequestAndResponse:
    def test_request_shape(self, monkeypatch):
        t = Transport(monkeypatch, anthropic_blocks({"type": "text", "text": "hi"}))
        anthropic_client().chat("SYSTEM TEXT", HISTORY, TOOLS, max_output_tokens=222)
        sent = t.last
        assert sent["url"] == "https://anthropic.test/v1/messages"
        assert sent["headers"]["x-api-key"] == KEY and sent["headers"]["anthropic-version"] == "2023-06-01"
        body = sent["body"]
        assert body["system"] == "SYSTEM TEXT" and body["max_tokens"] == 222 and body["model"] == "test-model"
        assert body["messages"][0]["role"] == "user"
        assert body["tools"] == [{"name": "get_order_status", "description": "Order status.",
                                  "input_schema": TOOLS[0]["parameters"]}]

    def test_text_blocks_are_joined(self, monkeypatch):
        Transport(monkeypatch, anthropic_blocks({"type": "text", "text": "a"}, {"type": "text", "text": "b"}))
        assert anthropic_client().chat("S", HISTORY, [], max_output_tokens=5).text == "ab"

    def test_tool_use(self, monkeypatch):
        Transport(monkeypatch, anthropic_blocks(
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "tu1", "name": "get_order_status", "input": {"order_id": "ORD1001"}}))
        result = anthropic_client().chat("S", HISTORY, TOOLS, max_output_tokens=5)
        assert result.text == "Let me check."
        assert (result.tool_calls[0].name, result.tool_calls[0].arguments) == (
            "get_order_status", {"order_id": "ORD1001"})

    def test_complete(self, monkeypatch):
        Transport(monkeypatch, anthropic_blocks({"type": "text", "text": "new_issue"}))
        assert anthropic_client().complete("S", "P", max_output_tokens=5) == "new_issue"

    @pytest.mark.parametrize("body", [
        {}, {"content": None}, {"content": "text"}, {"content": ["text"]}, {"content": [None]},
        {"content": [{"type": "tool_use", "name": "t", "input": "not an object"}]},
        {"content": [{"type": "tool_use", "input": {}}]},
        {"content": [{"type": "text"}]},
    ])
    def test_malformed_responses_raise_llm_error(self, monkeypatch, body):
        Transport(monkeypatch, body)
        with pytest.raises(LLMError):
            anthropic_client().chat("S", HISTORY, TOOLS, max_output_tokens=5)

    def test_complete_without_text_raises(self, monkeypatch):
        Transport(monkeypatch, anthropic_blocks({"type": "tool_use", "name": "t", "input": {}}))
        with pytest.raises(LLMError):
            anthropic_client().complete("S", "P", max_output_tokens=5)


FAILURES = [
    urllib.error.HTTPError("https://x", 500, "server error", {}, None),
    urllib.error.HTTPError("https://x", 429, "rate limited", {}, None),
    urllib.error.URLError("no route"),
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
    http.client.IncompleteRead(b"par"),
    b"\xff\xfe not utf-8",
    b"<html>not json</html>",
    b"[1, 2, 3]",
    b"",
]


@pytest.mark.parametrize("make", [openai_client, anthropic_client], ids=["openai", "anthropic"])
class TestTransportFailures:
    @pytest.mark.parametrize("failure", FAILURES, ids=lambda f: type(f).__name__ if not isinstance(f, bytes) else "body")
    def test_failures_raise_llm_error_once_without_retry(self, monkeypatch, make, failure):
        t = Transport(monkeypatch, failure)
        with pytest.raises(LLMError) as caught:
            make().chat("S", HISTORY, TOOLS, max_output_tokens=5)
        assert len(t.requests) == 1  # never retried
        assert KEY not in str(caught.value)

    def test_error_text_does_not_contain_the_key(self, monkeypatch, make):
        Transport(monkeypatch, urllib.error.URLError(f"failed with {KEY}"))
        with pytest.raises(LLMError) as caught:
            make().complete("S", "P", max_output_tokens=5)
        assert KEY not in str(caught.value)  # the message is generic; the key is not echoed


class TestAdapterWithTheBackend:
    """The real adapter classes driving the real backend over the mocked transport."""

    def test_tool_call_then_answer(self, temp_db, monkeypatch):
        t = Transport(monkeypatch,
                      openai_call("get_order_status", '{"order_id": "ORD1001"}'),
                      openai_text("Your order ORD1001 was delivered."))
        reply = Client(Backend(llm=openai_client(), settings=Settings()), "C101").say("where is ORD1001?")
        assert reply.status == "ok" and reply.text == "Your order ORD1001 was delivered."
        assert len(t.requests) == 2
        # The tool result sent back to the model came from the database, scoped to C101.
        second = json.dumps(t.requests[1][0].data.decode("utf-8"))
        assert "Delivered" in second and "C101" not in second and KEY not in second

    def test_cross_customer_tool_call_gets_not_found(self, temp_db, monkeypatch):
        t = Transport(monkeypatch,
                      anthropic_blocks({"type": "tool_use", "id": "1", "name": "get_order_status",
                                        "input": {"order_id": "ORD1003"}}),
                      anthropic_blocks({"type": "text", "text": "I couldn't find that order."}))
        Client(Backend(llm=anthropic_client(), settings=Settings()), "C101").say("status of ORD1003")
        second = t.requests[1][0].data.decode("utf-8")
        assert "order_not_found" in second and "P1002" not in second

    def test_provider_failure_becomes_an_unavailable_reply(self, temp_db, monkeypatch):
        Transport(monkeypatch, urllib.error.URLError("down"))
        reply = Client(Backend(llm=openai_client(), settings=Settings()), "C101").say("where is ORD1001?")
        assert reply.status == "error" and reply.error_code == "unavailable"
        assert "down" not in reply.text and KEY not in reply.text

    def test_model_asking_for_an_unknown_tool_is_refused(self, temp_db, monkeypatch):
        t = Transport(monkeypatch, openai_call("delete_everything", "{}"), openai_text("Sorry, I can't do that."))
        reply = Client(Backend(llm=openai_client(), settings=Settings()), "C101").say("do it")
        assert reply.text == "Sorry, I can't do that."
        assert "unknown_tool" in t.requests[1][0].data.decode("utf-8")


class TestReadiness:
    """Configuration checks: no network, and no secret value is ever reported."""

    SECRET = "sk-ant-SUPER-SECRET-VALUE"

    def test_default_is_the_offline_stand_in(self):
        from app.llm.providers import readiness
        assert readiness({}) == {"provider": "rule_based", "configured": True, "missing": [], "live": False}

    def test_anthropic_fully_configured(self):
        from app.llm.providers import readiness
        env = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": self.SECRET, "LLM_MODEL": "some-model"}
        result = readiness(env)
        assert result == {"provider": "anthropic", "configured": True, "missing": [], "live": True}
        assert self.SECRET not in repr(result)

    @pytest.mark.parametrize("env,missing", [
        ({"LLM_PROVIDER": "anthropic"}, ["ANTHROPIC_API_KEY", "LLM_MODEL"]),
        ({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": SECRET}, ["LLM_MODEL"]),
        ({"LLM_PROVIDER": "anthropic", "LLM_MODEL": "m"}, ["ANTHROPIC_API_KEY"]),
        ({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": SECRET}, ["LLM_MODEL"]),
        ({"LLM_PROVIDER": "openai"}, ["OPENAI_API_KEY", "LLM_MODEL"]),
    ])
    def test_missing_variables_are_named_but_never_valued(self, env, missing):
        from app.llm.providers import readiness
        result = readiness(env)
        assert result["configured"] is False and result["missing"] == missing and result["live"] is False
        assert self.SECRET not in repr(result)

    def test_unknown_provider_and_bad_timeout(self):
        from app.llm.providers import readiness
        assert readiness({"LLM_PROVIDER": "nope"})["configured"] is False
        bad = readiness({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "LLM_MODEL": "m",
                         "LLM_TIMEOUT_SECONDS": "soon"})
        assert bad["configured"] is False and "LLM_TIMEOUT_SECONDS (not a number)" in bad["missing"]

    def test_readiness_agrees_with_build_llm_client(self):
        from app.llm.base import LLMConfigError
        from app.llm.providers import build_llm_client, readiness
        for env in ({"LLM_PROVIDER": "anthropic"}, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k"},
                    {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "LLM_MODEL": "m"}, {}):
            if readiness(env)["configured"]:
                build_llm_client(env)
            else:
                with pytest.raises(LLMConfigError):
                    build_llm_client(env)

    def test_readiness_makes_no_network_call(self, monkeypatch):
        from app.llm.providers import readiness
        t = Transport(monkeypatch)
        readiness({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "LLM_MODEL": "m"})
        assert t.requests == []

    def test_a_misconfigured_provider_fails_closed_and_never_becomes_the_stand_in(self, temp_db):
        from app.llm.base import LLMConfigError
        with pytest.raises(LLMConfigError) as caught:
            Backend(env={"LLM_PROVIDER": "anthropic"})
        assert "ANTHROPIC_API_KEY" in str(caught.value)

    def test_the_stand_in_stays_available_for_offline_use(self, temp_db):
        backend = Backend(env={})
        assert backend.check_backend().llm_mode == "rule_based"
        assert Client(backend, "C101").say("where is ORD1001?").status == "ok"


class TestPublicConfigError:
    def test_the_frontend_can_catch_the_config_error_from_app_backend(self):
        import app.backend as public
        from app.llm.base import LLMConfigError
        assert public.LLMConfigError is LLMConfigError and "LLMConfigError" in public.__all__

    def test_the_module_level_api_raises_it_on_first_use_and_recovers_after_the_fix(self, temp_db, monkeypatch):
        import app.backend as public
        public.set_backend(None)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        try:
            with pytest.raises(public.LLMConfigError) as caught:
                public.check_backend()
            message = str(caught.value)
            assert "ANTHROPIC_API_KEY" in message and "LLM_MODEL" in message
            with pytest.raises(public.LLMConfigError):  # still failing closed on the next call
                public.list_login_options()
            monkeypatch.setenv("LLM_PROVIDER", "rule_based")  # operator fixes the configuration
            assert public.check_backend().status == "ready"
            assert len(public.list_login_options()) == 6
        finally:
            public.set_backend(None)
