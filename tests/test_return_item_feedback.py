"""Phase 2: a wrong item in a model-proposed return no longer ends the turn.

Baseline v2 (3 live runs): C015 and C017 ended with "I couldn't find that item on that order." because the model
guessed the item ID and the engine finished the turn with a fixed error. A live check then showed the model can
also misread the feedback it is given (it told a customer an order "has only one item" when it had two). So the
recovery is layered, safest first:

1. code resolves the item when the customer's own words name exactly one item of that order (product-name words);
2. otherwise the model gets the customer's own items as a tool result and may retry once;
3. otherwise a deterministic "which item?" question.

Eligibility, ownership and the Confirm gate are untouched: every later step is the same code as before.
Scripted model output only; no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import real_eval as ev  # noqa: E402
from app.backend import Backend, Settings  # noqa: E402
from app.llm.base import LLMResult, ToolCall  # noqa: E402
from app.orchestrator import guard  # noqa: E402
from backend_helpers import Client, ScriptedLLM  # noqa: E402

NAMED = "I want to return the earbuds from ORD1001 because they keep cutting out"
# Two items on ORD1005 (a monitor and a charger) and nothing in the message that names either.
VAGUE = "I want to return the slow one from ORD1005 because it overheats"
MONITOR_AND_CHARGER = [{"item_id": "IT105", "product_name": "24-inch Monitor (Full HD)"},
                       {"item_id": "IT106", "product_name": "65W GaN Charger"}]
EARBUDS_ONLY = [{"item_id": "IT101", "product_name": "Wireless Earbuds"}]


def call(order_id, item_id, call_id="c1", **extra):
    return LLMResult(tool_calls=(ToolCall("start_return_request",
                                          {"order_id": order_id, "item_id": item_id, **extra}, call_id),))


class Sequence(ScriptedLLM):
    """Plays one scripted answer per chat call and records every conversation it was shown.

    It declares ``interprets_language`` like a real model: the offline stand-in does not get item recovery.
    """

    interprets_language = True

    def __init__(self, *answers):
        super().__init__(chat=self._next)
        self.answers = list(answers)
        self.seen: list[list[dict]] = []

    def _next(self, system, messages, tools):
        self.seen.append([dict(m) for m in messages])
        answer = self.answers.pop(0)
        return answer(messages) if callable(answer) else answer


def rows(raw, table):
    return [dict(r) for r in raw.execute(f"SELECT * FROM {table}").fetchall()]


def tool_messages(llm):
    return [json.loads(m["content"]) for conversation in llm.seen[1:] for m in conversation if m["role"] == "tool"]


def start(temp_db, llm, customer="C101", **settings):
    backend = Backend(llm=llm, settings=Settings(**settings))
    return backend, Client(backend, customer)


# ---------------------------------------------------------------------------
# 1. Code resolves an item the customer named
# ---------------------------------------------------------------------------

class TestResolveByName:
    @pytest.mark.parametrize("text, expected", [
        ("Mujhe ORD1005 ka charger wapas karna hai, slow charge karta hai", "IT106"),
        ("I want to send the monitor back", "IT105"), ("the 65W one", "IT106"), ("full hd screen is dim", "IT105"),
        ("I want to return the chargers", "IT106"),
    ])
    def test_exactly_one_item_named(self, text, expected):
        assert guard.resolve_item_by_name(text, MONITOR_AND_CHARGER) == expected

    @pytest.mark.parametrize("text", [
        "I want to return the slow one", "the monitor and the charger both", "return it", "wapas karna hai",
        "चार्जर वापस करना है", ""])
    def test_nothing_or_more_than_one_named_is_not_guessed(self, text):
        assert guard.resolve_item_by_name(text, MONITOR_AND_CHARGER) is None

    def test_a_single_item_order_resolves_when_nothing_else_is_named(self):
        for text in ("The earbuds keep cutting out. Send them back.", "send it back please", "return this"):
            assert guard.resolve_item_by_name(text, EARBUDS_ONLY) == "IT101"

    def test_a_single_item_order_does_not_resolve_when_another_product_is_named(self):
        assert guard.resolve_item_by_name("I want to return the charger", EARBUDS_ONLY) is None

    def test_an_empty_order_resolves_nothing(self):
        assert guard.resolve_item_by_name("the charger", []) is None


class TestCodeResolvesBeforeAskingTheModelAgain:
    def test_a_guessed_id_is_replaced_by_the_item_the_customer_named(self, temp_db, raw):
        llm = Sequence(call("ORD1005", "IT999"))
        backend, c = start(temp_db, llm, "C103")
        reply = c.say("Mujhe ORD1005 ka charger wapas karna hai, slow charge karta hai")
        assert reply.status == "needs_choice" and "65W GaN Charger" in reply.text
        assert len(llm.seen) == 1 and tool_messages(llm) == [] and rows(raw, "return_requests") == []

    def test_the_resolved_item_is_still_checked_by_the_database(self, temp_db, raw):
        llm = Sequence(call("ORD1002", "IT999"))  # ORD1002 is only shipped
        backend, c = start(temp_db, llm)
        reply = c.say("I want to return the charger from ORD1002 because it is slow")
        assert "delivered orders" in reply.text and reply.choice is None and rows(raw, "return_requests") == []

    def test_the_resolved_return_still_needs_the_explicit_confirm(self, temp_db, raw):
        llm = Sequence(call("ORD1005", "IT999"))
        backend, c = start(temp_db, llm, "C103")
        card = c.pick(c.say("I want to return the charger from ORD1005 because it is slow"), "standard")
        assert card.status == "needs_confirmation" and rows(raw, "return_requests") == []
        assert c.say("yes").status == "needs_confirmation" and rows(raw, "return_requests") == []
        assert "RET_" in c.confirm(card).text
        saved = rows(raw, "return_requests")
        assert len(saved) == 1 and saved[0]["item_id"] == "IT106" and saved[0]["customer_id"] == "C103"


# ---------------------------------------------------------------------------
# 2. Otherwise the model gets the customer's own items and may retry once
# ---------------------------------------------------------------------------

class TestWrongItemIsFedBack:
    def test_a_vague_request_gets_the_items_and_the_retry_reaches_the_menu(self, temp_db, raw):
        llm = Sequence(call("ORD1005", "IT999"), call("ORD1005", "IT106", "c2"))
        backend, c = start(temp_db, llm, "C103")
        reply = c.say(VAGUE)
        assert reply.status == "needs_choice" and reply.choice is not None
        assert len(llm.seen) == 2 and rows(raw, "return_requests") == []
        (feedback,) = tool_messages(llm)
        assert feedback["status"] == "invalid_input" and feedback["code"] == "item_not_in_order"
        assert feedback["data"] == {"order_id": "ORD1005", "items": MONITOR_AND_CHARGER}
        assert "item_id" in feedback["note"] and "Call start_return_request again" in feedback["note"]

    def test_a_second_wrong_item_gets_a_deterministic_which_item_question(self, temp_db, raw):
        llm = Sequence(call("ORD1005", "IT999"), call("ORD1005", "IT888", "c2"))
        backend, c = start(temp_db, llm, "C103")
        reply = c.say(VAGUE)
        assert reply.status == "ok" and reply.choice is None and reply.pending is None
        assert "Which item would you like to return" in reply.text
        assert "IT105" in reply.text and "IT106" in reply.text and "24-inch Monitor" in reply.text
        assert len(llm.seen) == 2 and rows(raw, "return_requests") == []

    def test_a_model_that_answers_in_text_after_the_feedback_is_not_overridden(self, temp_db):
        llm = Sequence(call("ORD1005", "IT999"), LLMResult(text="Which one do you mean, the monitor or the charger?"))
        backend, c = start(temp_db, llm, "C103")
        assert "monitor or the charger" in c.say(VAGUE).text

    def test_an_item_from_another_customers_order_leaks_nothing(self, temp_db):
        llm = Sequence(call("ORD1005", "IT101"), call("ORD1005", "IT106", "c2"))  # IT101 belongs to C101
        backend, c = start(temp_db, llm, "C103")
        assert c.say(VAGUE).status == "needs_choice"
        (feedback,) = tool_messages(llm)
        assert {i["item_id"] for i in feedback["data"]["items"]} == {"IT105", "IT106"}
        assert "Wireless Earbuds" not in json.dumps(feedback)

    @pytest.mark.parametrize("bad_item", ["slow", "the charger", "", "IT 101"])
    def test_a_malformed_item_is_fed_back_too(self, temp_db, bad_item):
        llm = Sequence(call("ORD1005", bad_item), call("ORD1005", "IT106", "c2"))
        backend, c = start(temp_db, llm, "C103")
        assert c.say(VAGUE).status == "needs_choice"
        (feedback,) = tool_messages(llm)
        assert feedback["code"] in ("invalid_arguments", "item_not_in_order")
        assert {i["item_id"] for i in feedback["data"]["items"]} == {"IT105", "IT106"}

    def test_an_order_that_is_not_the_customers_keeps_the_fixed_reply_and_asks_the_model_nothing_more(self, temp_db):
        llm = Sequence(call("ORD1001", "IT101"))
        backend, c = start(temp_db, llm, "C102")  # ORD1001 belongs to C101
        reply = c.say(NAMED)
        assert reply.text == "I couldn't find that order on your account." and len(llm.seen) == 1
        assert tool_messages(llm) == []

    def test_an_unusable_order_id_keeps_the_fixed_reply(self, temp_db):
        llm = Sequence(call("my order", "IT101"))
        backend, c = start(temp_db, llm)
        reply = c.say(NAMED)
        assert "valid order ID and item ID" in reply.text and len(llm.seen) == 1

    def test_a_correct_item_never_triggers_any_recovery(self, temp_db):
        llm = Sequence(call("ORD1001", "IT101"))
        backend, c = start(temp_db, llm)
        assert c.say(NAMED).status == "needs_choice" and len(llm.seen) == 1 and tool_messages(llm) == []

    def test_the_models_own_reason_is_still_ignored_after_a_retry(self, temp_db, raw):
        llm = Sequence(call("ORD1005", "IT999", reason="INVENTED"), call("ORD1005", "IT106", "c2", reason="INVENTED"))
        backend, c = start(temp_db, llm, "C103")
        choice = c.say(VAGUE)
        c.confirm(c.pick(choice, "standard"))
        assert rows(raw, "return_requests")[0]["reason"] == VAGUE

    def test_the_retry_still_needs_the_explicit_confirm(self, temp_db, raw):
        llm = Sequence(call("ORD1005", "IT999"), call("ORD1005", "IT106", "c2"))
        backend, c = start(temp_db, llm, "C103")
        card = c.pick(c.say(VAGUE), "standard")
        assert card.status == "needs_confirmation" and rows(raw, "return_requests") == []
        assert c.say("yes").status == "needs_confirmation" and rows(raw, "return_requests") == []
        assert "RET_" in c.confirm(card).text and len(rows(raw, "return_requests")) == 1

    def test_a_database_failure_while_listing_items_is_reported_not_hidden(self, temp_db, monkeypatch):
        from app.db import repository
        from app.db.errors import DatabaseOperationalError

        def down(customer_id, order_id):
            raise DatabaseOperationalError("down")

        monkeypatch.setattr(repository, "get_order_items", down)
        llm = Sequence(call("ORD1001", "IT999"))
        backend, c = start(temp_db, llm)
        reply = c.say(NAMED)
        assert reply.status == "error" and "can't reach the order system" in reply.text

    def test_the_tool_call_budget_still_applies(self, temp_db):
        llm = Sequence(call("ORD1005", "IT999"), call("ORD1005", "IT888", "c2"))
        backend, c = start(temp_db, llm, "C103", max_tool_calls_per_turn=1)
        reply = c.say(VAGUE)
        # The first call is answered with feedback; the model's second call is over the budget of one.
        assert reply.choice is None and "wasn't able to finish" in reply.text and len(llm.seen) == 2


class TestStandInIsUnchanged:
    def test_the_offline_stand_in_keeps_its_fixed_reply_for_a_wrong_item(self, temp_db):
        c = Client(Backend(llm=ScriptedLLM(), settings=Settings()), "C101")
        assert c.say("I want to return item IT102 from order ORD1001 because it is too big").text == (
            "I couldn't find that item on that order.")

    def test_the_offline_stand_in_still_starts_the_return_directly(self, temp_db):
        c = Client(Backend(llm=ScriptedLLM(), settings=Settings()), "C101")
        assert c.say("I want to return item IT101 from order ORD1001 because it is too big").status == "needs_choice"


class TestNotAskingIsNotFooledByTheWordsOrderId:
    @pytest.mark.parametrize("text", [
        "Order ID **ORD1002** ka status hai **Shipped**.", "Order ID ORD1013 ka total ₹2,499 tha.",
        "Your order ID ORD1002 is shipped."])
    def test_a_statement_that_contains_the_words_is_not_a_question(self, text):
        assert ev._ASK_RE.search(text) is None

    @pytest.mark.parametrize("text", [
        "Could you share the order ID?", "Please send your order ID.", "What is your order number?",
        "Kya aap mujhe apna order number de sakte ho?", "Order ID bata sakte hain?", "Which order do you mean?",
        "What is the issue you'd like it to be about? Please include the order ID if there is one.",
        "kaun sa order hai?"])
    def test_a_real_request_for_it_is_still_a_question(self, text):
        assert ev._ASK_RE.search(text) is not None
