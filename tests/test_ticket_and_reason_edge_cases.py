"""Narrow corrective pass after the read-only audit.

A  A dedicated ticket-detail validator (questions and replacement requests are valid; a different return
   request, another order or item, and unrelated messages are not).
B  A three-way answer to the ticket offer (affirmative, negative, ambiguous); an ambiguous answer keeps the
   offer without extending its expiry; offer state and visible wording always agree.
C  A product word is not a reason, but "no power" is one for a power bank.

RuleBasedLLM, temporary seeded databases and fixed clocks only; no protected database and no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backend import Backend, Settings
from app.db import repository
from app.llm.rule_based import RuleBasedLLM
from app.orchestrator import guard
from backend_helpers import Client

UTC = timezone.utc
DELIVERED = datetime(2026, 11, 4, tzinfo=UTC)  # ORD1001 / IT101 (C101)
REQUEST_WITH_REASON = "I want to return item IT101 from order ORD1001 because it is damaged"
QUESTION = "Would you like to do that?"
EARBUDS = guard.PRODUCT_WORDS | guard.product_words("Wireless Earbuds")
POWER_BANK = guard.PRODUCT_WORDS | guard.product_words("Power Bank 20,000mAh")
HEADPHONES = guard.PRODUCT_WORDS | guard.product_words("Over-Ear Headphones")
MONITOR = guard.PRODUCT_WORDS | guard.product_words("24-inch Monitor (Full HD)")


def rows(raw, table):
    return [dict(r) for r in raw.execute(f"SELECT * FROM {table}").fetchall()]


@pytest.fixture
def backend(temp_db):
    return Backend(llm=RuleBasedLLM(), settings=Settings())


def session_of(backend, client):
    return backend.registry.get(client.state._handle)


def make_offer(backend, set_now, hours=72, category="damaged"):
    set_now(DELIVERED + timedelta(hours=hours))
    c = Client(backend, "C101")
    out = c.pick(c.say(REQUEST_WITH_REASON), category)
    return c, out


def to_details(backend, set_now):
    c, _ = make_offer(backend, set_now)
    assert "describe the problem" in c.say("yes").text
    return c


# ---------------------------------------------------------------------------
# A  Ticket-detail validation
# ---------------------------------------------------------------------------

VALID_DETAILS = [
    "Why is the left earbud not working?",
    "The screen keeps going black.",
    "I want a replacement because this one is defective.",
    "Left earbud is not working",
    "Can you check why the screen goes black?",
    "I tried to return it but the window had closed.",
    "यह काम नहीं कर रहा है",
    "इसमें आवाज़ नहीं आ रही",
    "Product kaam nahi kar raha",
    "Mujhe replacement chahiye kyunki yeh kharab hai",
    "Mujhe refund chahiye",
]

INVALID_DETAILS = [
    ("", "meaningless"), ("   ", "meaningless"), ("!!!", "meaningless"), ("...", "meaningless"), ("?", "meaningless"),
    ("yes", "meaningless"), ("no", "meaningless"), ("ok thanks", "meaningless"), ("please", "meaningless"),
    ("thanks", "meaningless"), ("earbuds", "meaningless"), ("the charger", "meaningless"),
    ("x" * 2001, "too_long"),
    ("I want to return the monitor", "new_return"),
    ("I want to return it because it is broken", "new_return"),
    ("I would like to start a return", "new_return"),
    ("I want to return item IT102", "other_target"),
    ("Problem with ORD1002", "other_target"),
    ("IT102 is broken", "other_target"),
    ("Where is my order?", "unrelated"),
    ("What is your refund policy?", "unrelated"),
    ("Tell me a joke", "unrelated"),
    ("Hello", "meaningless"),
    ("show my orders", "unrelated"),
    ("मेरा ऑर्डर कहाँ है", "unrelated"),
    ("Order kab aayega", "unrelated"),
]


class TestTicketDetailValidator:
    @pytest.mark.parametrize("text", VALID_DETAILS)
    def test_meaningful_descriptions_are_accepted(self, text):
        assert guard.ticket_detail_problem(text, "ORD1001", "IT101", EARBUDS) is None

    @pytest.mark.parametrize("text, code", INVALID_DETAILS)
    def test_other_messages_are_refused_with_a_reason_code(self, text, code):
        assert guard.ticket_detail_problem(text, "ORD1001", "IT101", EARBUDS) == code

    def test_the_offers_own_order_and_item_are_not_another_target(self):
        assert guard.ticket_detail_problem("IT101 on ORD1001 is broken", "ORD1001", "IT101", EARBUDS) is None

    def test_a_question_mark_alone_does_not_invalidate(self):
        assert guard.ticket_detail_problem("Is the left earbud supposed to be this quiet?", "ORD1001", "IT101",
                                           EARBUDS) is None


class TestTicketDetailConversations:
    @pytest.mark.parametrize("text", ["Why is the left earbud not working?", "The screen keeps going black.",
                                      "I want a replacement because this one is defective.",
                                      "यह काम नहीं कर रहा है", "Product kaam nahi kar raha"])
    def test_a_valid_description_becomes_the_card_and_the_ticket_exactly(self, backend, raw, set_now, text):
        c = to_details(backend, set_now)
        card = c.say(text)
        assert card.status == "needs_confirmation" and card.pending.kind == "support_ticket"
        assert text in card.pending.summary and rows(raw, "support_tickets") == []
        c.confirm(card)
        ticket = rows(raw, "support_tickets")[0]
        assert ticket["description"] == text and ticket["order_id"] == "ORD1001"

    @pytest.mark.parametrize("text, expected", [
        ("I want to return it because it is broken", "different return request"),
        ("I want to return the monitor", "different return request"),
        ("Problem with ORD1002", "another order or item"),
        ("IT102 is broken", "another order or item"),
    ])
    def test_another_return_or_target_is_clarified_not_used(self, backend, raw, set_now, text, expected):
        c = to_details(backend, set_now)
        offer_before = dict(session_of(backend, c).ticket_offer)
        reply = c.say(text)
        assert reply.pending is None and expected in reply.text and "describe the problem" in reply.text
        assert rows(raw, "support_tickets") == []
        offer = session_of(backend, c).ticket_offer
        assert offer["stage"] == "details" and offer["expires"] == offer_before["expires"]  # kept, not extended
        assert c.say("The left earbud is not working.").status == "needs_confirmation"

    @pytest.mark.parametrize("text", ["Where is my order?", "What is your refund policy?", "Hello",
                                      "मेरा ऑर्डर कहाँ है", "!!!", "ok thanks"])
    def test_unrelated_meaningless_or_overlong_text_is_not_used(self, backend, raw, set_now, text):
        c = to_details(backend, set_now)
        reply = c.say(text)
        assert reply.pending is None and "describe the problem" in reply.text
        assert rows(raw, "support_tickets") == []
        assert session_of(backend, c).ticket_offer["stage"] == "details"

    def test_an_overlong_message_is_refused_upstream_and_the_offer_is_untouched(self, backend, raw, set_now):
        """The API's own message limit equals the ticket limit, so the engine's too_long branch is a backstop."""
        c = to_details(backend, set_now)
        before = dict(session_of(backend, c).ticket_offer)
        reply = c.say("x" * 2001)
        assert reply.status == "error" and reply.pending is None and rows(raw, "support_tickets") == []
        assert session_of(backend, c).ticket_offer == before

    def test_confirmation_and_expiry_protections_still_apply(self, backend, raw, set_now, bnow):
        c = to_details(backend, set_now)
        card = c.say("Why is the left earbud not working?")
        bnow(minutes=11)
        assert "expired" in c.confirm(card).text and rows(raw, "support_tickets") == []


# ---------------------------------------------------------------------------
# B  Three-way offer answer
# ---------------------------------------------------------------------------

AFFIRMATIVE = ["yes", "Yes", "yes please", "Yes, please.", "yeah", "yep", "sure", "haan", "हाँ", "जी हाँ",
               "ji haan", "go ahead", "I want to raise a ticket"]
NEGATIVE = ["no", "No thanks", "no, thank you", "nope", "nah", "not now", "नहीं", "nahi", "I don't want a ticket"]
AMBIGUOUS = ["ok thanks", "ok", "okay", "please", "thanks", "maybe", "hmm", "what", "Where is my order?",
             "yes no", "no yes", "", "   ", "!!!", "I don't know", "ok, please"]


class TestOfferAnswerClassification:
    @pytest.mark.parametrize("text", AFFIRMATIVE)
    def test_affirmative(self, text):
        assert guard.classify_offer_reply(text) == "affirmative"

    @pytest.mark.parametrize("text", NEGATIVE)
    def test_negative(self, text):
        assert guard.classify_offer_reply(text) == "negative"

    @pytest.mark.parametrize("text", AMBIGUOUS)
    def test_ambiguous(self, text):
        assert guard.classify_offer_reply(text) == "ambiguous"


class TestOfferConversations:
    @pytest.mark.parametrize("text", ["ok thanks", "please", "ok", "maybe", "hmm", "!!!"])
    def test_an_ambiguous_answer_keeps_the_offer_and_asks_for_yes_or_no(self, backend, raw, set_now, text):
        c, _ = make_offer(backend, set_now)
        before = dict(session_of(backend, c).ticket_offer)
        reply = c.say(text)
        assert reply.pending is None and "yes or no" in reply.text and "support ticket" in reply.text
        assert "describe the problem" not in reply.text and rows(raw, "support_tickets") == []
        after = session_of(backend, c).ticket_offer
        assert after["stage"] == "offered" and after["expires"] == before["expires"]
        assert "describe the problem" in c.say("yes").text  # still answerable

    @pytest.mark.parametrize("text", ["Where is my order?", "What is your refund policy?", "show my orders",
                                      "Where is ORD1001", "I would like to know more about how the whole thing works"])
    def test_a_plainly_new_request_drops_the_offer_and_is_handled_normally(self, backend, raw, set_now, text):
        c, _ = make_offer(backend, set_now)
        reply = c.say(text)
        assert "yes or no" not in reply.text and "describe the problem" not in reply.text
        assert session_of(backend, c).ticket_offer is None and rows(raw, "support_tickets") == []

    def test_a_new_return_request_after_the_refusal_is_not_swallowed_by_the_offer(self, backend, raw, set_now):
        c, _ = make_offer(backend, set_now)
        choice = c.say(REQUEST_WITH_REASON)
        assert choice.status == "needs_choice" and session_of(backend, c).ticket_offer is None

    def test_an_ambiguous_answer_does_not_extend_the_expiry(self, backend, raw, set_now, bnow):
        c, _ = make_offer(backend, set_now)
        bnow(minutes=6)
        assert "yes or no" in c.say("hmm").text
        bnow(minutes=5)  # 11 minutes after the offer, 5 after the ambiguous answer
        reply = c.say("yes")
        assert "describe the problem" not in reply.text and session_of(backend, c).ticket_offer is None
        assert rows(raw, "support_tickets") == []

    @pytest.mark.parametrize("text", NEGATIVE[:5])
    def test_a_negative_answer_clears_the_offer(self, backend, raw, set_now, text):
        c, _ = make_offer(backend, set_now)
        reply = c.say(text)
        assert "won't raise a ticket" in reply.text and session_of(backend, c).ticket_offer is None
        assert rows(raw, "support_tickets") == []

    def test_typed_cancel_clears_the_offer(self, backend, raw, set_now):
        c, _ = make_offer(backend, set_now)
        assert "cancelled" in c.say("cancel").text and session_of(backend, c).ticket_offer is None

    def test_an_affirmative_answer_still_checks_capacity_and_ownership(self, backend, raw, set_now):
        c, _ = make_offer(backend, set_now)
        session = session_of(backend, c)
        session.committed["support_ticket"] = Settings().cap_for("support_ticket")
        reply = c.say("yes")
        assert "limit of support tickets" in reply.text and session.ticket_offer is None


class TestOfferStateAndWordingAgree:
    def check(self, backend, client, text, offered):
        offer = session_of(backend, client).ticket_offer
        assert (QUESTION in text) is offered, text
        assert (offer is not None) is offered, text
        if offered:
            assert "support ticket" in text.lower() and offer["order_id"] == "ORD1001" and offer["item_id"] == "IT101"

    def test_early_refusal_after_every_window(self, backend, set_now):
        set_now(DELIVERED + timedelta(hours=200))
        c = Client(backend, "C101")
        self.check(backend, c, c.say(REQUEST_WITH_REASON).text, True)

    @pytest.mark.parametrize("hours, category", [(49, "damaged"), (72, "wrong_product")])
    def test_damaged_or_wrong_product_after_48_hours(self, backend, set_now, hours, category):
        c, out = make_offer(backend, set_now, hours, category)
        self.check(backend, c, out.text, True)

    def test_both_windows_closed_between_request_and_pick(self, backend, set_now):
        set_now(DELIVERED + timedelta(hours=168))
        c = Client(backend, "C101")
        choice = c.say(REQUEST_WITH_REASON)
        set_now(DELIVERED + timedelta(hours=169))
        self.check(backend, c, c.pick(choice, "damaged").text, True)

    def test_standard_window_closed_between_request_and_pick(self, backend, set_now):
        set_now(DELIVERED + timedelta(hours=168))
        c = Client(backend, "C101")
        choice = c.say(REQUEST_WITH_REASON)
        set_now(DELIVERED + timedelta(hours=169))
        self.check(backend, c, c.pick(choice, "standard").text, True)

    def test_window_closing_before_confirmation(self, backend, set_now):
        set_now(DELIVERED + timedelta(hours=47))
        c = Client(backend, "C101")
        pending = c.pick(c.say(REQUEST_WITH_REASON), "damaged")
        set_now(DELIVERED + timedelta(hours=49))
        self.check(backend, c, c.confirm(pending).text, True)

    def test_standard_read_failing_falls_back_to_an_offer_with_a_question(self, backend, set_now, monkeypatch):
        from app.db.errors import DatabaseOperationalError

        real, calls = repository.check_return_eligibility, []

        def flaky(customer_id, order_id, item_id, *, reason_category):
            calls.append(reason_category)
            if len(calls) == 3:
                raise DatabaseOperationalError("down")
            return real(customer_id, order_id, item_id, reason_category=reason_category)

        monkeypatch.setattr(repository, "check_return_eligibility", flaky)
        c, out = make_offer(backend, set_now)
        assert calls == ["standard", "damaged", "standard"]
        self.check(backend, c, out.text, True)

    def test_standard_verdict_missing_its_window_falls_back_to_an_offer_with_a_question(
            self, backend, set_now, monkeypatch):
        real, calls = repository.check_return_eligibility, []

        def incomplete(customer_id, order_id, item_id, *, reason_category):
            calls.append(reason_category)
            verdict = dict(real(customer_id, order_id, item_id, reason_category=reason_category))
            if len(calls) == 3:
                verdict["window_hours"] = None
            return verdict

        monkeypatch.setattr(repository, "check_return_eligibility", incomplete)
        c, out = make_offer(backend, set_now)
        self.check(backend, c, out.text, True)

    def test_standard_verdict_of_another_kind_still_offers_with_a_question(self, backend, set_now, monkeypatch):
        real, calls = repository.check_return_eligibility, []

        def other(customer_id, order_id, item_id, *, reason_category):
            calls.append(reason_category)
            verdict = dict(real(customer_id, order_id, item_id, reason_category=reason_category))
            if len(calls) == 3:
                verdict = {"outcome": "active_return_exists", "window_hours": 168,
                           "window_closes_at": "2026-11-11T00:00:00Z"}
            return verdict

        monkeypatch.setattr(repository, "check_return_eligibility", other)
        c, out = make_offer(backend, set_now)
        self.check(backend, c, out.text, True)

    def test_an_active_return_is_refused_without_an_offer(self, backend, set_now):
        repository.create_return_request("C101", "ORD1001", "IT101", "elsewhere", reason_category="standard")
        c = Client(backend, "C101")
        self.check(backend, c, c.say(REQUEST_WITH_REASON).text, False)

    def test_an_order_that_is_not_delivered_is_refused_without_an_offer(self, backend):
        c = Client(backend, "C101")
        out = c.say("I want to return item IT102 from order ORD1002 because it is damaged")
        assert QUESTION not in out.text and session_of(backend, c).ticket_offer is None

    def test_a_missing_delivery_date_is_refused_without_an_offer(self, backend, monkeypatch):
        def missing(customer_id, order_id, item_id, *, reason_category):
            return {"outcome": "delivery_timestamp_missing"}

        monkeypatch.setattr(repository, "check_return_eligibility", missing)
        c = Client(backend, "C101")
        out = c.say(REQUEST_WITH_REASON)
        assert QUESTION not in out.text and session_of(backend, c).ticket_offer is None

    def test_another_customers_item_is_refused_without_an_offer(self, backend, set_now):
        set_now(DELIVERED + timedelta(hours=200))
        c = Client(backend, "C102")
        out = c.say(REQUEST_WITH_REASON)
        assert QUESTION not in out.text and session_of(backend, c).ticket_offer is None


# ---------------------------------------------------------------------------
# C  Product words and real reasons
# ---------------------------------------------------------------------------

class TestProductWordOverlap:
    @pytest.mark.parametrize("text", ["no power", "No power", "The power bank is not charging", "no charging",
                                      "power bank gets very hot", "not holding charge", "the power bank is dead"])
    def test_genuine_power_bank_reasons_are_accepted(self, text):
        assert guard.has_explanation(text, POWER_BANK) is True

    @pytest.mark.parametrize("text", ["power bank", "the power bank", "I want to return the power bank", "power",
                                      "20,000mAh", "I want to return the charger", "no", "no thanks", "no please",
                                      "no, I want to return the charger", "no the charger"])
    def test_a_product_reference_or_a_bare_no_is_still_not_a_reason(self, text):
        assert guard.has_explanation(text, POWER_BANK) is False

    @pytest.mark.parametrize("text", ["no sound in the left ear", "ear pain", "no sound", "left side has no sound"])
    def test_genuine_headphone_reasons_are_accepted(self, text):
        assert guard.has_explanation(text, HEADPHONES) is True

    @pytest.mark.parametrize("text", ["Over-Ear Headphones", "headphones", "I want to return the headphones",
                                      "over ear"])
    def test_headphone_references_are_not_reasons(self, text):
        assert guard.has_explanation(text, HEADPHONES) is False

    @pytest.mark.parametrize("text", ["no display", "no signal", "not full hd", "no monitor signal"])
    def test_genuine_monitor_reasons_are_accepted(self, text):
        assert guard.has_explanation(text, MONITOR) is True

    @pytest.mark.parametrize("text", ["24-inch Monitor", "the monitor", "full hd monitor"])
    def test_monitor_references_are_not_reasons(self, text):
        assert guard.has_explanation(text, MONITOR) is False

    @pytest.mark.parametrize("text", ["no wireless connection", "not wireless", "no earbuds in the box"])
    def test_earbud_reasons_with_overlapping_words_are_accepted(self, text):
        assert guard.has_explanation(text, EARBUDS) is True

    def test_the_charger_request_is_still_rejected_for_every_item(self):
        for words in (EARBUDS, POWER_BANK, HEADPHONES, MONITOR):
            assert guard.has_explanation("I want to return the charger", words) is False

    def test_no_power_for_a_power_bank_is_stored_exactly(self, backend, raw):
        c = Client(backend, "C106")
        asked = c.say("I want to return item IT114 from order ORD1013")
        assert "why you want to return" in asked.text
        choice = c.say("no power")
        assert choice.status == "needs_choice", choice.text
        c.confirm(c.pick(choice, "standard"))
        assert rows(raw, "return_requests")[0]["reason"] == "no power"

    def test_a_power_bank_reference_alone_is_still_asked_about(self, backend, raw):
        c = Client(backend, "C106")
        c.say("I want to return item IT114 from order ORD1013")
        reply = c.say("the power bank")
        assert reply.choice is None and reply.pending is None and rows(raw, "return_requests") == []
