"""Phase 1 corrective pass: M1, M2/M3, T3, W1, G1, G2, G3, T1 and T2.

M1       A negated return clause about one item never cancels a separate affirmative request about another;
         the item is identified by name only when that is reliable, otherwise the customer is asked.
M2/M3    An opening question word or exclamation does not override a later affirmative request; "I can't wait
         to return it" is a request; a curly apostrophe negates.
T3       Off-topic Hindi and Hinglish messages are not accepted as a return reason.
W1       A damaged or wrong-product report after its 48-hour window gets an honest refusal and a support-ticket
         offer, never an automatic conversion, an instruction to pick another reason, or a promise.
G1/G2    Eligibility is re-validated when state changes between steps and again at confirmation.
G3       Error and fallback branches around the new checks.
T1/T2    Short meaningful Devanagari reasons count; apostrophe-only input does not.

RuleBasedLLM, temporary seeded databases and fixed clocks only; no protected database and no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backend import Backend, Settings
from app.db import repository
from app.db.errors import DatabaseOperationalError
from app.llm import rule_based
from app.llm.rule_based import RuleBasedLLM
from app.orchestrator import guard
from backend_helpers import Client, ScriptedLLM

UTC = timezone.utc
DELIVERED = datetime(2026, 11, 4, tzinfo=UTC)  # ORD1001 / IT101 (C101)
ASK_FOR_ORDER = "Which order would you like to return? Please share the order ID."
STILL_WAITING = "still waiting for your reason"
ASK_FOR_REASON = "why you want to return"


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
def spy():
    return Spy()


@pytest.fixture
def make_client(temp_db, spy):
    def build(customer_id="C101", discussed=None):
        c = Client(Backend(llm=spy, settings=Settings()), customer_id)
        if discussed:
            c.say(discussed)
        spy.proposed.clear()
        return c
    return build


@pytest.fixture
def backend(temp_db):
    return Backend(llm=ScriptedLLM(), settings=Settings())


def started(spy):
    return [args for name, args in spy.proposed if name == "start_return_request"]


def names(spy):
    return [name for name, _ in spy.proposed]


# ---------------------------------------------------------------------------
# M1  Mixed clauses
# ---------------------------------------------------------------------------

class TestClauseClassification:
    @pytest.mark.parametrize("text, expected", [
        ("i don't want to return the earbuds, i want to return the charger", ["negated", "request"]),
        ("i don't want to return this item, but i want to return my headphones", ["negated", "request"]),
        ("i don't want to return anything", ["negated"]),
        ("i don't want to return the earbuds", ["negated"]),
        ("i want to return the charger", ["request"]),
        ("i don't like the colour, i want to return it", ["request"]),
        ("i don't want to return the monitor, how many days do i have to return the charger?",
         ["negated", "question"]),
        ("how long do i have to return it? i want to return it", ["question", "request"]),
        ("i do not like it and i want to return it", ["request"]),
        ("i want to return the earbuds and the charger", ["request"]),
    ])
    def test_each_clause_gets_its_own_intent(self, text, expected):
        assert [intent for intent, _ in rule_based.return_clauses(text)] == expected

    def test_a_request_beats_a_negation_and_a_question_only_when_it_is_a_request(self):
        assert rule_based.return_intent("i don't want to return the monitor, i want to return the charger") == "request"
        assert rule_based.return_intent("i don't want to return the monitor, how do i return the charger?") == "question"
        assert rule_based.return_intent("i don't want to return the monitor, i want my money back") == "negated"


class TestMixedClauseConversations:
    def test_the_negated_item_is_never_chosen_and_the_requested_one_is(self, make_client, spy):
        c = make_client("C103", discussed="ORD1005")  # IT105 monitor, IT106 charger
        reply = c.say("I don't want to return the monitor, I want to return the charger.")
        assert started(spy) == [{"order_id": "ORD1005", "item_id": "IT106"}]
        assert reply.status in ("needs_choice", "ok") and "IT105" not in reply.text

    def test_the_spec_example_on_an_order_without_a_charger_asks_instead_of_guessing(self, make_client, spy, raw):
        c = make_client("C101", discussed="ORD1001")  # one item: the earbuds the customer does not want to return
        reply = c.say("I don't want to return the earbuds, I want to return the charger.")
        assert started(spy) == []  # the earbuds are never chosen for them
        assert "Which item would you like to return" in reply.text and "IT101" in reply.text
        assert reply.choice is None and reply.pending is None and rows(raw, "return_requests") == []

    def test_without_an_order_the_customer_is_asked_for_it(self, make_client, spy):
        reply = make_client().say("I don't want to return the earbuds, I want to return the charger.")
        assert reply.text == ASK_FOR_ORDER and spy.proposed == []

    def test_a_negation_then_a_but_clause_naming_headphones(self, make_client, spy):
        c = make_client("C104", discussed="ORD1009")  # IT110 Over-Ear Headphones
        c.say("I don't want to return this item, but I want to return my headphones.")
        assert started(spy) == [{"order_id": "ORD1009", "item_id": "IT110"}]

    def test_nothing_to_return(self, make_client, spy, raw):
        c = make_client("C103", discussed="ORD1005")
        reply = c.say("I don't want to return anything.")
        assert spy.proposed == [] and reply.text.startswith("Okay, I won't start a return")
        assert reply.choice is None and reply.pending is None and rows(raw, "return_requests") == []

    def test_a_single_negated_item_request_is_not_a_return(self, make_client, spy, raw):
        c = make_client("C103", discussed="ORD1005")
        reply = c.say("I don't want to return the earbuds.")
        assert started(spy) == [] and reply.text.startswith("Okay, I won't start a return")
        assert rows(raw, "return_requests") == []

    def test_an_affirmative_item_request_alone_starts_the_return_for_that_item(self, make_client, spy):
        c = make_client("C103", discussed="ORD1005")
        c.say("I want to return the charger.")
        assert started(spy) == [{"order_id": "ORD1005", "item_id": "IT106"}]

    def test_an_affirmative_item_request_with_no_order_asks_for_the_order(self, make_client, spy):
        assert make_client().say("I want to return the charger.").text == ASK_FOR_ORDER

    def test_a_negation_about_the_colour_does_not_block_the_request(self, make_client, spy):
        c = make_client("C101", discussed="ORD1001")
        c.say("I don't like the colour, I want to return it.")
        assert started(spy) == [{"order_id": "ORD1001", "item_id": "IT101"}]

    def test_a_second_clause_that_is_a_question_is_policy_not_a_return(self, make_client, spy, raw):
        c = make_client("C103", discussed="ORD1005")
        reply = c.say("I don't want to return the monitor, how many days do I have to return the charger?")
        assert names(spy)[:1] == ["search_policies"] and started(spy) == []
        assert reply.choice is None and reply.pending is None and rows(raw, "return_requests") == []

    @pytest.mark.parametrize("text", ["I don't want to return the monitor, I want to return it.",
                                      "I want to return the laptop.",
                                      "I want to return the monitor and the charger.",
                                      "I want to return it."])
    def test_an_item_that_cannot_be_identified_is_asked_about_never_guessed(self, make_client, spy, text):
        c = make_client("C103", discussed="ORD1005")  # two items
        reply = c.say(text)
        assert started(spy) == []
        assert "Which item would you like to return" in reply.text
        assert "IT105" in reply.text and "IT106" in reply.text

    def test_a_single_item_order_with_a_generic_request_still_starts_for_that_item(self, make_client, spy):
        c = make_client("C101", discussed="ORD1001")
        c.say("I want to return this item.")
        assert started(spy) == [{"order_id": "ORD1001", "item_id": "IT101"}]

    def test_an_order_id_in_the_message_still_selects_by_name(self, make_client, spy):
        c = make_client()
        c.say("I don't want to return the monitor, I want to return the charger from ORD1005")
        assert started(spy) == []  # ORD1005 belongs to C103: the lookup is refused, nothing starts
        assert "get_order_status" in names(spy)

    def test_the_category_is_still_the_customers_choice(self, make_client, raw):
        c = make_client("C103", discussed="ORD1005")
        reply = c.say("I don't want to return the monitor, I want to return the charger because it is slow")
        assert reply.status == "needs_choice" and reply.choice is not None  # buttons: never inferred
        assert rows(raw, "return_requests") == []


# ---------------------------------------------------------------------------
# M2 / M3  Openers and idioms
# ---------------------------------------------------------------------------

AFFIRMATIVE = ["When I opened the package it was broken, so I want to return it.",
               "How disappointing! I want to return it.",
               "What a waste, I want to return it.",
               "Why? I want to return it.",
               "How long do I have to return it? I want to return it.",
               "I can't wait to return it.", "I can’t wait to return it.",
               "I want to return it.", "I want to return this item because it is damaged."]
NEGATED = ["I don’t want to return it.", "I don't want to return it.", "I do not want to return this."]
POLICY = ["How many days do I have to return this?", "Can I return it?", "What is the return window?",
          "Why can't I return it?", "How do I start a return?"]


class TestOpenersAndIdioms:
    @pytest.mark.parametrize("text", AFFIRMATIVE)
    def test_a_genuine_request_still_reaches_the_return_workflow(self, make_client, spy, text):
        c = make_client("C101", discussed="ORD1001")
        c.say(text)
        assert started(spy) == [{"order_id": "ORD1001", "item_id": "IT101"}], text

    @pytest.mark.parametrize("text", NEGATED)
    def test_a_negated_request_does_not_start_a_return(self, make_client, spy, text):
        c = make_client("C101", discussed="ORD1001")
        reply = c.say(text)
        assert started(spy) == [] and reply.text.startswith("Okay, I won't start a return")

    @pytest.mark.parametrize("text", POLICY)
    def test_a_policy_question_does_not_start_a_return(self, make_client, spy, text):
        c = make_client("C101", discussed="ORD1001")
        c.say(text)
        assert started(spy) == [] and "get_order_status" not in names(spy)

    @pytest.mark.parametrize("text", AFFIRMATIVE)
    def test_without_an_earlier_order_a_genuine_request_asks_for_it(self, make_client, text):
        assert make_client().say(text).text.startswith(ASK_FOR_ORDER)


# ---------------------------------------------------------------------------
# T3 / T1 / T2  Reasons in Hindi and Hinglish
# ---------------------------------------------------------------------------

HINDI_OFF_TOPIC = ["मेरा ऑर्डर कहाँ है", "क्या मैं रिफंड ले सकता हूँ", "मुझे मेरा पैसा वापस चाहिए", "ऑर्डर कब आएगा"]
HINGLISH_OFF_TOPIC = ["Mera order kaha hai", "Mujhe refund chahiye", "Order kab aayega", "kya main refund le sakta hoon"]
HINDI_REASONS = ["यह काम नहीं कर रहा है", "मुझे गलत सामान मिला है", "इसमें आवाज़ नहीं आ रही", "यह टूटा हुआ आया",
                 "टूटा हुआ", "खराब है", "ठीक नहीं", "मुझे पैसे वापस चाहिए क्योंकि यह खराब है"]
HINGLISH_REASONS = ["Product kaam nahi kar raha", "Galat item mila hai", "yeh kharab hai", "size chhota hai"]


class TestOffTopicReasonDetection:
    @pytest.mark.parametrize("text", HINDI_OFF_TOPIC + HINGLISH_OFF_TOPIC)
    def test_off_topic_text_is_not_a_reason(self, text):
        assert guard.looks_unrelated_to_a_reason(text, "ORD1001", "IT101") is True

    @pytest.mark.parametrize("text", HINDI_REASONS + HINGLISH_REASONS)
    def test_valid_reasons_are_not_flagged(self, text):
        assert guard.looks_unrelated_to_a_reason(text, "ORD1001", "IT101") is False

    @pytest.mark.parametrize("text", ["It stopped working after two days", "The colour is too bright",
                                      "I changed my mind", "wrong size"])
    def test_english_reasons_are_unchanged(self, text):
        assert guard.looks_unrelated_to_a_reason(text, "ORD1001", "IT101") is False

    @pytest.mark.parametrize("text", ["Where is my order?", "What is your refund policy?", "show my orders"])
    def test_english_off_topic_text_is_unchanged(self, text):
        assert guard.looks_unrelated_to_a_reason(text, "ORD1001", "IT101") is True


class TestOffTopicReasonConversations:
    @pytest.mark.parametrize("text", HINDI_OFF_TOPIC + HINGLISH_OFF_TOPIC)
    def test_the_customer_is_asked_again_and_nothing_is_stored(self, backend, raw, text):
        c = Client(backend, "C101")
        assert ASK_FOR_REASON in c.say("I want to return item IT101 from order ORD1001").text
        reply = c.say(text)
        assert STILL_WAITING in reply.text and reply.choice is None and reply.pending is None
        assert rows(raw, "return_requests") == []

    @pytest.mark.parametrize("reason", HINDI_REASONS + HINGLISH_REASONS)
    def test_a_valid_short_reason_is_accepted_and_stored_exactly(self, backend, raw, reason):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        choice = c.say(reason)
        assert choice.status == "needs_choice", choice.text
        pending = c.pick(choice, "standard")
        assert pending.status == "needs_confirmation" and reason in pending.pending.summary
        assert "RET_" in c.confirm(pending).text
        assert rows(raw, "return_requests")[0]["reason"] == reason  # never translated or rewritten

    def test_after_an_off_topic_message_a_real_reason_is_still_accepted(self, backend, raw):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        assert STILL_WAITING in c.say("मेरा ऑर्डर कहाँ है").text
        assert c.say("यह काम नहीं कर रहा है").status == "needs_choice"

    @pytest.mark.parametrize("junk", ["!!!", "...", "।", "", "   ", "please", "I want to return it", "yes"])
    def test_blank_punctuation_and_request_words_are_not_reasons(self, backend, raw, junk):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        reply = c.say(junk)
        assert reply.choice is None and reply.pending is None and rows(raw, "return_requests") == []


class TestShortTokensAndApostrophes:
    @pytest.mark.parametrize("reason", ["टूटा हुआ", "खराब है", "ठीक नहीं", "बेकार", "टूटा", "खराब", "नहीं चल रहा"])
    def test_short_meaningful_devanagari_reasons_count(self, reason):
        assert guard.has_explanation(reason) is True

    @pytest.mark.parametrize("reason", ["का", "की", "है", "क्", "क", "ं", "ाा"])
    def test_a_single_letter_with_its_vowel_sign_is_not_a_reason(self, reason):
        assert guard.has_explanation(reason) is False

    @pytest.mark.parametrize("reason", ["''", "'''", "'", "a'", "' '", "''' '''"])
    def test_apostrophe_only_input_is_not_an_explanation(self, reason):
        assert guard.has_explanation(reason) is False

    @pytest.mark.parametrize("reason", ["don't", "it's broken", "'tis", "i'd", "o'clock"])
    def test_words_with_apostrophes_still_count(self, reason):
        assert guard.has_explanation(reason) is True


# ---------------------------------------------------------------------------
# W1  Damaged / wrong product after the 48-hour reporting window
# ---------------------------------------------------------------------------

REQUEST_TEXT = "I want to return item IT101 from order ORD1001 because it is {problem}"


def at_hours(backend, set_now, hours, customer="C101"):
    set_now(DELIVERED + timedelta(hours=hours))
    return Client(backend, customer)


def refusal_for(c, category, problem="damaged"):
    return c.pick(c.say(REQUEST_TEXT.format(problem=problem)), category)


def assert_offers_a_ticket_without_promises(text):
    assert "support ticket" in text.lower()
    lowered = text.lower()
    for forbidden in ("refund", "replacement", "approved", "approve", "guarantee", "we will", "standard option",
                      "another reason", "choose", "select a different"):
        assert forbidden not in lowered, forbidden
    assert "can't promise" in lowered or "cannot promise" in lowered or "ticket is a request" in lowered


class TestDamagedAfterTheReportingWindow:
    @pytest.mark.parametrize("hours, category", [(49, "damaged"), (72, "damaged"), (72, "wrong_product"),
                                                 (167, "damaged"), (168, "wrong_product")])
    def test_the_reporting_window_has_passed_and_a_ticket_is_offered(self, backend, raw, set_now, hours, category):
        c = at_hours(backend, set_now, hours)
        out = refusal_for(c, category, "damaged" if category == "damaged" else "the wrong colour")
        assert out.status == "ok" and out.pending is None and out.choice is None
        assert "48-hour reporting window" in out.text and "2026-11-06" in out.text
        assert "can't start" in out.text
        assert_offers_a_ticket_without_promises(out.text)
        assert rows(raw, "return_requests") == [] and rows(raw, "support_tickets") == []

    @pytest.mark.parametrize("category", ["damaged", "wrong_product"])
    def test_after_168_hours_both_windows_are_reported_and_a_ticket_is_still_offered(
            self, backend, raw, set_now, category):
        c = at_hours(backend, set_now, 168)
        choice = c.say(REQUEST_TEXT.format(problem="damaged"))
        set_now(DELIVERED + timedelta(hours=169))
        out = c.pick(choice, category)
        assert "48-hour" in out.text and "168-hour" in out.text and "2026-11-11" in out.text
        assert "so I can't start a return" in out.text
        assert_offers_a_ticket_without_promises(out.text)
        assert rows(raw, "return_requests") == [] and out.pending is None

    @pytest.mark.parametrize("hours", [49, 72, 167, 168])
    def test_a_standard_return_is_unaffected_and_eligible(self, backend, set_now, hours):
        c = at_hours(backend, set_now, hours)
        assert refusal_for(c, "standard").status == "needs_confirmation"

    def test_a_standard_return_after_168_hours_is_closed_with_a_ticket_offer(self, backend, raw, set_now):
        c = at_hours(backend, set_now, 168)
        choice = c.say(REQUEST_TEXT.format(problem="too big"))
        set_now(DELIVERED + timedelta(hours=169))
        out = c.pick(choice, "standard")
        assert "168-hour" in out.text and "2026-11-11" in out.text and "can't start a return" in out.text
        assert "support ticket" in out.text.lower() and rows(raw, "return_requests") == []

    def test_the_selected_category_is_never_changed(self, backend, raw, set_now):
        c = at_hours(backend, set_now, 72)
        out = refusal_for(c, "damaged")
        assert out.pending is None and rows(raw, "return_requests") == []

    def test_the_offer_is_not_a_ticket_and_a_typed_yes_creates_nothing(self, backend, raw, set_now):
        c = at_hours(backend, set_now, 72)
        refusal_for(c, "damaged")
        yes = c.say("yes")
        assert yes.pending is None and yes.status != "needs_confirmation"
        assert rows(raw, "support_tickets") == [] and rows(raw, "return_requests") == []

    @pytest.mark.parametrize("answer", ["no", "No thanks", "cancel"])
    def test_declining_the_offer_creates_nothing(self, backend, raw, set_now, answer):
        c = at_hours(backend, set_now, 72)
        refusal_for(c, "damaged")
        reply = c.say(answer)
        assert reply.pending is None and rows(raw, "support_tickets") == [] and rows(raw, "return_requests") == []

    def test_taking_the_offer_collects_details_first_and_needs_the_confirm_button(self, backend, raw, set_now):
        """Superseded by the final corrective pass: accepting only starts detail collection."""
        c = at_hours(backend, set_now, 72)
        refusal_for(c, "damaged")
        asked = c.say("I want to raise a ticket")
        assert asked.pending is None and "describe the problem" in asked.text
        assert rows(raw, "support_tickets") == []
        proposal = c.say("The left earbud is not working and the case is cracked.")
        assert proposal.status == "needs_confirmation" and proposal.pending.kind == "support_ticket"
        assert "left earbud" in proposal.pending.summary and rows(raw, "support_tickets") == []
        typed = c.say("yes")  # a typed yes is not a confirmation
        assert typed.status == "needs_confirmation" and typed.pending.action_id == proposal.pending.action_id
        assert rows(raw, "support_tickets") == []
        assert c.say("no").status == "needs_confirmation" and rows(raw, "support_tickets") == []
        assert "TKT_" in c.confirm(proposal).text  # only the explicit action creates the ticket
        tickets = rows(raw, "support_tickets")
        assert len(tickets) == 1 and tickets[0]["customer_id"] == "C101"
        assert rows(raw, "return_requests") == []

    def test_declining_the_proposal_with_the_button_creates_no_ticket(self, backend, raw, set_now):
        c = at_hours(backend, set_now, 72)
        refusal_for(c, "damaged")
        c.say("I want to raise a ticket")
        proposal = c.say("The left earbud is not working and the case is cracked.")
        c.confirm(proposal, accepted=False)
        assert rows(raw, "support_tickets") == []


# ---------------------------------------------------------------------------
# G1 / G2 / G3  Late revalidation and error branches
# ---------------------------------------------------------------------------

class TestRevalidationBetweenSteps:
    def test_a_duplicate_created_before_the_category_is_refused_and_nothing_is_added(self, backend, raw):
        c = Client(backend, "C101")
        choice = c.say(REQUEST_TEXT.format(problem="too big"))
        assert choice.status == "needs_choice"
        repository.create_return_request("C101", "ORD1001", "IT101", "created elsewhere", reason_category="standard")
        out = c.pick(choice, "standard")
        assert "already an active return" in out.text and out.pending is None and out.status == "ok"
        assert len(rows(raw, "return_requests")) == 1  # only the one created elsewhere
        session = backend.registry.get(c.state._handle)
        assert backend.registry.open_pending(session) is None and session.draft is None

    @pytest.mark.parametrize("new_status", ["Returned", "Cancelled", "Shipped"])
    def test_an_order_that_stops_being_delivered_before_the_category_is_refused(self, backend, raw, new_status):
        c = Client(backend, "C101")
        choice = c.say(REQUEST_TEXT.format(problem="too big"))
        assert choice.status == "needs_choice"
        raw.execute("PRAGMA ignore_check_constraints = ON")  # a controlled fixture change, test database only
        raw.execute("UPDATE orders SET status = ? WHERE order_id = 'ORD1001'", (new_status,))
        raw.commit()
        out = c.pick(choice, "standard")
        assert "delivered orders" in out.text and new_status in out.text and out.pending is None
        assert rows(raw, "return_requests") == []

    def test_confirmation_time_revalidation_still_refuses_a_changed_order(self, backend, raw):
        c = Client(backend, "C101")
        pending = c.pick(c.say(REQUEST_TEXT.format(problem="too big")), "standard")
        assert pending.status == "needs_confirmation"
        raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute("UPDATE orders SET status = 'Returned' WHERE order_id = 'ORD1001'")
        raw.commit()
        out = c.confirm(pending)
        assert "RET_" not in out.text and "delivered orders" in out.text
        assert rows(raw, "return_requests") == []

    def test_confirmation_time_revalidation_still_refuses_a_duplicate(self, backend, raw):
        c = Client(backend, "C101")
        pending = c.pick(c.say(REQUEST_TEXT.format(problem="too big")), "standard")
        repository.create_return_request("C101", "ORD1001", "IT101", "elsewhere", reason_category="standard")
        out = c.confirm(pending)
        assert "already an active return" in out.text and len(rows(raw, "return_requests")) == 1

    @pytest.mark.parametrize("category", ["damaged", "wrong_product"])
    def test_a_category_window_that_closes_before_confirmation_is_refused_with_a_ticket_offer(
            self, backend, raw, set_now, category):
        c = at_hours(backend, set_now, 47)
        pending = c.pick(c.say(REQUEST_TEXT.format(problem="damaged")), category)
        assert pending.status == "needs_confirmation"
        set_now(DELIVERED + timedelta(hours=49))
        out = c.confirm(pending)
        assert "RET_" not in out.text and "48-hour reporting window" in out.text
        assert_offers_a_ticket_without_promises(out.text)
        assert rows(raw, "return_requests") == []
        session = backend.registry.get(c.state._handle)
        assert session.committed["return_request"] == 0 and session.reserved["return_request"] == 0

    def test_the_boundary_second_is_still_allowed_for_the_category(self, backend, raw, set_now):
        c = at_hours(backend, set_now, 47)
        pending = c.pick(c.say(REQUEST_TEXT.format(problem="damaged")), "damaged")
        set_now(DELIVERED + timedelta(hours=48))  # inclusive
        assert "RET_" in c.confirm(pending).text and len(rows(raw, "return_requests")) == 1


class TestErrorAndFallbackBranches:
    def test_a_database_error_in_the_early_check_is_reported_and_asks_nothing(self, backend, raw, monkeypatch):
        def down(*args, **kwargs):
            raise DatabaseOperationalError("down")

        monkeypatch.setattr(repository, "check_return_eligibility", down)
        c = Client(backend, "C101")
        reply = c.say("I want to return item IT101 from order ORD1001")
        assert reply.status == "error" and reply.error_code == "unavailable"
        assert "can't reach the order system" in reply.text and ASK_FOR_REASON not in reply.text
        assert backend.registry.get(c.state._handle).draft is None and rows(raw, "return_requests") == []

    def test_if_the_standard_window_cannot_be_read_the_category_refusal_falls_back_safely(
            self, backend, raw, set_now, monkeypatch):
        real = repository.check_return_eligibility
        calls = []

        def flaky(customer_id, order_id, item_id, *, reason_category):
            calls.append(reason_category)
            if len(calls) == 3:  # the third read is the standard-window lookup inside the refusal
                raise DatabaseOperationalError("down")
            return real(customer_id, order_id, item_id, reason_category=reason_category)

        monkeypatch.setattr(repository, "check_return_eligibility", flaky)
        c = at_hours(backend, set_now, 72)
        out = refusal_for(c, "damaged")
        assert calls == ["standard", "damaged", "standard"]
        assert out.status == "ok" and out.pending is None
        assert "48-hour" in out.text and "closed on" in out.text and "support ticket" in out.text.lower()
        assert "None" not in out.text and rows(raw, "return_requests") == []

    def test_a_missing_delivery_timestamp_is_refused_before_any_question(self, backend, raw, monkeypatch):
        def missing(customer_id, order_id, item_id, *, reason_category):
            return {"outcome": "delivery_timestamp_missing", "reason_category": reason_category,
                    "order_status": "Delivered", "window_hours": 168, "delivered_at": None,
                    "window_closes_at": None, "evaluated_at": "2026-11-07T00:00:00Z",
                    "window_reason": "delivery_not_recorded", "existing_return_status": None}

        monkeypatch.setattr(repository, "check_return_eligibility", missing)
        c = Client(backend, "C101")
        reply = c.say("I want to return item IT101 from order ORD1001")
        assert "can't confirm the delivery date" in reply.text and "support ticket" in reply.text.lower()
        assert ASK_FOR_REASON not in reply.text and reply.choice is None
        assert backend.registry.get(c.state._handle).draft is None and rows(raw, "return_requests") == []
