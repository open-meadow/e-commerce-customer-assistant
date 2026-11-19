"""The eligibility check is about the item the customer named, settled by code and never guessed.

Live run of the new sets: asked about "the charger from ORD1005", the model called the check with a guessed item ID,
was told the item was not on the order, and asked the customer a pointless question. Code now resolves the item from
the customer's own words, the way the return flow already does, and re-runs the read-only check. It never picks an
item that conflicts with what the customer said, and it never reaches another customer's order. Scripted models only;
no network.
"""

from __future__ import annotations

import json

import pytest

import app.backend  # noqa: F401
from app.backend import Backend, Settings
from app.llm.base import LLMResult, ToolCall
from backend_helpers import Client, ScriptedLLM

CHECK = "check_return_eligibility"


class Sequence(ScriptedLLM):
    interprets_language = True

    def __init__(self, *answers):
        super().__init__(chat=self._next)
        self.answers = list(answers)
        self.seen: list[list[dict]] = []

    def _next(self, system, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.answers.pop(0)


def ask(order, item):
    return LLMResult(tool_calls=(ToolCall(CHECK, {"order_id": order, "item_id": item}, "e1"),))


def run(customer, message, *steps):
    llm = Sequence(*steps)
    backend = Backend(llm=llm, settings=Settings())
    reply = Client(backend, customer).say(message)
    tools = [json.loads(m["content"]) for conversation in llm.seen[1:] for m in conversation if m["role"] == "tool"]
    return reply, llm, tools


DONE = LLMResult(text="The ordinary return period is open until 12 November 2026, 12:00 UTC.")


class TestResolvedFromTheCustomersWords:
    def test_a_guessed_id_on_a_multi_item_order_becomes_the_item_the_customer_named(self, temp_db):
        reply, llm, tools = run("C103", "Is it too late to return the charger from ORD1005?", ask("ORD1005", "IT999"), DONE)
        assert llm.chat_calls == 2  # no retry round trip was needed
        assert tools[0]["data"]["result"] == "timing_check"
        assert tools[0]["data"]["item_resolved_from_customer_words"] == "IT106"
        assert "12 November 2026, 12:00 UTC" in json.dumps(tools[0]["data"])

    def test_a_valid_but_different_item_is_replaced_by_the_one_the_customer_described(self, temp_db):
        reply, llm, tools = run("C103", "Can I still return the charger from ORD1005?", ask("ORD1005", "IT105"), DONE)
        assert tools[0]["data"]["item_resolved_from_customer_words"] == "IT106"

    def test_the_monitor_is_chosen_when_the_customer_says_monitor(self, temp_db):
        reply, llm, tools = run("C103", "Can I still return the monitor from ORD1005?", ask("ORD1005", "IT106"), DONE)
        assert tools[0]["data"]["item_resolved_from_customer_words"] == "IT105"

    def test_a_guess_on_a_one_item_order_becomes_the_only_item(self, temp_db):
        reply, llm, tools = run("C101", "Can I still return my order ORD1001?", ask("ORD1001", "IT999"), DONE)
        assert tools[0]["data"]["item_resolved_from_customer_words"] == "IT101"

    def test_hinglish_words_name_the_item_too(self, temp_db):
        reply, llm, tools = run("C103", "ORD1005 ka charger abhi bhi return ho sakta hai kya?", ask("ORD1005", "IT999"), DONE)
        assert tools[0]["data"]["item_resolved_from_customer_words"] == "IT106"

    def test_the_correct_item_is_left_alone(self, temp_db):
        reply, llm, tools = run("C103", "Can I still return the charger from ORD1005?", ask("ORD1005", "IT106"), DONE)
        assert "item_resolved_from_customer_words" not in tools[0]["data"]


class TestNeverGuessedAndNeverConflicting:
    def test_nothing_named_on_a_multi_item_order_leaves_the_models_item_and_lists_the_options(self, temp_db):
        reply, llm, tools = run("C103", "Is it too late to return something from ORD1005?", ask("ORD1005", "IT999"),
                                LLMResult(text="Which item do you mean?"))
        data = tools[0]["data"]
        assert data["result"] == "item_not_on_this_order" and "item_resolved_from_customer_words" not in data
        assert [i["item_id"] for i in data["order_items"]] == ["IT105", "IT106"]

    def test_a_different_product_named_on_a_one_item_order_is_not_silently_replaced(self, temp_db):
        reply, llm, tools = run("C101", "Can I still return the charger from ORD1001?", ask("ORD1001", "IT999"),
                                LLMResult(text="That order has the earbuds."))
        data = tools[0]["data"]
        assert data["result"] == "item_not_on_this_order" and "item_resolved_from_customer_words" not in data

    def test_a_valid_item_that_agrees_with_no_description_is_kept(self, temp_db):
        reply, llm, tools = run("C103", "Can I still return something from ORD1005?", ask("ORD1005", "IT105"), DONE)
        assert tools[0]["data"]["result"] == "timing_check" and "item_resolved_from_customer_words" not in tools[0]["data"]

    def test_two_items_named_are_not_guessed(self, temp_db):
        reply, llm, tools = run("C103", "Can I return the charger and the monitor from ORD1005?", ask("ORD1005", "IT999"),
                                LLMResult(text="Which one first?"))
        assert tools[0]["data"]["result"] == "item_not_on_this_order"


class TestOwnershipStaysWithTheBackend:
    def test_another_customers_order_is_not_found_and_nothing_is_listed_or_recovered(self, temp_db):
        reply, llm, tools = run("C102", "Can I still return the charger from ORD1005?", ask("ORD1005", "IT106"),
                                LLMResult(text="I could not find that order on your account."))
        body = json.dumps(tools[0])
        assert tools[0]["status"] == "not_found" and "order_items" not in body and "IT106" not in body
        assert "item_resolved_from_customer_words" not in body and "GaN" not in body

    def test_the_recovery_never_writes(self, temp_db, raw):
        run("C103", "Is it too late to return the charger from ORD1005?", ask("ORD1005", "IT999"), DONE)
        assert raw.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0

    def test_it_does_not_touch_the_return_request_path(self, temp_db):
        llm = Sequence(LLMResult(tool_calls=(ToolCall("start_return_request",
                                                      {"order_id": "ORD1005", "item_id": "IT106"}, "r1"),)))
        backend = Backend(llm=llm, settings=Settings())
        reply = Client(backend, "C103").say("I want to return the charger from ORD1005, it overheats")
        assert reply.status == "needs_choice" and llm.chat_calls == 1

    def test_the_resolved_check_is_still_only_a_timing_check(self, temp_db):
        reply, llm, tools = run("C103", "Is it too late to return the charger from ORD1005?", ask("ORD1005", "IT999"), DONE)
        note = tools[0]["note"].lower()
        assert "timing check only, not an approval" in note and "never convert" in note
