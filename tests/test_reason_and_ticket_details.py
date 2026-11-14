"""Final Phase 1 corrective pass: Objective A (a product name is not a return reason) and Objective B
(the support-ticket offer after a missed 48-hour window collects issue details before any confirmation).

RuleBasedLLM, temporary seeded databases and fixed clocks only; no protected database and no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backend import Backend, Settings
from app.llm.rule_based import RuleBasedLLM
from app.orchestrator import guard
from backend_helpers import Client

UTC = timezone.utc
DELIVERED = datetime(2026, 11, 4, tzinfo=UTC)  # ORD1001 / IT101 earbuds (C101)
ASK_FOR_REASON = "why you want to return"
STILL_WAITING = "still waiting for your reason"
REQUEST = "I want to return item IT101 from order ORD1001"
REQUEST_WITH_REASON = REQUEST + " because it is damaged"
DETAILS = "The left earbud is not working and the case is cracked."


class Spy(RuleBasedLLM):
    def __init__(self) -> None:
        self.proposed: list[tuple[str, dict]] = []

    def chat(self, system, messages, tools, *, max_output_tokens):
        result = super().chat(system, messages, tools, max_output_tokens=max_output_tokens)
        for call in result.tool_calls or ():
            self.proposed.append((call.name, dict(call.arguments)))
        return result


def rows(raw, table):
    return [dict(r) for r in raw.execute(f"SELECT * FROM {table}").fetchall()]


@pytest.fixture
def backend(temp_db):
    return Backend(llm=RuleBasedLLM(), settings=Settings())


@pytest.fixture
def spy():
    return Spy()


# ---------------------------------------------------------------------------
# Objective A: a product name alone is not a reason
# ---------------------------------------------------------------------------

class TestProductNameIsNotAReason:
    @pytest.mark.parametrize("text", ["charger", "the charger", "earbuds", "Wireless Earbuds", "headphones",
                                      "I want to return the charger", "I want to return the earbuds.",
                                      "return my charger please", "I want to return this item"])
    def test_a_product_reference_alone_is_not_an_explanation(self, text):
        words = guard.PRODUCT_WORDS | guard.product_words("Wireless Earbuds") | guard.product_words("65W GaN Charger")
        assert guard.has_explanation(text, ignore_words=words) is False

    @pytest.mark.parametrize("text", ["Broken", "Not working", "Wrong item", "Damaged", "टूटा हुआ", "काम नहीं कर रहा",
                                      "Product kaam nahi kar raha", "Galat item mila hai", "too big",
                                      "the charger is too slow", "charger stopped working"])
    def test_short_genuine_reasons_still_count(self, text):
        words = guard.PRODUCT_WORDS | guard.product_words("Wireless Earbuds") | guard.product_words("65W GaN Charger")
        assert guard.has_explanation(text, ignore_words=words) is True

    def test_without_item_context_the_legacy_behaviour_is_unchanged(self):
        assert guard.has_explanation("charger") is True  # the context-free check never guessed product words

    def test_item_name_words_are_matched_in_the_singular_and_plural(self):
        words = guard.product_words("Wireless Earbuds")
        assert guard.has_explanation("earbud", ignore_words=words) is False
        assert guard.has_explanation("wireless earbuds", ignore_words=words) is False


class TestProductNameConversations:
    @pytest.mark.parametrize("message", ["I want to return the earbuds.", "earbuds"])
    def test_a_product_name_is_asked_about_not_stored(self, backend, raw, message):
        c = Client(backend, "C101")
        c.say("Tell me about ORD1001")
        first = c.say("I want to return item IT101 from order ORD1001")
        assert ASK_FOR_REASON in first.text
        again = c.say(message)
        assert again.choice is None and again.pending is None and rows(raw, "return_requests") == []
        assert "briefly" in again.text or STILL_WAITING in again.text

    def test_the_charger_request_asks_for_a_reason_then_continues(self, backend, spy, raw):
        spy_backend = Backend(llm=spy, settings=Settings())
        c = Client(spy_backend, "C103")
        c.say("ORD1005")
        first = c.say("I want to return the charger.")
        assert [a for n, a in spy.proposed if n == "start_return_request"] == [
            {"order_id": "ORD1005", "item_id": "IT106"}]
        assert ASK_FOR_REASON in first.text and first.choice is None  # no category, no pending
        assert rows(raw, "return_requests") == []
        second = c.say("It is too slow for my laptop")
        assert second.status == "needs_choice"
        pending = c.pick(second, "standard")
        assert pending.status == "needs_confirmation" and "too slow for my laptop" in pending.pending.summary
        c.confirm(pending)
        stored = rows(raw, "return_requests")
        assert len(stored) == 1 and stored[0]["reason"] == "It is too slow for my laptop"
        assert stored[0]["item_id"] == "IT106" and stored[0]["reason_category"] == "standard"

    def test_a_product_name_plus_a_genuine_reason_in_one_message_is_accepted_whole(self, backend, raw):
        c = Client(backend, "C101")
        text = "I want to return item IT101 from order ORD1001 because the earbuds are broken"
        choice = c.say(text)
        assert choice.status == "needs_choice"
        c.confirm(c.pick(choice, "standard"))
        assert rows(raw, "return_requests")[0]["reason"] == text

    @pytest.mark.parametrize("reason", ["Broken", "Not working", "Wrong item", "Damaged", "टूटा हुआ",
                                        "काम नहीं कर रहा", "Product kaam nahi kar raha"])
    def test_short_genuine_reasons_after_the_prompt_are_stored_exactly(self, backend, raw, reason):
        c = Client(backend, "C101")
        assert ASK_FOR_REASON in c.say(REQUEST).text
        choice = c.say(reason)
        assert choice.status == "needs_choice", choice.text
        c.confirm(c.pick(choice, "standard"))
        assert rows(raw, "return_requests")[0]["reason"] == reason

    @pytest.mark.parametrize("junk", ["", "   ", "!!!", "...", "?"])
    def test_empty_and_punctuation_only_input_is_not_a_reason(self, backend, raw, junk):
        c = Client(backend, "C101")
        c.say(REQUEST)
        reply = c.say(junk)
        assert reply.choice is None and reply.pending is None and rows(raw, "return_requests") == []

    @pytest.mark.parametrize("question", ["Where is my order?", "What is your refund policy?",
                                          "मेरा ऑर्डर कहाँ है", "Order kab aayega"])
    def test_an_unrelated_question_is_never_stored_as_the_reason(self, backend, raw, question):
        c = Client(backend, "C101")
        c.say(REQUEST)
        reply = c.say(question)
        assert STILL_WAITING in reply.text and reply.choice is None
        assert rows(raw, "return_requests") == []
        assert c.say("It stopped working").status == "needs_choice"  # the real reason still works afterwards


# ---------------------------------------------------------------------------
# Objective B: the support-ticket offer and its detail collection
# ---------------------------------------------------------------------------

def make_offer(backend, set_now, hours=72, customer="C101", category="damaged"):
    set_now(DELIVERED + timedelta(hours=hours))
    c = Client(backend, customer)
    out = c.pick(c.say(REQUEST_WITH_REASON), category)
    return c, out


class TestTicketOffer:
    @pytest.mark.parametrize("hours, category", [(49, "damaged"), (72, "damaged"), (72, "wrong_product")])
    def test_the_offer_is_a_question_and_creates_nothing(self, backend, raw, set_now, hours, category):
        c, out = make_offer(backend, set_now, hours, category=category)
        assert "48-hour reporting window" in out.text and "support ticket" in out.text
        assert "Would you like to do that?" in out.text
        for word in ("refund", "replacement", "approved", "guarantee"):
            assert word not in out.text.lower()
        assert out.pending is None and rows(raw, "support_tickets") == [] and rows(raw, "return_requests") == []

    def test_the_early_refusal_after_every_window_also_offers_and_it_works(self, backend, raw, set_now):
        set_now(DELIVERED + timedelta(hours=200))
        c = Client(backend, "C101")
        out = c.say(REQUEST)
        assert "Would you like to do that?" in out.text and ASK_FOR_REASON not in out.text
        assert "describe" in c.say("yes please").text.lower()
        assert rows(raw, "support_tickets") == []


class TestTicketDetailCollection:
    def accept(self, backend, set_now, raw, **kw):
        c, _ = make_offer(backend, set_now, **kw)
        asked = c.say("Yes, please.")
        return c, asked

    @pytest.mark.parametrize("answer", ["yes", "Yes, please.", "yeah", "sure", "haan", "I want to raise a ticket"])  # a bare "ok" is ambiguous now (see test_ticket_and_reason_edge_cases.py)
    def test_a_typed_yes_only_starts_detail_collection(self, backend, raw, set_now, answer):
        c, _ = make_offer(backend, set_now)
        asked = c.say(answer)
        assert asked.status == "ok" and asked.pending is None and asked.choice is None
        assert "describe the problem" in asked.text and "earbuds" in asked.text.lower()
        assert "ORD1001" in asked.text and "IT101" in asked.text
        assert rows(raw, "support_tickets") == [] and rows(raw, "return_requests") == []

    @pytest.mark.parametrize("meaningless", ["yes", "ok", "!!!", "...", "", "   ", "please", "the", "earbuds"])
    def test_empty_or_meaningless_detail_is_rejected(self, backend, raw, set_now, meaningless):
        c, asked = self.accept(backend, set_now, raw)
        reply = c.say(meaningless)
        assert reply.pending is None and reply.status != "needs_confirmation"
        assert "describe" in reply.text.lower() or reply.status == "error"  # an empty message is refused upstream
        assert rows(raw, "support_tickets") == []

    def test_an_unrelated_question_is_not_used_as_the_description(self, backend, raw, set_now):
        c, _ = self.accept(backend, set_now, raw)
        reply = c.say("Where is my order?")
        assert reply.pending is None and "describe" in reply.text.lower()
        assert rows(raw, "support_tickets") == []

    def test_meaningful_details_show_a_summary_and_a_confirmation_card(self, backend, raw, set_now):
        c, _ = self.accept(backend, set_now, raw)
        card = c.say(DETAILS)
        assert card.status == "needs_confirmation" and card.pending.kind == "support_ticket"
        assert DETAILS in card.pending.summary and "ORD1001" in card.pending.summary
        assert "Yes, please." not in card.pending.summary  # the acceptance is not the description
        assert rows(raw, "support_tickets") == []

    def test_typed_yes_no_after_the_summary_creates_nothing(self, backend, raw, set_now):
        c, _ = self.accept(backend, set_now, raw)
        card = c.say(DETAILS)
        for typed in ("yes", "Yes, please", "no", "ok", "confirm"):
            again = c.say(typed)
            assert again.status == "needs_confirmation" and again.pending.action_id == card.pending.action_id
        assert rows(raw, "support_tickets") == []

    @pytest.mark.parametrize("answer", ["no", "No thanks", "not now", "nope"])
    def test_declining_the_offer_creates_nothing_and_drops_it(self, backend, raw, set_now, answer):
        c, _ = make_offer(backend, set_now)
        reply = c.say(answer)
        assert reply.pending is None and "won't raise a ticket" in reply.text
        assert rows(raw, "support_tickets") == []

    def test_typed_cancel_during_detail_collection_clears_it(self, backend, raw, set_now):
        c, _ = self.accept(backend, set_now, raw)
        assert "cancelled" in c.say("cancel").text
        assert c.say("yes").pending is None
        assert rows(raw, "support_tickets") == []

    def test_declining_while_describing_clears_it(self, backend, raw, set_now):
        c, _ = self.accept(backend, set_now, raw)
        assert "won't raise a ticket" in c.say("no").text
        assert rows(raw, "support_tickets") == []

    def test_an_unrelated_reply_to_the_offer_drops_it_and_is_handled_normally(self, backend, raw, set_now):
        c, _ = make_offer(backend, set_now)
        reply = c.say("Where is my order ORD1001?")
        assert "ORD1001" in reply.text and reply.pending is None
        assert c.say("yes").status != "needs_confirmation"
        assert rows(raw, "support_tickets") == []


class TestTicketConfirmation:
    def card(self, backend, set_now, raw):
        c, _ = make_offer(backend, set_now)
        c.say("Yes, please.")
        return c, c.say(DETAILS)

    def test_clicking_confirm_creates_exactly_one_ticket_with_the_details(self, backend, raw, set_now):
        c, card = self.card(backend, set_now, raw)
        done = c.confirm(card)
        assert "TKT_" in done.text
        tickets = rows(raw, "support_tickets")
        assert len(tickets) == 1
        assert tickets[0]["customer_id"] == "C101" and tickets[0]["order_id"] == "ORD1001"
        assert tickets[0]["description"] == DETAILS and tickets[0]["issue_type"] == "return"
        assert rows(raw, "return_requests") == []

    def test_a_repeated_confirmation_cannot_create_a_duplicate(self, backend, raw, set_now):
        c, card = self.card(backend, set_now, raw)
        c.confirm(card)
        again = c.confirm(card)
        assert "TKT_" not in again.text and "already" in again.text.lower()
        assert len(rows(raw, "support_tickets")) == 1

    def test_declining_with_the_button_creates_nothing(self, backend, raw, set_now):
        c, card = self.card(backend, set_now, raw)
        c.confirm(card, accepted=False)
        assert rows(raw, "support_tickets") == []

    def test_an_expired_confirmation_cannot_create_a_ticket(self, backend, raw, set_now, bnow):
        c, card = self.card(backend, set_now, raw)
        bnow(minutes=11)
        out = c.confirm(card)
        assert "expired" in out.text and "TKT_" not in out.text
        assert rows(raw, "support_tickets") == []

    def test_an_expired_offer_is_dropped(self, backend, raw, set_now, bnow):
        c, _ = make_offer(backend, set_now)
        bnow(minutes=11)
        reply = c.say("yes")
        assert reply.pending is None and "describe the problem" not in reply.text
        assert rows(raw, "support_tickets") == []

    def test_expired_detail_collection_is_dropped(self, backend, raw, set_now, bnow):
        c, _ = make_offer(backend, set_now)
        c.say("yes")
        bnow(minutes=11)
        reply = c.say(DETAILS)
        assert reply.pending is None or reply.pending.kind != "support_ticket"
        assert rows(raw, "support_tickets") == []


class TestTicketOwnership:
    def test_another_customers_order_never_gets_an_offer_or_a_ticket(self, backend, raw, set_now):
        set_now(DELIVERED + timedelta(hours=72))
        c = Client(backend, "C102")
        out = c.say(REQUEST_WITH_REASON)  # ORD1001 belongs to C101
        assert "couldn't find" in out.text and "Would you like to do that?" not in out.text
        assert "describe the problem" not in c.say("yes").text
        assert rows(raw, "support_tickets") == []

    def test_each_session_has_its_own_offer(self, backend, raw, set_now):
        c1, _ = make_offer(backend, set_now)
        other = Client(backend, "C102")
        reply = other.say("yes")
        assert "describe the problem" not in reply.text and reply.pending is None
        assert "describe the problem" in c1.say("yes").text

    def test_a_ticket_is_linked_only_to_the_offered_owned_order(self, backend, raw, set_now):
        c, _ = make_offer(backend, set_now)
        c.say("yes")
        other = c.say("The left earbud is not working. Also see ORD1003 and ORD1005.")
        assert other.pending is None and "describe" in other.text.lower()  # another order's ID: not a description
        card = c.say("The left earbud is not working.")
        c.confirm(card)
        ticket = rows(raw, "support_tickets")[0]
        assert ticket["order_id"] == "ORD1001" and ticket["customer_id"] == "C101"


class TestOrdinaryTicketFlowsAreUnchanged:
    def test_complaint_then_description_then_confirm(self, backend, raw):
        c = Client(backend, "C101")
        asked = c.say("I want to raise a ticket")
        assert "What is the issue" in asked.text
        card = c.say("My earbuds from ORD1001 stopped working after two days")
        assert card.status == "needs_confirmation" and card.pending.kind == "support_ticket"
        assert rows(raw, "support_tickets") == []
        c.confirm(card)
        assert len(rows(raw, "support_tickets")) == 1

    def test_a_plain_yes_with_no_offer_does_nothing(self, backend, raw):
        reply = Client(backend, "C101").say("yes")
        assert reply.pending is None and rows(raw, "support_tickets") == []
