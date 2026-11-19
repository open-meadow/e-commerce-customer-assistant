"""Edge cases of the prose-return guard: several delivered orders, several items, ownership and tool tracking.

Seed facts used (baseline 2026-11-07): C106 has two delivered orders (ORD1012, window closed; ORD1013, window open); C103's
delivered ORD1005 holds two items and is inside its window; C102's only delivered order ORD1003 is outside its window.
Offline: scripted models, a temporary seeded database, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.backend  # noqa: F401
from app import catalog
from app.db import repository
from app.llm.base import LLMResult
from app.tools import flow_texts
from tests.backend_helpers import Client, calls
from tests.test_return_prose_guard import CUSTOMER_REQUEST, closes_date, scripted

T0 = datetime(2026, 11, 7, tzinfo=timezone.utc)
LISTING_C106 = "ORD1012 (Delivered), ORD1013 (Delivered)"
WHICH_ITEM = "Which item would you like to return? This order has: "


class TestSeveralDeliveredOrdersAreAskedAboutByTheApplication:
    @pytest.mark.parametrize("language,message", [
        ("en", "I want to return it"),
        ("hinglish", "mujhe return karna hai, product kharab hai"),
        ("hi", "मुझे रिटर्न करना है"),
    ])
    def test_the_which_order_question_is_fixed_text_in_the_customers_language(self, temp_db, language, message):
        backend, llm = scripted(LLMResult(text="MODEL CHOSE ORD1013 FOR YOU"))
        reply = Client(backend, "C106").say(message)
        assert reply.text == flow_texts.text("which_order_to_return", language, orders=LISTING_C106)
        assert "MODEL CHOSE" not in reply.text and reply.choice is None and reply.pending is None
        assert llm.chat_calls == 1

    def test_the_question_lists_only_ids_and_statuses(self, temp_db):
        backend, _ = scripted(LLMResult(text="Which one?"))
        reply = Client(backend, "C106").say("I want to return it")
        assert not any(w in reply.text for w in ("₹", "November", "October", "Total", "refund", "Earbuds", "Headphones"))

    def test_the_models_pick_of_one_order_never_decides_for_the_customer(self, temp_db):
        backend, _ = scripted(LLMResult(text="I'll use ORD1013 for you."))
        reply = Client(backend, "C106").say("I want to return it")
        assert reply.text == flow_texts.text("which_order_to_return", "en", orders=LISTING_C106)
        assert reply.choice is None and reply.pending is None

    def test_two_typed_order_ids_are_asked_about_not_picked(self, temp_db):
        backend, _ = scripted(LLMResult(text="Done."))
        reply = Client(backend, "C106").say("I want to return ORD1012 and ORD1013")
        assert reply.text == flow_texts.text("which_order_to_return", "en", orders=LISTING_C106)

    def test_a_typed_id_settles_it_and_the_closed_one_is_refused(self, temp_db):
        backend, _ = scripted(LLMResult(text="Done."))
        reply = Client(backend, "C106").say("I want to return ORD1012, the product is bad")
        assert reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168,
                                                     closes=closes_date("C106", "ORD1012", "IT113")))

    def test_a_typed_open_one_starts_the_real_flow(self, temp_db):
        backend, _ = scripted(LLMResult(text="Done."))
        reply = Client(backend, "C106").say("I want to return ORD1013, it is not working as expected")
        assert reply.choice is not None


class TestTheItemIsNeverGuessed:
    """Order status and the window belong to the order; only an active return belongs to an item."""

    @staticmethod
    def items():
        return repository.get_order_items("C103", "ORD1005")

    def test_eligibility_is_item_specific_only_through_an_active_return(self, temp_db):
        first, second = [i["item_id"] for i in self.items()]
        repository.create_return_request("C103", "ORD1005", first, "bad", reason_category="standard")
        a = repository.check_return_eligibility("C103", "ORD1005", first, reason_category="standard")
        b = repository.check_return_eligibility("C103", "ORD1005", second, reason_category="standard")
        assert a["outcome"] == "active_return_exists" and b["outcome"] == "eligible"  # one order, one window, two verdicts

    def test_an_open_window_with_no_item_named_asks_which_item(self, temp_db):
        backend, _ = scripted(LLMResult(text="Please choose the return reason category in the interface."))
        reply = Client(backend, "C103").say("it is not working as expected, I want to return it")
        assert reply.text.startswith(WHICH_ITEM) and all(i["item_id"] in reply.text for i in self.items())
        assert reply.choice is None and reply.pending is None

    def test_an_active_return_on_one_item_never_becomes_the_answer_for_the_other(self, temp_db):
        first, _ = [i["item_id"] for i in self.items()]
        repository.create_return_request("C103", "ORD1005", first, "bad", reason_category="standard")
        backend, _ = scripted(LLMResult(text="Please choose the category."))
        reply = Client(backend, "C103").say("I want to return it")
        assert reply.text.startswith(WHICH_ITEM) and "already" not in reply.text.lower()

    def test_a_closed_window_is_refused_for_the_whole_order_without_choosing_an_item(self, temp_db, set_now):
        set_now(T0 + timedelta(days=10))
        backend, _ = scripted(LLMResult(text="Please choose the category."))
        reply = Client(backend, "C103").say("I want to return it")
        assert reply.text.startswith("Returns can be requested within 168 hours of delivery") and "Which item" not in reply.text

    def test_a_closed_window_does_not_hide_an_item_that_has_its_own_verdict(self, temp_db, set_now):
        first, _ = [i["item_id"] for i in self.items()]
        repository.create_return_request("C103", "ORD1005", first, "bad", reason_category="standard")
        set_now(T0 + timedelta(days=10))
        backend, _ = scripted(LLMResult(text="Please choose the category."))
        reply = Client(backend, "C103").say("I want to return it")
        # item 1 would say "a return is already open", item 2 "window closed": the item matters, so the customer is asked
        assert reply.text.startswith(WHICH_ITEM)

    def test_naming_the_item_in_words_goes_to_the_normal_flow(self, temp_db):
        item = self.items()[0]
        name = catalog.default_catalog().name_for(item["product_id"])
        backend, _ = scripted(LLMResult(text="Sure."))
        reply = Client(backend, "C103").say(f"the {name} is not working as expected, I want to return it")
        assert reply.choice is not None and item["item_id"] in reply.text

    def test_a_product_the_customer_names_that_the_order_does_not_hold_is_left_to_the_model(self, temp_db):
        # C102 asks about a charger; their delivered ORD1003 holds headphones: nothing is refused or started on its behalf
        backend, _ = scripted(LLMResult(text="Which order is the charger in?"))
        reply = Client(backend, "C101").say("my charger is bad, I want to return it")
        assert reply.text == "Which order is the charger in?"


class TestOwnershipAndToolTracking:
    def test_an_order_id_typed_earlier_that_is_not_the_customers_is_never_read_or_used(self, temp_db, monkeypatch):
        seen = []
        real = repository.get_order_items
        monkeypatch.setattr(repository, "get_order_items", lambda customer, order: (seen.append(order), real(customer, order))[1])
        backend, _ = scripted(LLMResult(text="ORD1005 is not on your account."), LLMResult(text="Which one do you mean?"))
        client = Client(backend, "C102")
        client.say("where is ORD1005?")  # C103's order
        reply = client.say("I want to return it")
        assert reply.text == "Which one do you mean?"  # left to the model
        assert "ORD1005" not in seen  # the guard read nothing about it

    def test_an_order_id_typed_earlier_that_is_the_customers_own_is_used(self, temp_db):
        backend, _ = scripted(LLMResult(text="Here is ORD1003."), LLMResult(text="Sure."))
        client = Client(backend, "C102")
        client.say("tell me about ORD1003")
        reply = client.say("I want to return it")
        assert reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168,
                                                     closes=closes_date("C102", "ORD1003", "IT103")))

    def test_a_foreign_order_id_typed_now_is_not_settled_by_this_guard(self, temp_db):
        backend, _ = scripted(LLMResult(text="I couldn't find ORD1005 on your account."))
        reply = Client(backend, "C102").say("I want to return ORD1005")
        assert "ORD1005" in reply.text and "168 hours" not in reply.text

    def test_a_check_the_model_ran_does_not_exempt_an_explicit_return_request(self, temp_db):
        # the tool is for questions; after a (valid) check the model still wrote "choose the category in the interface"
        backend, _ = scripted(calls(("check_return_eligibility", {"order_id": "ORD1003", "item_id": "IT103"})),
                              LLMResult(text="Please choose the return reason category in the interface."))
        reply = Client(backend, "C102").say(CUSTOMER_REQUEST)
        assert reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168,
                                                     closes=closes_date("C102", "ORD1003", "IT103")))
        assert "interface" not in reply.text

    def test_a_rejected_eligibility_call_does_not_exempt_it_either(self, temp_db):
        backend, _ = scripted(calls(("check_return_eligibility", {"order_id": "NOT-AN-ORDER", "item_id": "X"})),
                              LLMResult(text="Please choose the return reason category in the interface."))
        reply = Client(backend, "C102").say(CUSTOMER_REQUEST)
        assert reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168,
                                                     closes=closes_date("C102", "ORD1003", "IT103")))

    def test_the_guard_reads_only_through_the_signed_in_customers_scope(self, temp_db, monkeypatch):
        customers = []
        for name in ("list_customer_orders", "get_order_items", "check_return_eligibility"):
            real = getattr(repository, name)
            monkeypatch.setattr(repository, name, (lambda real: lambda customer, *a, **k: (customers.append(customer), real(customer, *a, **k))[1])(real))
        backend, _ = scripted(LLMResult(text="Please choose the category."))
        Client(backend, "C102").say(CUSTOMER_REQUEST)
        assert customers and set(customers) == {"C102"}


FALSE_CLAIM = "I have submitted your return request and created a ticket for you."


def writes(raw):
    return (raw.execute("SELECT count(*) FROM return_requests").fetchone()[0],
            raw.execute("SELECT count(*) FROM support_tickets").fetchone()[0])


class TestAFalseSuccessClaimNeverBypassesTheEligibilityDecision:
    def test_a_closed_window_gets_the_fixed_refusal_and_nothing_is_written(self, temp_db, raw):
        backend, _ = scripted(LLMResult(text=FALSE_CLAIM))
        reply = Client(backend, "C102").say("please return my headphones ORD1003, they are not working")
        expected = (flow_texts.text("window_closed_before", "en", hours=168, closes=closes_date("C102", "ORD1003", "IT103"))
                    + flow_texts.text("ticket_offer_review", "en"))
        assert reply.text == expected
        assert "submitted" not in reply.text and "created a ticket" not in reply.text
        assert flow_texts.text("grounding", "en") not in reply.text  # its "I can start a return request" would contradict the refusal
        assert reply.text.count("Would you like to do that?") == 1  # exactly one ticket offer
        assert reply.choice is None and reply.pending is None and writes(raw) == (0, 0)

    def test_a_closed_window_with_no_item_or_order_typed_is_refused_the_same_way(self, temp_db, raw):
        backend, _ = scripted(LLMResult(text=FALSE_CLAIM))
        reply = Client(backend, "C102").say("I want to return it, it is broken")
        assert reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168,
                                                     closes=closes_date("C102", "ORD1003", "IT103")))
        assert reply.text.count("Would you like to do that?") == 1 and writes(raw) == (0, 0)

    def test_an_eligible_order_never_shows_the_false_confirmation_and_writes_only_after_the_customers_confirm(
            self, temp_db, raw):
        backend, _ = scripted(LLMResult(text=FALSE_CLAIM))
        client = Client(backend, "C101")
        first = client.say("please return my earbuds IT101 from ORD1001, they are bad")
        assert first.text.startswith(flow_texts.text("grounding", "en"))  # says nothing has been done
        assert "submitted" not in first.text and "created a ticket for you" not in first.text
        assert first.choice is not None and first.pending is None and writes(raw) == (0, 0)
        card = client.pick(first, "standard")
        assert card.pending is not None and writes(raw) == (0, 0)  # a card to confirm, still nothing saved
        done = client.confirm(card)
        assert writes(raw) == (1, 0) and done.status == "ok"  # one return, written only by the customer's explicit Confirm

    def test_a_declined_card_writes_nothing(self, temp_db, raw):
        backend, _ = scripted(LLMResult(text=FALSE_CLAIM))
        client = Client(backend, "C101")
        card = client.pick(client.say("please return my earbuds IT101 from ORD1001, they are bad"), "standard")
        client.confirm(card, accepted=False)
        assert writes(raw) == (0, 0)

    def test_a_false_claim_with_several_delivered_orders_still_gets_the_which_order_question(self, temp_db, raw):
        backend, _ = scripted(LLMResult(text=FALSE_CLAIM))
        reply = Client(backend, "C106").say("I want to return it")
        assert reply.text == flow_texts.text("which_order_to_return", "en", orders=LISTING_C106)
        assert writes(raw) == (0, 0)


class TestAmbiguityAndProductSelectionNeverUseTheModelsReply:
    @pytest.mark.parametrize("reply_text", [
        "I'll use ORD1012.", "I'll use ORD1013.", "Both ORD1012 and ORD1013 look fine.", "Let me return ORD1005 for you.",
        "Your Power Bank in ORD1013 is returnable.", FALSE_CLAIM, "Sure!"])
    def test_several_delivered_orders_get_the_same_fixed_question_whatever_the_model_says(self, temp_db, reply_text):
        backend, _ = scripted(LLMResult(text=reply_text))
        reply = Client(backend, "C106").say("I want to return it")
        assert reply.text == flow_texts.text("which_order_to_return", "en", orders=LISTING_C106)

    @pytest.mark.parametrize("reply_text", ["Return ORD1005 for you.", "Your headphones in ORD1013.", "Sure!", FALSE_CLAIM])
    def test_the_only_delivered_order_is_used_whatever_the_model_names(self, temp_db, reply_text):
        backend, _ = scripted(LLMResult(text=reply_text))
        reply = Client(backend, "C102").say("I want to return it, it is broken")
        assert reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168,
                                                     closes=closes_date("C102", "ORD1003", "IT103")))

    @pytest.mark.parametrize("reply_text", ["The earbuds in ORD1001 qualify.", FALSE_CLAIM, "Which order is the charger in?"])
    def test_a_product_the_customer_names_decides_not_the_model(self, temp_db, reply_text):
        # C101's charger orders are shipped or processing, so nothing is returnable; whatever the model says about ORD1001
        # (a delivered order that holds no charger) is never taken as the customer's order
        backend, _ = scripted(LLMResult(text=reply_text))
        reply = Client(backend, "C101").say("my charger is bad, I want to return it")
        assert reply.choice is None and reply.pending is None
        assert not reply.text.startswith(flow_texts.text("window_closed_before", "en", hours=168, closes="2026-11-11"))

    @pytest.mark.parametrize("reply_text", ["Return ORD1001 for you.", "Return the monitor, IT105.", "Sure!"])
    def test_the_item_comes_from_the_customers_words_or_a_question_never_from_the_model(self, temp_db, reply_text):
        backend, _ = scripted(LLMResult(text=reply_text))
        reply = Client(backend, "C103").say("it is not working as expected, I want to return it")
        assert reply.text.startswith(WHICH_ITEM) and "IT105" in reply.text and "IT106" in reply.text


class TestNoWriteBeforeTheCustomerConfirms:
    def test_a_model_that_ignores_the_request_still_cannot_make_the_engine_write_before_a_confirm(self, temp_db, raw):
        backend, _ = scripted(LLMResult(text="Hello."))
        client = Client(backend, "C101")
        reply = client.say("I want to return item IT101 from ORD1001. It does not fit me and I would like my money back.")
        assert reply.choice is not None and writes(raw) == (0, 0)
        card = client.pick(reply, "standard")
        assert card.pending is not None and writes(raw) == (0, 0)
        client.confirm(card)
        assert writes(raw) == (1, 0)
