"""A customer asks to return an item and the model answers in PROSE, without calling any return tool.

Live report: C102 (ORD1003 delivered 10 days before the seed time, past even the 7-day standard window) said "product is not
working as expected i want to return it" and the assistant answered "Is that the item you want to return? If so, choose the
return reason category in the interface", then repeated "select the return reason category in the interface" when the customer
said yes. No category buttons ever existed, and an order that cannot be returned was presented as returnable, because the
engine's eligibility check (``_refusal_before_reason``) only ran when the model called ``start_return_request``.

The engine now settles an explicit return request by code whenever the model did not propose ``start_return_request``: a
delivered order whose window is closed gets the fixed refusal and one ticket offer; an eligible single-item order starts the
normal return flow (reason, then the category buttons, then the customer's Confirm). The order comes only from the customer's
words or their only delivered order, never from the model's reply. Several delivered orders, several items and false success
claims are covered in ``test_return_prose_guard_edges.py``. Anything unclear is left to the model. Offline: scripted models, a
temporary seeded database, no network.
"""

from __future__ import annotations

import app.backend  # noqa: F401
from app.backend import Backend, Settings
from app.db import repository
from app.llm.base import LLMResult
from app.tools import flow_texts
from tests.backend_helpers import Client, ScriptedLLM, calls

PROSE_ORD1003 = ("I see **Over-Ear Headphones** in delivered order **ORD1003**; your other orders haven’t been delivered yet. "
                 "Is that the item you want to return? If so, choose the return reason category in the interface.")
CUSTOMER_REQUEST = "product is not working as expected i want to return it"


def scripted(*steps):
    queue = list(steps)
    log = {"messages": []}

    def chat(system, messages, tools):
        log["messages"].append(messages)
        step = queue.pop(0) if queue else LLMResult(text="MODEL TEXT")
        return step(system, messages) if callable(step) else step

    llm = ScriptedLLM(chat=chat)
    llm.interprets_language = True  # behave like a real model: the engine's real-model paths are active
    return Backend(llm=llm, settings=Settings()), llm


def closes_date(customer: str, order: str, item: str) -> str:
    verdict = repository.check_return_eligibility(customer, order, item, reason_category="standard")
    return verdict["window_closes_at"][:10]


class TestAClosedWindowIsRefusedBeforeAnythingElse:
    def test_the_reported_chat_gets_the_fixed_refusal_not_the_models_return_invitation(self, temp_db):
        backend, llm = scripted(LLMResult(text=PROSE_ORD1003))
        reply = Client(backend, "C102").say(CUSTOMER_REQUEST)
        closes = closes_date("C102", "ORD1003", "IT103")
        assert reply.text == (flow_texts.text("window_closed_before", "en", hours=168, closes=closes)
                              + flow_texts.text("ticket_offer_review", "en"))
        assert "interface" not in reply.text and "category" not in reply.text.lower()
        assert reply.choice is None and reply.pending is None
        assert llm.chat_calls == 1

    def test_no_second_ticket_offer_is_appended_to_the_refusal(self, temp_db):
        backend, _ = scripted(LLMResult(text=PROSE_ORD1003))
        reply = Client(backend, "C102").say(CUSTOMER_REQUEST)
        assert reply.text.lower().count("support ticket") <= 1 and "for this issue" not in reply.text

    def test_the_customers_yes_i_want_to_return_it_gets_the_refusal_again_never_the_interface_instruction(self, temp_db):
        # the third message of the live chat; the model repeated "select the return reason category in the interface"
        backend, llm = scripted(LLMResult(text=PROSE_ORD1003),
                                LLMResult(text="Please select the return reason category in the interface (for example, "
                                               "**defective/not working**, if available)."))
        client = Client(backend, "C102")
        client.say(CUSTOMER_REQUEST)
        again = client.say("yes i want to return it")
        closes = closes_date("C102", "ORD1003", "IT103")
        assert again.text.startswith(flow_texts.text("window_closed_before", "en", hours=168, closes=closes))
        assert "interface" not in again.text and again.choice is None and again.pending is None

    def test_it_also_holds_after_a_lookup_in_the_same_turn(self, temp_db):
        backend, llm = scripted(calls(("get_order_status", {"order_id": "ORD1003"})), LLMResult(text=PROSE_ORD1003))
        reply = Client(backend, "C102").say(CUSTOMER_REQUEST)
        assert reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168,
                                                     closes=closes_date("C102", "ORD1003", "IT103")))

    def test_a_typed_order_id_decides_which_order(self, temp_db):
        backend, _ = scripted(LLMResult(text="Sure, which reason category should I use?"))
        reply = Client(backend, "C102").say("I want to return ORD1003, the headphones stopped working")
        assert reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168,
                                                     closes=closes_date("C102", "ORD1003", "IT103")))

    def test_the_only_delivered_order_is_used_when_nothing_names_one(self, temp_db):
        backend, _ = scripted(LLMResult(text="Please choose the return reason category in the interface."))
        reply = Client(backend, "C102").say(CUSTOMER_REQUEST)  # ORD1003 is C102's only delivered order
        assert reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168,
                                                     closes=closes_date("C102", "ORD1003", "IT103")))

    def test_hinglish_gets_the_hinglish_refusal(self, temp_db):
        backend, _ = scripted(LLMResult(text="Kya aap ORD1003 return karna chahte hain? Category chunein."))
        reply = Client(backend, "C102").say("ORD1003 return karna hai, product kharab hai")
        assert reply.text.startswith(flow_texts.text("window_closed_before", "hinglish", hours=168,
                                                     closes=closes_date("C102", "ORD1003", "IT103")))


class TestAnEligibleOrderStartsTheRealReturnFlow:
    def test_the_models_category_instruction_becomes_the_category_buttons(self, temp_db):
        backend, llm = scripted(LLMResult(text="Please select the return reason category in the interface for ORD1001."))
        reply = Client(backend, "C101").say("it is not working as expected, I want to return it")
        assert reply.choice is not None and "interface" not in reply.text
        assert len(reply.choice.options) >= 3  # the categories and a way to cancel
        assert llm.chat_calls == 1

    def test_a_different_product_named_by_the_customer_is_never_guessed_past(self, temp_db):
        backend, _ = scripted(LLMResult(text="Please select the return reason category in the interface for ORD1001."))
        reply = Client(backend, "C101").say("my charger stopped working, I want to return it")
        assert reply.choice is None  # ORD1001 holds no charger: the model's reply stands, nothing is started by guesswork

    def test_no_reason_in_the_message_asks_for_the_reason_first(self, temp_db):
        backend, _ = scripted(LLMResult(text="Sure, I can help with that return."))
        reply = Client(backend, "C101").say("I want to return it")
        assert reply.choice is None and reply.pending is None and reply.text  # the normal reason question, from code
        assert reply.text != "Sure, I can help with that return."


class TestEverythingElseIsLeftToTheModel:
    def test_a_policy_question_is_not_a_return_request(self, temp_db):
        backend, _ = scripted(LLMResult(text="Our return policy is in the FAQ."))
        reply = Client(backend, "C102").say("what is your return policy?")
        assert reply.text == "Our return policy is in the FAQ."

    def test_a_negated_request_is_not_a_return_request(self, temp_db):
        backend, _ = scripted(LLMResult(text="Understood, nothing will be returned."))
        reply = Client(backend, "C102").say("I don't want to return it")
        assert reply.text == "Understood, nothing will be returned."

    def test_an_eligibility_question_is_answered_by_the_existing_path_not_this_guard(self, temp_db):
        backend, _ = scripted(
            calls(("check_return_eligibility", {"order_id": "ORD1003", "item_id": "IT103"})),
            LLMResult(text="MODEL ANSWER FROM THE TOOL RESULT"))
        reply = Client(backend, "C102").say("can I still return ORD1003?")  # a question: wants_to_return is false
        assert "interface" not in reply.text and reply.choice is None and reply.pending is None

    def test_the_offline_stand_in_is_not_affected(self, temp_db):
        backend = Backend(settings=Settings())  # RuleBasedLLM
        reply = Client(backend, "C102").say("I want to return ORD1003")
        assert reply.status in ("ok", "error")  # the stand-in keeps its own fixed behaviour; the guard needs a real model
