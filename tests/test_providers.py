"""OpenAI Responses API and Anthropic adapters against a MOCKED transport.

These tests replace ``urllib.request.urlopen``. They check request
construction, response parsing, the tool-call lifecycle, error handling and
logging. They are **not** live provider verification: no network call is made
and no credentials are used.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import socket
import urllib.error

import pytest

from app.backend import Backend, Settings
from app.llm import providers
from app.llm.base import LLMConfigError, LLMError, LLMResult, ToolCall
from app.llm.providers import AnthropicClient, OpenAIResponsesClient, build_llm_client, readiness
from app.llm.rule_based import RuleBasedLLM
from app.tools.registry import llm_tool_definitions
from backend_helpers import Client, ScriptedLLM

KEY = "sk-test-SECRET-KEY"
CUSTOMER_TEXT = "CUSTOMER-PRIVATE-TEXT-4711"
HIDDEN_REASONING = "HIDDEN-REASONING-TEXT"
ENCRYPTED = "ENCRYPTED-REASONING-BLOB"
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
        return self.sent(-1)

    def sent(self, index):
        request, timeout = self.requests[index]
        return {"url": request.full_url, "headers": {k.lower(): v for k, v in request.header_items()},
                "body": json.loads(request.data.decode("utf-8")), "timeout": timeout,
                "method": request.get_method()}


def openai_client():
    return OpenAIResponsesClient(KEY, "test-model", "https://api.test/v1/", 7.5)


def anthropic_client():
    return AnthropicClient(KEY, "test-model", "https://anthropic.test/", 7.5)


# -- Responses API response builders -----------------------------------------

def message_item(text):
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def function_call(name, arguments, call_id="call_1"):
    return {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name,
            "arguments": arguments, "status": "completed"}


def reasoning(item_id="rs_1"):
    return {"type": "reasoning", "id": item_id, "encrypted_content": ENCRYPTED,
            "summary": [{"type": "summary_text", "text": HIDDEN_REASONING}]}


def response(*items, status="completed"):
    return {"status": status, "output": list(items)}


def responses_text(text):
    return response(message_item(text))


def responses_call(name, arguments, call_id="call_1"):
    return response(function_call(name, arguments, call_id))


def anthropic_blocks(*blocks):
    return {"content": list(blocks)}


def http_error(status, body=None):
    fp = io.BytesIO(json.dumps(body).encode("utf-8")) if body is not None else None
    return urllib.error.HTTPError("https://api.test/v1/responses", status, "err", {}, fp)


HISTORY = [
    {"role": "user", "content": "where is ORD1001"},
    {"role": "assistant", "tool_calls": [{"name": "get_order_status", "arguments": {"order_id": "ORD1001"},
                                          "call_id": "call_1"}]},
    {"role": "tool", "name": "get_order_status", "call_id": "call_1", "content": '{"status":"ok"}'},
]


# ===========================================================================
# OpenAI Responses API: request
# ===========================================================================

class TestOpenAIRequest:
    def test_endpoint_method_headers_and_timeout(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("hi"))
        openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        sent = t.last
        assert sent["url"] == "https://api.test/v1/responses" and sent["method"] == "POST"
        assert sent["headers"]["authorization"] == f"Bearer {KEY}"
        assert sent["headers"]["content-type"] == "application/json"
        assert sent["timeout"] == 7.5

    def test_request_is_in_the_responses_format(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("hi"))
        openai_client().chat("SYSTEM TEXT", HISTORY, TOOLS, max_output_tokens=111)
        body = t.last["body"]
        assert set(body) == {"model", "instructions", "input", "max_output_tokens", "store", "tools"}
        assert body["model"] == "test-model" and body["instructions"] == "SYSTEM TEXT"
        # Chat Completions leftovers and server-side conversation state are absent.
        for absent in ("messages", "max_tokens", "previous_response_id", "conversation"):
            assert absent not in body

    def test_max_output_tokens_is_sent_and_raised_to_the_minimum(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("a"), responses_text("b"))
        openai_client().chat("S", HISTORY, [], max_output_tokens=512)
        assert t.sent(0)["body"]["max_output_tokens"] == 512
        openai_client().chat("S", HISTORY, [], max_output_tokens=5)
        assert t.sent(1)["body"]["max_output_tokens"] == providers._MIN_OUTPUT_TOKENS == 16

    def test_nothing_is_stored_server_side(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("hi"))
        openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert t.last["body"]["store"] is False

    def test_system_prompt_is_instructions_and_text_turns_map_to_roles(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("hi"))
        openai_client().chat("THE SYSTEM PROMPT", [
            {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "where is my order?"}], [], max_output_tokens=50)
        body = t.last["body"]
        assert body["instructions"] == "THE SYSTEM PROMPT"
        assert body["input"] == [{"role": "user", "content": "hello"},
                                 {"role": "assistant", "content": "hi there"},
                                 {"role": "user", "content": "where is my order?"}]
        assert "THE SYSTEM PROMPT" not in json.dumps(body["input"])

    def test_no_tools_key_when_no_tools(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("hi"))
        openai_client().complete("S", "P", max_output_tokens=50)
        assert "tools" not in t.last["body"]
        assert t.last["body"]["input"] == [{"role": "user", "content": "P"}]

    def test_function_tools_use_the_flat_shape_with_the_schemas_unchanged(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("hi"))
        definitions = llm_tool_definitions()
        openai_client().chat("S", HISTORY, definitions, max_output_tokens=50)
        sent = {tool["name"]: tool for tool in t.last["body"]["tools"]}
        assert set(sent) == {d["name"] for d in definitions}
        for definition in definitions:
            tool = sent[definition["name"]]
            assert tool["type"] == "function" and "function" not in tool
            assert tool["description"] == definition["description"]
            assert tool["parameters"] == definition["parameters"]
            assert tool["parameters"]["additionalProperties"] is False
            assert tool["strict"] in (True, False)  # always explicit: the API default is strict

    def test_strict_is_set_only_for_schemas_that_satisfy_the_strict_rules(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("hi"))
        openai_client().chat("S", HISTORY, llm_tool_definitions(), max_output_tokens=50)
        strict = {tool["name"]: tool["strict"] for tool in t.last["body"]["tools"]}
        # Every property required and no extras: strict. Optional properties: not strict.
        assert {n for n, s in strict.items() if s} == {
            "list_my_orders", "get_order_status", "get_my_returns", "get_my_tickets",
            "search_policies", "start_return_request"}
        assert {n for n, s in strict.items() if not s} == {
            "get_refund_policy", "lookup_product", "start_support_ticket"}

    def test_tool_exchange_is_structured_items_not_text(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("hi"))
        openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        items = t.last["body"]["input"]
        assert items == [
            {"role": "user", "content": "where is ORD1001"},
            {"type": "function_call", "call_id": "call_1", "name": "get_order_status",
             "arguments": '{"order_id": "ORD1001"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": '{"status":"ok"}'},
        ]
        assert "[tool call" not in json.dumps(items) and "[tool result" not in json.dumps(items)

    def test_provider_items_are_echoed_unchanged_and_in_order(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("hi"))
        carried = [reasoning(), message_item("Looking that up."),
                   function_call("get_order_status", '{"order_id": "ORD1001"}', "call_9")]
        messages = [
            {"role": "user", "content": "where is ORD1001"},
            {"role": "assistant", "tool_calls": [{"name": "get_order_status",
                                                  "arguments": {"order_id": "ORD1001"}, "call_id": "call_9"}],
             "provider_items": carried},
            {"role": "tool", "name": "get_order_status", "call_id": "call_9", "content": "{}"},
        ]
        openai_client().chat("S", messages, TOOLS, max_output_tokens=50)
        assert t.last["body"]["input"] == [
            {"role": "user", "content": "where is ORD1001"}, *carried,
            {"type": "function_call_output", "call_id": "call_9", "output": "{}"}]

    def test_a_tool_result_without_a_call_id_is_refused_before_any_request(self, monkeypatch):
        t = Transport(monkeypatch)
        bad = [{"role": "user", "content": "x"}, {"role": "tool", "name": "t", "content": "{}"}]
        with pytest.raises(LLMError) as caught:
            openai_client().chat("S", bad, TOOLS, max_output_tokens=50)
        assert caught.value.category == "invalid_request" and t.requests == []


# ===========================================================================
# OpenAI Responses API: response
# ===========================================================================

class TestOpenAIResponse:
    def test_assistant_text(self, monkeypatch):
        Transport(monkeypatch, responses_text("Hello there"))
        result = openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert result.text == "Hello there" and result.tool_calls == () and result.provider_items == ()

    def test_text_parts_are_joined_and_a_refusal_is_text(self, monkeypatch):
        item = {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "a"}, {"type": "output_text", "text": "b"},
            {"type": "refusal", "refusal": " no."}]}
        Transport(monkeypatch, response(item))
        assert openai_client().chat("S", HISTORY, [], max_output_tokens=50).text == "ab no."

    def test_structured_function_call_with_its_call_id(self, monkeypatch):
        Transport(monkeypatch, responses_call("get_order_status", '{"order_id": "ORD1001"}', "call_abc"))
        result = openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        call = result.tool_calls[0]
        assert (call.name, call.arguments, call.call_id) == (
            "get_order_status", {"order_id": "ORD1001"}, "call_abc")

    def test_the_complete_output_array_is_carried_verbatim_and_in_order(self, monkeypatch):
        call = function_call("get_order_status", '{"order_id": "ORD1001"}', "call_abc")
        unknown = {"type": "some_future_item", "id": "x_1", "payload": {"keep": ["me", 1]}}
        output = [reasoning(), message_item("Checking."), call, unknown]
        Transport(monkeypatch, response(*output))
        result = openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert list(result.provider_items) == output  # every item, original order, original content
        assert result.tool_calls[0].call_id == "call_abc"
        assert HIDDEN_REASONING not in repr(result) and ENCRYPTED not in repr(result)

    def test_the_narration_next_to_a_call_is_data_for_replay_not_an_answer(self, monkeypatch):
        Transport(monkeypatch, response(reasoning(), message_item("Checking."),
                                        function_call("list_my_orders", "{}", "c1")))
        result = openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert result.text == "Checking." and result.tool_calls  # the orchestrator ignores text when calls exist

    def test_reasoning_alone_is_not_carried_when_there_is_no_call(self, monkeypatch):
        Transport(monkeypatch, response(reasoning(), message_item("Done.")))
        result = openai_client().chat("S", HISTORY, [], max_output_tokens=50)
        assert result.text == "Done." and result.provider_items == ()

    def test_several_calls_keep_their_own_ids_in_order(self, monkeypatch):
        Transport(monkeypatch, response(function_call("get_order_status", '{"order_id": "ORD1001"}', "c1"),
                                        function_call("list_my_orders", "{}", "c2")))
        result = openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert [(c.name, c.call_id) for c in result.tool_calls] == [("get_order_status", "c1"),
                                                                    ("list_my_orders", "c2")]

    @pytest.mark.parametrize("arguments", ["", None, "{}"])
    def test_empty_arguments_become_an_empty_object(self, monkeypatch, arguments):
        Transport(monkeypatch, responses_call("list_my_orders", arguments))
        assert openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50).tool_calls[0].arguments == {}

    def test_complete_returns_text(self, monkeypatch):
        Transport(monkeypatch, responses_text("same_issue"))
        assert openai_client().complete("S", "P", max_output_tokens=16) == "same_issue"

    def test_complete_without_text_raises(self, monkeypatch):
        Transport(monkeypatch, response(reasoning()))
        with pytest.raises(LLMError) as caught:
            openai_client().complete("S", "P", max_output_tokens=16)
        assert caught.value.category == "invalid_response"

    @pytest.mark.parametrize("body", [
        {}, {"output": None}, {"output": "x"}, {"output": {}}, {"output": [None]}, {"output": ["text"]},
        {"output": [{"type": "message", "content": ["x"]}]},
        {"output": [{"type": "message", "content": [{"type": "output_text"}]}]},
        response(function_call("t", "{not json")), response(function_call("t", "[1, 2]")),
        response({"type": "function_call", "name": "t", "arguments": "{}"}),               # no call_id
        response({"type": "function_call", "call_id": "", "name": "t", "arguments": "{}"}),  # empty call_id
        response({"type": "function_call", "call_id": "c", "arguments": "{}"}),             # no name
        response({"type": "function_call", "call_id": "c", "name": 5, "arguments": "{}"}),
    ])
    def test_malformed_responses_raise_llm_error(self, monkeypatch, body):
        Transport(monkeypatch, body)
        with pytest.raises(LLMError) as caught:
            openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert caught.value.category == "invalid_response" and KEY not in str(caught.value)

    @pytest.mark.parametrize("body,category", [
        ({"status": "failed", "output": []}, "provider_error"),
        ({"status": "cancelled", "output": []}, "provider_error"),
        ({"status": "completed", "error": {"code": "x"}, "output": []}, "provider_error"),
    ])
    def test_failed_responses_raise_typed_errors(self, monkeypatch, body, category):
        Transport(monkeypatch, body)
        with pytest.raises(LLMError) as caught:
            openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert caught.value.category == category

    @pytest.mark.parametrize("body", [
        {"status": "incomplete", "output": []},
        {"status": "incomplete", "output": [reasoning()]},
        response(message_item("Partial answer"), status="incomplete"),                       # partial text
        response(reasoning(), message_item("Partial answer"), status="incomplete"),
        response(function_call("t", '{"a":'), status="incomplete"),                         # truncated arguments
        response(function_call("list_my_orders", "{}", "c1"), status="incomplete"),         # looks complete
        response(message_item("Checking."), function_call("list_my_orders", "{}"), status="incomplete"),
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
         "output": [message_item("Partial answer")]},
    ], ids=["empty", "reasoning-only", "partial-text", "reasoning-and-text", "truncated-arguments",
            "complete-looking-call", "text-and-call", "with-details"])
    def test_every_incomplete_response_is_rejected_even_with_usable_looking_output(self, monkeypatch, body):
        Transport(monkeypatch, body)
        with pytest.raises(LLMError) as caught:
            openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert caught.value.category == "incomplete" and caught.value.status is None
        assert "Partial answer" not in str(caught.value) and KEY not in str(caught.value)

    @pytest.mark.parametrize("item", [
        {**function_call("list_my_orders", "{}", "c1"), "status": "incomplete"},
        {**message_item("Half an answ"), "status": "incomplete"},
    ], ids=["function-call", "message"])
    def test_an_incomplete_item_is_rejected_even_when_the_outer_status_is_completed(self, monkeypatch, item):
        Transport(monkeypatch, response(reasoning(), item, status="completed"))
        with pytest.raises(LLMError) as caught:
            openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert caught.value.category == "incomplete"

    def test_one_incomplete_call_rejects_the_whole_response_including_complete_calls(self, monkeypatch):
        Transport(monkeypatch, response(function_call("list_my_orders", "{}", "c1"),
                                        {**function_call("get_my_returns", "{}", "c2"), "status": "incomplete"}))
        with pytest.raises(LLMError) as caught:
            openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert caught.value.category == "incomplete"

    def test_complete_is_rejected_when_incomplete(self, monkeypatch):
        Transport(monkeypatch, response(message_item("same_iss"), status="incomplete"))
        with pytest.raises(LLMError) as caught:
            openai_client().complete("S", "P", max_output_tokens=16)
        assert caught.value.category == "incomplete"

    def test_an_incomplete_response_logs_only_safe_metadata(self, monkeypatch, caplog):
        Transport(monkeypatch, response(message_item(CUSTOMER_TEXT), reasoning(),
                                        function_call("t", '{"secret": "ORD777777"'), status="incomplete"))
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(LLMError):
                openai_client().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        records = [r for r in caplog.records if r.name == "app.llm"]
        assert len(records) == 1 and "category=incomplete" in records[0].getMessage()
        for secret in (CUSTOMER_TEXT, HIDDEN_REASONING, ENCRYPTED, "ORD777777", KEY):
            assert secret not in caplog.text


# ===========================================================================
# Anthropic (unchanged behavior)
# ===========================================================================

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
        assert "store" not in body and "instructions" not in body

    def test_tool_exchange_is_still_flattened_to_text_without_ids_or_provider_state(self, monkeypatch):
        t = Transport(monkeypatch, anthropic_blocks({"type": "text", "text": "hi"}))
        messages = [dict(m) for m in HISTORY]
        messages[1]["provider_items"] = [reasoning()]
        anthropic_client().chat("S", messages, TOOLS, max_output_tokens=5)
        text = json.dumps(t.last["body"]["messages"])
        assert '[tool call: [{\\"name\\": \\"get_order_status\\", \\"arguments\\": {\\"order_id\\": \\"ORD1001\\"}}]]' in text
        assert "[tool result: get_order_status]" in text
        assert "call_1" not in text and ENCRYPTED not in text and "provider_items" not in text

    def test_text_blocks_are_joined(self, monkeypatch):
        Transport(monkeypatch, anthropic_blocks({"type": "text", "text": "a"}, {"type": "text", "text": "b"}))
        assert anthropic_client().chat("S", HISTORY, [], max_output_tokens=5).text == "ab"

    def test_tool_use(self, monkeypatch):
        Transport(monkeypatch, anthropic_blocks(
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "tu1", "name": "get_order_status", "input": {"order_id": "ORD1001"}}))
        result = anthropic_client().chat("S", HISTORY, TOOLS, max_output_tokens=5)
        assert result.text == "Let me check." and result.provider_items == ()
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
        with pytest.raises(LLMError) as caught:
            anthropic_client().chat("S", HISTORY, TOOLS, max_output_tokens=5)
        assert caught.value.category == "invalid_response"

    def test_complete_without_text_raises(self, monkeypatch):
        Transport(monkeypatch, anthropic_blocks({"type": "tool_use", "name": "t", "input": {}}))
        with pytest.raises(LLMError):
            anthropic_client().complete("S", "P", max_output_tokens=5)


# ===========================================================================
# Failure contract (both providers share the transport)
# ===========================================================================

@pytest.mark.parametrize("make", [openai_client, anthropic_client], ids=["openai", "anthropic"])
class TestFailureContract:
    @pytest.mark.parametrize("status,category", [
        (400, "bad_request"), (422, "bad_request"), (401, "auth"), (403, "auth"), (404, "not_found"),
        (429, "rate_limited"), (500, "server_error"), (503, "server_error"), (418, "http_error")])
    def test_http_statuses_are_distinguished(self, monkeypatch, make, status, category):
        t = Transport(monkeypatch, http_error(status))
        with pytest.raises(LLMError) as caught:
            make().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert (caught.value.category, caught.value.status) == (category, status)
        assert len(t.requests) == 1  # never retried
        assert KEY not in str(caught.value)

    @pytest.mark.parametrize("failure,category", [
        (TimeoutError("timed out"), "timeout"),
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        (urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")), "dns"),
        (urllib.error.URLError("no route"), "network"),
        (ConnectionResetError("reset"), "network"),
        (http.client.IncompleteRead(b"par"), "network"),
    ], ids=["timeout", "url-timeout", "dns", "url-error", "reset", "incomplete-read"])
    def test_timeouts_and_network_failures_are_distinguished(self, monkeypatch, make, failure, category):
        t = Transport(monkeypatch, failure)
        with pytest.raises(LLMError) as caught:
            make().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert caught.value.category == category and caught.value.status is None
        assert len(t.requests) == 1 and KEY not in str(caught.value)

    @pytest.mark.parametrize("body,category", [
        (b"\xff\xfe not utf-8", "invalid_response"), (b"<html>not json</html>", "invalid_json"),
        (b"[1, 2, 3]", "invalid_response"), (b"", "invalid_json")])
    def test_bad_bodies_are_distinguished(self, monkeypatch, make, body, category):
        t = Transport(monkeypatch, body)
        with pytest.raises(LLMError) as caught:
            make().chat("S", HISTORY, TOOLS, max_output_tokens=50)
        assert caught.value.category == category and len(t.requests) == 1

    def test_the_error_message_never_echoes_the_provider_text_or_the_key(self, monkeypatch, make):
        Transport(monkeypatch, urllib.error.URLError(f"failed with {KEY}"))
        with pytest.raises(LLMError) as caught:
            make().complete("S", "P", max_output_tokens=50)
        assert KEY not in str(caught.value) and KEY not in repr(caught.value)
        Transport(monkeypatch, http_error(401, {"error": {"message": f"bad key {KEY}", "code": "invalid_api_key"}}))
        with pytest.raises(LLMError) as caught:
            make().complete("S", "P", max_output_tokens=50)
        assert KEY not in str(caught.value) and "bad key" not in str(caught.value)

    def test_the_typed_contract(self, monkeypatch, make):
        Transport(monkeypatch, http_error(429))
        with pytest.raises(LLMError) as caught:
            make().complete("S", "P", max_output_tokens=50)
        assert isinstance(caught.value, Exception) and caught.value.category == "rate_limited"
        assert issubclass(LLMConfigError, LLMError)


# ===========================================================================
# Diagnostics and redaction
# ===========================================================================

class TestDiagnosticsAndRedaction:
    @staticmethod
    def secret_messages():
        return [{"role": "user", "content": CUSTOMER_TEXT},
                {"role": "assistant", "tool_calls": [{"name": "get_order_status",
                                                      "arguments": {"order_id": "ORD777777"},
                                                      "call_id": "call_secret"}],
                 "provider_items": [reasoning(), message_item("NARRATION-PRIVATE"),
                                    function_call("get_order_status", '{"order_id": "ORD777777"}', "call_secret")]},
                {"role": "tool", "name": "get_order_status", "call_id": "call_secret",
                 "content": '{"data": {"payment_status": "PRIVATE-RECORD-FIELD"}}'}]

    def test_an_http_error_logs_only_safe_metadata(self, monkeypatch, caplog):
        body = {"error": {"message": f"Unsupported parameter. {KEY} {CUSTOMER_TEXT}",
                          "type": "invalid_request_error", "code": "unsupported_parameter",
                          "param": "max_output_tokens"}}
        Transport(monkeypatch, http_error(400, body))
        with caplog.at_level(logging.DEBUG, logger="app.llm"):
            with pytest.raises(LLMError):
                openai_client().chat("S", self.secret_messages(), TOOLS, max_output_tokens=50)
        records = [r for r in caplog.records if r.name == "app.llm"]
        assert len(records) == 1 and records[0].levelno == logging.WARNING
        text = records[0].getMessage()
        for expected in ("provider=openai", "model=test-model", "category=bad_request", "status=400",
                         "error_type=invalid_request_error", "error_code=unsupported_parameter",
                         "error_param=max_output_tokens"):
            assert expected in text
        assert "Unsupported parameter" not in caplog.text

    def test_error_identifiers_that_are_not_plain_identifiers_are_dropped(self, monkeypatch, caplog):
        body = {"error": {"type": "has spaces and customer text", "code": "x" * 200, "param": {"a": 1}}}
        Transport(monkeypatch, http_error(400, body))
        with caplog.at_level(logging.DEBUG, logger="app.llm"):
            with pytest.raises(LLMError):
                openai_client().complete("S", "P", max_output_tokens=50)
        text = caplog.records[-1].getMessage()
        assert "error_type=-" in text and "error_code=-" in text and "error_param=-" in text

    @pytest.mark.parametrize("failure", [
        http_error(500), http_error(401, {"error": {"code": "invalid_api_key"}}), TimeoutError("t"),
        urllib.error.URLError(f"x {KEY} {CUSTOMER_TEXT}"), b"<html>", responses_text(CUSTOMER_TEXT) | {"output": 1}])
    @pytest.mark.parametrize("make", [openai_client, anthropic_client], ids=["openai", "anthropic"])
    def test_logs_never_contain_keys_headers_conversation_or_tool_data(self, monkeypatch, caplog, make, failure):
        Transport(monkeypatch, failure)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(LLMError):
                make().chat("SYSTEM PROMPT TEXT", self.secret_messages(), TOOLS, max_output_tokens=50)
        everything = caplog.text + "".join(r.getMessage() for r in caplog.records)
        for secret in (KEY, "Bearer", "authorization", "x-api-key", CUSTOMER_TEXT, "ORD777777",
                       "PRIVATE-RECORD-FIELD", "SYSTEM PROMPT TEXT", "call_secret",
                       "NARRATION-PRIVATE", HIDDEN_REASONING, ENCRYPTED):
            assert secret.lower() not in everything.lower(), secret
        assert len([r for r in caplog.records if r.name == "app.llm"]) == 1

    def test_a_successful_call_logs_nothing(self, monkeypatch, caplog):
        Transport(monkeypatch, responses_text("fine"))
        with caplog.at_level(logging.DEBUG):
            openai_client().chat("S", self.secret_messages(), TOOLS, max_output_tokens=50)
        assert [r for r in caplog.records if r.name == "app.llm"] == []

    def test_provider_state_is_not_in_the_repr_of_a_result(self):
        result = LLMResult(text="t", tool_calls=(ToolCall("n", {}, "c"),), provider_items=(reasoning(),))
        assert ENCRYPTED not in repr(result) and HIDDEN_REASONING not in repr(result)


# ===========================================================================
# The Responses adapter driving the real backend (mocked transport)
# ===========================================================================

def outputs(request_data, call_id=None):
    """The function_call_output items of a recorded request body."""
    items = json.loads(request_data)["input"]
    return [i for i in items if i.get("type") == "function_call_output"
            and (call_id is None or i["call_id"] == call_id)]


class TestResponsesAdapterWithTheBackend:
    def backend(self):
        return Backend(llm=openai_client(), settings=Settings())

    def test_tool_call_then_answer(self, temp_db, monkeypatch):
        t = Transport(monkeypatch,
                      responses_call("get_order_status", '{"order_id": "ORD1001"}', "call_one"),
                      responses_text("Your order ORD1001 was delivered."))
        reply = Client(self.backend(), "C101").say("where is ORD1001?")
        assert reply.status == "ok" and reply.text == "Your order ORD1001 was delivered."
        assert len(t.requests) == 2
        assert all(t.sent(i)["body"]["store"] is False for i in range(2))
        items = t.sent(1)["body"]["input"]
        assert [i.get("type", i.get("role")) for i in items] == ["user", "function_call", "function_call_output"]
        assert items[1]["call_id"] == items[2]["call_id"] == "call_one"
        # The tool result came from the database, scoped to C101, and carries no identity.
        output = items[2]["output"]
        assert "Delivered" in output and "C101" not in output and KEY not in json.dumps(items)

    def test_multi_round_tool_use_keeps_every_call_id_and_the_reasoning_context(self, temp_db, monkeypatch):
        r1, m1 = reasoning("rs_1"), message_item("NARRATION-ONE")
        r2, m2 = reasoning("rs_2"), message_item("NARRATION-TWO")
        f1 = function_call("get_order_status", '{"order_id": "ORD1001"}', "call_a")
        f2 = function_call("list_my_orders", "{}", "call_b")
        t = Transport(monkeypatch, response(r1, m1, f1), response(r2, m2, f2),
                      responses_text("ORD1001 is delivered and you have three orders."))
        reply = Client(self.backend(), "C101").say("where is ORD1001, and what else did I order?")
        assert reply.status == "ok" and "delivered" in reply.text
        assert len(t.requests) == 3
        third = t.sent(2)["body"]["input"]
        assert [i.get("type", i.get("role")) for i in third] == [
            "user", "reasoning", "message", "function_call", "function_call_output",
            "reasoning", "message", "function_call", "function_call_output"]
        assert third[1:4] == [r1, m1, f1] and third[5:8] == [r2, m2, f2]
        assert third[4]["call_id"] == "call_a" and "Delivered" in third[4]["output"]
        assert third[8]["call_id"] == "call_b" and "total_orders" in third[8]["output"]
        for private in (HIDDEN_REASONING, ENCRYPTED, "NARRATION-ONE", "NARRATION-TWO"):
            assert private not in reply.text

    def test_the_whole_response_output_round_trips_unchanged_before_the_matching_output(self, temp_db, monkeypatch):
        output = [reasoning("rs_9"), message_item("PRIVATE NARRATION"),
                  function_call("get_order_status", '{"order_id": "ORD1001"}', "call_rt"),
                  {"type": "some_future_item", "id": "x_9", "payload": {"keep": ["me", 1]}}]
        t = Transport(monkeypatch, response(*output), responses_text("Your order was delivered."))
        reply = Client(self.backend(), "C101").say("where is ORD1001?")
        assert reply.text == "Your order was delivered."
        second = t.sent(1)["body"]["input"]
        assert second[0] == {"role": "user", "content": "where is ORD1001?"}
        assert second[1:5] == output  # all items, original order, original content
        assert [i["type"] for i in second[5:]] == ["function_call_output"]
        assert second[5]["call_id"] == "call_rt" and "Delivered" in second[5]["output"]
        assert t.sent(1)["body"]["store"] is False
        for private in ("PRIVATE NARRATION", HIDDEN_REASONING, ENCRYPTED, "rs_9"):
            assert private not in reply.text

    def test_provider_items_are_not_logged_when_a_later_request_fails(self, temp_db, monkeypatch, caplog):
        Transport(monkeypatch,
                  response(reasoning(), message_item("PRIVATE NARRATION"),
                           function_call("get_order_status", '{"order_id": "ORD1001"}', "call_lg")),
                  http_error(500))
        with caplog.at_level(logging.DEBUG):
            reply = Client(self.backend(), "C101").say("where is ORD1001?")
        assert reply.status == "error" and reply.error_code == "unavailable"
        assert "category=server_error" in caplog.text
        for private in ("PRIVATE NARRATION", HIDDEN_REASONING, ENCRYPTED, "call_lg", "ORD1001", KEY):
            assert private not in caplog.text

    def test_an_incomplete_response_cannot_run_a_tool(self, temp_db, monkeypatch):
        t = Transport(monkeypatch, response(function_call("get_order_status", '{"order_id": "ORD1001"}', "ci"),
                                            status="incomplete"))
        reply = Client(self.backend(), "C101").say("where is ORD1001?")
        assert reply.status == "error" and reply.error_code == "unavailable"
        assert len(t.requests) == 1  # no tool ran, so no follow-up request with an output was made
        assert "incomplete" not in reply.text.lower() and "ORD1001" not in reply.text
        assert "temporarily unavailable" in reply.text

    def test_an_incomplete_response_cannot_start_a_return_or_a_ticket(self, temp_db, raw, monkeypatch):
        count = lambda table: raw.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        message = "I want to return item IT101 from order ORD1001 because I changed my mind about it"
        Transport(monkeypatch,
                  # outer status incomplete
                  response(function_call("start_return_request", '{"order_id": "ORD1001", "item_id": "IT101"}', "r1"),
                           status="incomplete"),
                  # outer status completed, but the call item itself is incomplete
                  response({**function_call("start_return_request",
                                            '{"order_id": "ORD1001", "item_id": "IT101"}', "r2"),
                            "status": "incomplete"}),
                  response(function_call("start_support_ticket", '{"issue_type": "product"}', "t1"),
                           status="incomplete"))
        customer = Client(self.backend(), "C101")
        for text in (message, message):
            reply = customer.say(text)
            assert reply.status == "error" and reply.error_code == "unavailable"
            assert reply.choice is None and reply.pending is None
        ticket = customer.say("I want to speak to a human agent")
        assert ticket.status == "error" and ticket.pending is None
        assert count("return_requests") == 0 and count("support_tickets") == 0

    def test_a_cut_off_answer_is_never_shown_as_the_assistants_reply(self, temp_db, monkeypatch):
        Transport(monkeypatch, response(message_item("Your order was deliv"), status="incomplete"))
        reply = Client(self.backend(), "C101").say("where is ORD1001?")
        assert reply.status == "error" and "deliv" not in reply.text

    def test_parallel_calls_each_get_an_output_with_the_matching_id(self, temp_db, monkeypatch):
        t = Transport(monkeypatch,
                      response(function_call("get_order_status", '{"order_id": "ORD1001"}', "p1"),
                               function_call("list_my_orders", "{}", "p2")),
                      responses_text("Done."))
        Client(self.backend(), "C101").say("status of ORD1001 and my orders")
        second = t.sent(1)["body"]["input"]
        assert [i.get("type", i.get("role")) for i in second] == [
            "user", "function_call", "function_call", "function_call_output", "function_call_output"]
        assert [i["call_id"] for i in second[1:3]] == ["p1", "p2"]
        assert [i["call_id"] for i in second[3:]] == ["p1", "p2"]

    def test_the_existing_tool_call_limit_still_stops_the_loop(self, temp_db, monkeypatch):
        t = Transport(monkeypatch, *[responses_call("list_my_orders", "{}", f"c{i}") for i in range(7)])
        reply = Client(self.backend(), "C101").say("my orders please")
        assert len(t.requests) == 7 and "wasn't able to finish" in reply.text
        assert len(outputs(t.requests[6][0].data)) == 6  # the seventh call was never executed

    def test_cross_customer_tool_call_gets_not_found(self, temp_db, monkeypatch):
        t = Transport(monkeypatch, responses_call("get_order_status", '{"order_id": "ORD1003"}', "x1"),
                      responses_text("I couldn't find that order."))
        Client(self.backend(), "C101").say("status of ORD1003")
        output = outputs(t.requests[1][0].data, "x1")[0]["output"]
        assert "order_not_found" in output and "P1002" not in output and "C102" not in output

    def test_a_customer_id_argument_from_the_model_is_rejected(self, temp_db, monkeypatch):
        t = Transport(monkeypatch, responses_call("list_my_orders", '{"customer_id": "C102"}', "x2"),
                      responses_text("Sorry."))
        Client(self.backend(), "C101").say("my orders")
        output = outputs(t.requests[1][0].data, "x2")[0]["output"]
        assert "invalid_arguments" in output and "ORD1003" not in output

    def test_model_asking_for_an_unknown_tool_is_refused(self, temp_db, monkeypatch):
        t = Transport(monkeypatch, responses_call("delete_everything", "{}", "x3"),
                      responses_text("Sorry, I can't do that."))
        reply = Client(self.backend(), "C101").say("do it")
        assert reply.text == "Sorry, I can't do that."
        assert "unknown_tool" in outputs(t.requests[1][0].data, "x3")[0]["output"]

    def test_provider_failure_becomes_a_generic_reply_and_a_categorized_log(self, temp_db, monkeypatch, caplog):
        Transport(monkeypatch, http_error(401, {"error": {"code": "invalid_api_key"}}))
        with caplog.at_level(logging.WARNING, logger="app.llm"):
            reply = Client(self.backend(), "C101").say("where is ORD1001?")
        assert reply.status == "error" and reply.error_code == "unavailable"
        assert "401" not in reply.text and "auth" not in reply.text.lower() and KEY not in reply.text
        assert "category=auth" in caplog.text and "status=401" in caplog.text and "invalid_api_key" in caplog.text
        assert KEY not in caplog.text and "ORD1001" not in caplog.text

    def test_a_model_claiming_success_is_never_a_database_write(self, temp_db, raw, monkeypatch):
        Transport(monkeypatch, responses_text("I have submitted your return request and created a ticket."))
        reply = Client(self.backend(), "C101").say("please return my earbuds IT101 from ORD1001, they are bad")
        assert "I can't confirm that anything has been done" in reply.text
        assert raw.execute("SELECT count(*) FROM return_requests").fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM support_tickets").fetchone()[0] == 0

    def test_a_return_needs_the_choice_and_an_explicit_confirm_and_uses_the_customers_reason(
            self, temp_db, raw, monkeypatch):
        t = Transport(monkeypatch, responses_call(
            "start_return_request",
            '{"order_id": "ORD1001", "item_id": "IT101", "reason": "MODEL-INVENTED-REASON"}', "ret1"))
        customer = Client(self.backend(), "C101")
        message = "I want to return item IT101 from order ORD1001 because I changed my mind about it"
        count = lambda: raw.execute("SELECT count(*) FROM return_requests").fetchone()[0]
        choice = customer.say(message)
        assert choice.status == "needs_choice" and count() == 0
        pending = customer.pick(choice, "standard")
        assert pending.status == "needs_confirmation" and count() == 0
        done = customer.confirm(pending)
        assert "RET_" in done.text and count() == 1
        row = raw.execute("SELECT customer_id, reason, reason_category FROM return_requests").fetchone()
        assert tuple(row) == ("C101", message, "standard")
        assert len(t.requests) == 1  # choice, confirmation and the write used no further model call
        assert "MODEL-INVENTED-REASON" not in json.dumps(tuple(row))

    def test_a_ticket_needs_an_explicit_confirm_and_the_summary_call_is_tool_free(self, temp_db, raw, monkeypatch):
        t = Transport(monkeypatch,
                      responses_call("start_support_ticket", '{"issue_type": "product"}', "tk1"),
                      responses_text("Customer Issue\nMy earbuds stopped charging.\n\nCurrent Status\n"
                                     "Awaiting support follow-up."))
        customer = Client(self.backend(), "C101")
        asked = customer.say("I want to speak to a human agent")
        assert asked.status == "ok" and "What is the issue" in asked.text
        proposal = customer.say("My earbuds from ORD1001 stopped charging after two days and I need help.")
        assert proposal.status == "needs_confirmation"
        assert raw.execute("SELECT count(*) FROM support_tickets").fetchone()[0] == 0
        summary_request = t.sent(1)["body"]
        assert summary_request["instructions"].startswith("TASK: summarize_ticket")
        assert "tools" not in summary_request and summary_request["store"] is False
        done = customer.confirm(proposal)
        assert "TKT_" in done.text
        assert raw.execute("SELECT count(*) FROM support_tickets").fetchone()[0] == 1


# ===========================================================================
# Provider-neutral orchestration; other LLMs unaffected
# ===========================================================================

class TestOrchestrationIsProviderNeutral:
    def test_call_ids_and_provider_items_are_passed_back_unchanged(self, temp_db):
        carried = ({"type": "reasoning", "id": "rs_x"}, {"type": "function_call", "call_id": "call_x"})
        seen = []

        def chat(system, messages, tools):
            seen.append([dict(m) for m in messages])
            if messages[-1]["role"] == "tool":
                return LLMResult(text="done")
            return LLMResult(tool_calls=(ToolCall("list_my_orders", {}, "call_x"),), provider_items=carried)

        Client(Backend(llm=ScriptedLLM(chat=chat)), "C101").say("my orders")
        assistant, tool = seen[1][-2], seen[1][-1]
        assert assistant == {"role": "assistant", "provider_items": list(carried), "tool_calls": [
            {"name": "list_my_orders", "arguments": {}, "call_id": "call_x"}]}
        assert tool["role"] == "tool" and tool["call_id"] == "call_x" and tool["name"] == "list_my_orders"

    def test_a_provider_without_extra_state_adds_no_provider_items(self, temp_db):
        seen = []

        def chat(system, messages, tools):
            seen.append(messages)
            return LLMResult(text="done") if messages[-1]["role"] == "tool" else LLMResult(
                tool_calls=(ToolCall("list_my_orders", {}),))

        Client(Backend(llm=ScriptedLLM(chat=chat)), "C101").say("my orders")
        assert "provider_items" not in seen[1][-2] and seen[1][-2]["tool_calls"][0]["call_id"] == ""

    def test_the_rule_based_stand_in_ignores_the_new_fields(self, temp_db):
        llm = RuleBasedLLM()
        messages = [{"role": "user", "content": "where is ORD1001"},
                    {"role": "assistant", "tool_calls": [{"name": "get_order_status",
                                                          "arguments": {"order_id": "ORD1001"}, "call_id": "c1"}],
                     "provider_items": [reasoning()]},
                    {"role": "tool", "name": "get_order_status", "call_id": "c1",
                     "content": json.dumps({"status": "not_found", "code": "order_not_found"})}]
        assert llm.chat("S", messages, [], max_output_tokens=50).text == "I couldn't find that on your account."
        reply = Client(Backend(llm=RuleBasedLLM()), "C101").say("where is ORD1001?")
        assert reply.status == "ok" and "Delivered" in reply.text

    def test_anthropic_still_drives_the_backend(self, temp_db, monkeypatch):
        t = Transport(monkeypatch,
                      anthropic_blocks({"type": "tool_use", "id": "1", "name": "get_order_status",
                                        "input": {"order_id": "ORD1003"}}),
                      anthropic_blocks({"type": "text", "text": "I couldn't find that order."}))
        Client(Backend(llm=anthropic_client(), settings=Settings()), "C101").say("status of ORD1003")
        second = t.requests[1][0].data.decode("utf-8")
        assert "order_not_found" in second and "P1002" not in second


# ===========================================================================
# Readiness and configuration: no network access
# ===========================================================================

class TestReadiness:
    """Configuration checks: no network, and no secret value is ever reported."""

    SECRET = "sk-ant-SUPER-SECRET-VALUE"

    def test_default_is_the_offline_stand_in(self):
        assert readiness({}) == {"provider": "rule_based", "configured": True, "missing": [], "live": False}

    def test_anthropic_fully_configured(self):
        env = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": self.SECRET, "LLM_MODEL": "some-model"}
        result = readiness(env)
        assert result == {"provider": "anthropic", "configured": True, "missing": [], "live": True}
        assert self.SECRET not in repr(result)

    def test_openai_fully_configured(self):
        env = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": self.SECRET, "LLM_MODEL": "some-model"}
        result = readiness(env)
        assert result == {"provider": "openai", "configured": True, "missing": [], "live": True}
        assert self.SECRET not in repr(result)

    @pytest.mark.parametrize("env,missing", [
        ({"LLM_PROVIDER": "anthropic"}, ["ANTHROPIC_API_KEY", "LLM_MODEL"]),
        ({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": SECRET}, ["LLM_MODEL"]),
        ({"LLM_PROVIDER": "anthropic", "LLM_MODEL": "m"}, ["ANTHROPIC_API_KEY"]),
        ({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": SECRET}, ["LLM_MODEL"]),
        ({"LLM_PROVIDER": "openai"}, ["OPENAI_API_KEY", "LLM_MODEL"]),
    ])
    def test_missing_variables_are_named_but_never_valued(self, env, missing):
        result = readiness(env)
        assert result["configured"] is False and result["missing"] == missing and result["live"] is False
        assert self.SECRET not in repr(result)

    def test_unknown_provider_and_bad_timeout(self):
        assert readiness({"LLM_PROVIDER": "nope"})["configured"] is False
        bad = readiness({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "LLM_MODEL": "m",
                         "LLM_TIMEOUT_SECONDS": "soon"})
        assert bad["configured"] is False and "LLM_TIMEOUT_SECONDS (not a number)" in bad["missing"]

    def test_readiness_agrees_with_build_llm_client(self):
        for env in ({"LLM_PROVIDER": "anthropic"}, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k"},
                    {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "LLM_MODEL": "m"},
                    {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k", "LLM_MODEL": "m"}, {}):
            if readiness(env)["configured"]:
                build_llm_client(env)
            else:
                with pytest.raises(LLMConfigError):
                    build_llm_client(env)

    def test_openai_settings_reach_the_client(self):
        client = build_llm_client({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k", "LLM_MODEL": "my-model",
                                   "OPENAI_BASE_URL": "https://example.test/v9/", "LLM_TIMEOUT_SECONDS": "3.5"})
        assert isinstance(client, OpenAIResponsesClient) and client.name == "openai"
        assert (client._model, client._base, client._timeout) == ("my-model", "https://example.test/v9", 3.5)
        default = build_llm_client({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k", "LLM_MODEL": "m"})
        assert default._base == "https://api.openai.com/v1" and default._timeout == 20.0

    def test_nothing_touches_the_network_until_a_model_request_is_made(self, temp_db, monkeypatch):
        t = Transport(monkeypatch)

        def forbidden(*args, **kwargs):
            raise AssertionError("network access")

        monkeypatch.setattr(socket, "getaddrinfo", forbidden)
        monkeypatch.setattr(socket.socket, "connect", forbidden)
        env = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k", "LLM_MODEL": "m"}
        readiness(env)
        build_llm_client(env)
        backend = Backend(env=env)
        assert isinstance(backend.llm, OpenAIResponsesClient)
        assert backend.check_backend().llm_mode == "openai" and backend.list_login_options()
        assert backend.start_session("C101").ok
        assert t.requests == []

    def test_a_misconfigured_provider_fails_closed_and_never_becomes_the_stand_in(self, temp_db):
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
