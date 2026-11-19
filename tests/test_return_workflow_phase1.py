"""Phase 1 return-workflow correctness (audit findings D-01, D-04, D-05, D-06).

D-01  Return intent: a clear request starts a return; a negated request or a question about eligibility or
      policy does not, with and without an order earlier in the conversation.
D-04  Eligibility that does not depend on the reason category (ownership, item, order status, active return,
      the widest window) is checked before the customer is asked for a reason or a category.
D-05  The 48-hour damaged/wrong-product refusal no longer claims every return is closed while the 168-hour
      standard window is still open; the customer's selected category is never changed.
D-06  Reasons written in Devanagari (and other non-ASCII scripts) are accepted and stored exactly.

Everything uses RuleBasedLLM, temporary seeded databases and fixed clocks; nothing touches a protected database
or the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backend import Backend, Settings
from app.llm import rule_based
from app.llm.rule_based import RuleBasedLLM
from app.orchestrator import guard
from backend_helpers import Client, ScriptedLLM

UTC = timezone.utc
DELIVERED = datetime(2026, 11, 4, tzinfo=UTC)  # ORD1001 / IT101 (C101); the seed baseline is 2026-11-07
ASK_FOR_ORDER = "Which order would you like to return? Please share the order ID."


class Spy(RuleBasedLLM):
    """The stand-in, recording every tool call it proposes."""

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
    def build(customer_id="C101"):
        return Client(Backend(llm=spy, settings=Settings()), customer_id)
    return build


@pytest.fixture
def backend(temp_db):
    return Backend(llm=ScriptedLLM(), settings=Settings())


def names(spy):
    return [name for name, _ in spy.proposed]


# ---------------------------------------------------------------------------
# D-01  Return intent
# ---------------------------------------------------------------------------

REQUESTS = ["I want to return it", "I want to return this item because it is damaged", "I would like to return it",
            "I need to return this", "return it", "Please return my order", "Could you please start a return?",
            "I don't like the colour, I want to return it", "I do not like it but I want to return it",
            "no, I want to return it"]
NEGATED = ["I don't want to return it", "I do not want to return this", "I don't want to return this item",
           "Please don't return it", "I do not want to return this because it is fine", "I never want to return it"]
QUESTIONS = ["Can I return it?", "Can I return this item?", "How many days do I have to return this?",
             "What is the return window?", "How do I start a return?", "Is it possible to return this item?",
             "Do I have to return this?", "Why can't I return it?", "I want to know how long I have to return this",
             "Could I return it?"]


class TestReturnIntentClassification:
    @pytest.mark.parametrize("text", REQUESTS)
    def test_a_clear_request_is_a_request(self, text):
        assert rule_based.return_intent(text.lower()) == "request"

    @pytest.mark.parametrize("text", NEGATED)
    def test_a_negated_request_is_negated(self, text):
        assert rule_based.return_intent(text.lower()) == "negated"

    @pytest.mark.parametrize("text", [q for q in QUESTIONS if q != "What is the return window?"])
    def test_a_question_is_a_question(self, text):
        assert rule_based.return_intent(text.lower()) == "question"

    def test_no_return_wording_is_none(self):
        assert rule_based.return_intent("what is the return window?") is None
        assert rule_based.return_intent("where is my order") is None

    def test_a_later_negation_does_not_cancel_an_earlier_request(self):
        assert rule_based.return_intent("i want to return it, not exchange it") == "request"


class TestReturnIntentInConversation:
    """With and without an order earlier in the conversation."""

    @pytest.mark.parametrize("history", [False, True])
    @pytest.mark.parametrize("text", NEGATED)
    def test_a_negated_request_never_starts_a_return(self, make_client, spy, raw, history, text):
        c = make_client()
        if history:
            c.say("ORD1001")
        spy.proposed.clear()
        reply = c.say(text)
        assert not {"start_return_request", "get_order_status"} & set(names(spy))
        assert reply.status == "ok" and reply.choice is None and reply.pending is None
        assert reply.text.startswith("Okay, I won't start a return")
        assert rows(raw, "return_requests") == []

    @pytest.mark.parametrize("history", [False, True])
    @pytest.mark.parametrize("text", QUESTIONS)
    def test_a_question_goes_to_policy_help_and_never_starts_a_return(self, make_client, spy, raw, history, text):
        c = make_client()
        if history:
            c.say("ORD1001")
        spy.proposed.clear()
        reply = c.say(text)
        assert "start_return_request" not in names(spy) and "get_order_status" not in names(spy)
        assert names(spy)[:1] == ["search_policies"]
        assert reply.choice is None and reply.pending is None and rows(raw, "return_requests") == []

    def test_a_request_after_an_order_was_discussed_starts_the_return(self, make_client, spy):
        c = make_client()
        c.say("ORD1001")
        spy.proposed.clear()
        reply = c.say("I want to return it")
        assert names(spy) == ["get_order_status", "start_return_request"]
        assert reply.status == "ok" and "why you want to return" in reply.text  # the existing reason step

    def test_a_request_with_a_reason_after_an_order_starts_the_return(self, make_client, spy):
        c = make_client()
        c.say("ORD1001")
        spy.proposed.clear()
        reply = c.say("I want to return this item because it is damaged")
        assert names(spy) == ["get_order_status", "start_return_request"]
        assert reply.status == "needs_choice"  # category buttons: the customer chooses

    @pytest.mark.parametrize("text", ["I want to return it", "I want to return this item because it is damaged"])
    def test_a_request_with_no_order_asks_for_the_order(self, make_client, spy, text):
        reply = make_client().say(text)
        assert reply.text.startswith(ASK_FOR_ORDER) and spy.proposed == []  # a damaged-item message may add a ticket offer

    def test_a_polite_request_with_an_order_id_is_still_a_request(self, make_client, spy):
        make_client().say("Could you please start a return for ORD1001?")
        assert names(spy) == ["get_order_status", "start_return_request"]

    def test_a_question_that_names_an_order_does_not_start_a_return(self, make_client, spy):
        reply = make_client().say("Can I return ORD1001?")
        assert "start_return_request" not in names(spy) and reply.choice is None and reply.pending is None

    def test_an_explicit_item_request_is_unchanged(self, make_client, spy):
        reply = make_client().say("I want to return item IT101 from order ORD1001 because I changed my mind")
        assert spy.proposed == [("start_return_request", {"order_id": "ORD1001", "item_id": "IT101"})]
        assert reply.status == "needs_choice"

    def test_nothing_is_written_before_the_confirm_button(self, make_client, raw):
        c = make_client()
        c.say("ORD1001")
        asked = c.say("I want to return it")
        choice = c.say("It is too big for me")
        assert choice.status == "needs_choice" and rows(raw, "return_requests") == []
        pending = c.pick(choice, "standard")
        assert pending.status == "needs_confirmation" and rows(raw, "return_requests") == []
        assert c.say("yes").status == "needs_confirmation" and rows(raw, "return_requests") == []
        assert asked.status == "ok"
        assert "RET_" in c.confirm(pending).text and len(rows(raw, "return_requests")) == 1


# ---------------------------------------------------------------------------
# D-04  Eligibility before the reason and category
# ---------------------------------------------------------------------------

BARE = "I want to return item {item} from order {order}"
WITH_REASON = "I want to return item {item} from order {order} because it does not suit me"
NEVER_ASKED = ("why you want to return", "What best describes the reason")


def refused_early(backend, reply, c, raw, saved=0):
    assert reply.status == "ok" and reply.choice is None and reply.pending is None
    assert not any(marker in reply.text for marker in NEVER_ASKED)
    assert backend.registry.get(c.state._handle).draft is None
    assert len(rows(raw, "return_requests")) == saved


class TestEligibilityBeforeTheReason:
    @pytest.mark.parametrize("template", [BARE, WITH_REASON])
    @pytest.mark.parametrize("customer, order, item, status", [
        ("C101", "ORD1002", "IT102", "Shipped"),
        ("C101", "ORD1014", "IT115", "Processing"),
        ("C103", "ORD1006", "IT107", "Cancelled"),
        ("C105", "ORD1010", "IT111", "Returned"),
        ("C104", "ORD1007", "IT108", "Out for Delivery"),
        ("C104", "ORD1009", "IT110", "Packed"),
    ])
    def test_an_order_that_is_not_delivered_is_refused_before_any_question(
            self, backend, raw, template, customer, order, item, status):
        c = Client(backend, customer)
        reply = c.say(template.format(order=order, item=item))
        refused_early(backend, reply, c, raw)
        assert "delivered orders" in reply.text and status in reply.text

    @pytest.mark.parametrize("template", [BARE, WITH_REASON])
    def test_a_delivered_order_proceeds(self, backend, template):
        reply = Client(backend, "C101").say(template.format(order="ORD1001", item="IT101"))
        if template is BARE:
            assert reply.status == "ok" and "why you want to return" in reply.text
        else:
            assert reply.status == "needs_choice"

    def test_another_customers_order_is_refused_without_disclosing_it(self, backend, raw):
        c = Client(backend, "C101")
        other = c.say(BARE.format(order="ORD1003", item="IT103"))  # exists, belongs to C102
        refused_early(backend, other, c, raw)
        unknown = Client(backend, "C101").say(BARE.format(order="ORD9999", item="IT103"))
        assert other.text == unknown.text == "I couldn't find that order on your account."

    def test_an_unknown_order_is_a_safe_not_found(self, backend, raw):
        c = Client(backend, "C101")
        refused_early(backend, c.say(WITH_REASON.format(order="ORD9999", item="IT101")), c, raw)

    def test_an_item_from_a_different_order_is_refused(self, backend, raw):
        c = Client(backend, "C101")
        reply = c.say(BARE.format(order="ORD1001", item="IT102"))  # IT102 is on ORD1002
        refused_early(backend, reply, c, raw)
        assert reply.text == "I couldn't find that item on that order."

    def test_an_existing_active_return_is_refused_before_a_new_reason(self, backend, raw):
        c = Client(backend, "C101")
        pending = c.pick(c.say(WITH_REASON.format(order="ORD1001", item="IT101")), "standard")
        assert "RET_" in c.confirm(pending).text
        again = c.say(BARE.format(order="ORD1001", item="IT101"))
        refused_early(backend, again, c, raw, saved=1)  # only the return that was already confirmed
        assert "already an active return" in again.text

    def test_the_window_that_has_closed_for_every_category_is_refused_before_a_question(
            self, backend, raw, set_now):
        set_now(DELIVERED + timedelta(hours=169))
        c = Client(backend, "C101")
        reply = c.say(BARE.format(order="ORD1001", item="IT101"))
        refused_early(backend, reply, c, raw)
        assert "2026-11-11" in reply.text and "168" in reply.text

    def test_the_check_is_repeated_when_the_customer_confirms(self, backend, raw, set_now):
        set_now(DELIVERED + timedelta(hours=167, minutes=58))
        c = Client(backend, "C101")
        pending = c.pick(c.say(WITH_REASON.format(order="ORD1001", item="IT101")), "standard")
        assert pending.status == "needs_confirmation"
        set_now(DELIVERED + timedelta(hours=168, seconds=1))
        out = c.confirm(pending)
        assert "RET_" not in out.text and rows(raw, "return_requests") == []

    def test_the_conversation_can_continue_after_an_early_refusal(self, backend):
        c = Client(backend, "C101")
        c.say(BARE.format(order="ORD1002", item="IT102"))
        assert "Delivered" in c.say("ORD1001").text


# ---------------------------------------------------------------------------
# D-05  Category window versus standard window (fixed clock)
# ---------------------------------------------------------------------------

def standard_open_until():
    return (DELIVERED + timedelta(hours=168)).strftime("%Y-%m-%d")  # 2026-11-11


def category_closes():
    return (DELIVERED + timedelta(hours=48)).strftime("%Y-%m-%d")  # 2026-11-06


def start_at(backend, set_now, hours):
    set_now(DELIVERED + timedelta(hours=hours))
    return Client(backend, "C101")


class TestCategoryWindowMessages:
    @pytest.mark.parametrize("category", ["damaged", "wrong_product"])
    @pytest.mark.parametrize("hours, eligible", [(47, True), (48, True)])
    def test_the_category_window_is_open_until_exactly_48_hours(self, backend, set_now, category, hours, eligible):
        c = start_at(backend, set_now, hours)
        out = c.pick(c.say(WITH_REASON.format(order="ORD1001", item="IT101")), category)
        assert out.status == "needs_confirmation" and out.pending.kind == "return_request"

    @pytest.mark.parametrize("category", ["damaged", "wrong_product"])
    @pytest.mark.parametrize("hours", [49, 72, 167, 168])
    def test_after_48_hours_the_reporting_window_is_explained_and_a_ticket_is_offered(
            self, backend, raw, set_now, category, hours):
        """Superseded by the approved W1 decision: no steer to the standard category, a ticket is offered."""
        c = start_at(backend, set_now, hours)
        out = c.pick(c.say(WITH_REASON.format(order="ORD1001", item="IT101")), category)
        text = out.text
        assert out.status == "ok" and out.pending is None and out.choice is None
        assert "48-hour reporting window" in text and category_closes() in text
        assert "can't start a return" in text and "support ticket" in text
        assert "may still be possible" not in text and "true for you" not in text  # no steer to another reason
        assert "standard" not in text and "168-hour" not in text
        assert rows(raw, "return_requests") == []

    @pytest.mark.parametrize("category", ["damaged", "wrong_product"])
    def test_after_168_hours_both_windows_are_reported_closed(self, backend, raw, set_now, category):
        c = start_at(backend, set_now, 168)  # the request is still eligible here...
        choice = c.say(WITH_REASON.format(order="ORD1001", item="IT101"))
        assert choice.status == "needs_choice"
        set_now(DELIVERED + timedelta(hours=169))  # ...and the standard window closes before the choice is made
        out = c.pick(choice, category)
        text = out.text
        assert out.status == "ok" and out.pending is None
        assert "48-hour" in text and "168-hour" in text and category_closes() in text and standard_open_until() in text
        assert "so I can't start a return" in text and "support ticket" in text
        assert "may still be possible" not in text
        assert rows(raw, "return_requests") == []

    @pytest.mark.parametrize("hours, eligible", [(47, True), (48, True), (49, True), (72, True), (167, True),
                                                 (168, True)])
    def test_the_standard_category_is_open_through_exactly_168_hours(self, backend, set_now, hours, eligible):
        c = start_at(backend, set_now, hours)
        out = c.pick(c.say(WITH_REASON.format(order="ORD1001", item="IT101")), "standard")
        assert out.status == "needs_confirmation"

    def test_the_standard_category_is_closed_at_169_hours_and_says_when(self, backend, raw, set_now):
        c = start_at(backend, set_now, 168)
        choice = c.say(WITH_REASON.format(order="ORD1001", item="IT101"))
        set_now(DELIVERED + timedelta(hours=169))
        out = c.pick(choice, "standard")
        assert out.status == "ok" and out.pending is None
        assert "168-hour" in out.text and standard_open_until() in out.text and "can't start a return" in out.text
        assert rows(raw, "return_requests") == []

    @pytest.mark.parametrize("hours", [49, 72, 167, 168])
    def test_the_damaged_refusal_does_not_change_what_the_standard_category_does(self, backend, set_now, hours):
        """The refusal never converts the category; the standard category stays separately eligible."""
        c = start_at(backend, set_now, hours)
        choice = c.say(WITH_REASON.format(order="ORD1001", item="IT101"))
        refusal = c.pick(choice, "damaged")
        assert refusal.pending is None and "standard" not in refusal.text
        c2 = Client(backend, "C101")
        second = c2.pick(c2.say(WITH_REASON.format(order="ORD1001", item="IT101")), "standard")
        assert second.status == "needs_confirmation"

    @pytest.mark.parametrize("hours", [49, 72, 167])
    def test_the_repository_still_refuses_the_category_directly(self, set_now, temp_db, hours):
        from app.db import repository

        set_now(DELIVERED + timedelta(hours=hours))
        verdict = repository.check_return_eligibility("C101", "ORD1001", "IT101", reason_category="damaged")
        assert verdict["outcome"] == "outside_return_window" and verdict["window_hours"] == 48
        again = repository.create_return_request("C101", "ORD1001", "IT101", "r", reason_category="damaged")
        assert again["outcome"] == "outside_return_window"

    def test_a_customer_can_still_complete_a_standard_return_after_the_damaged_refusal(self, backend, raw, set_now):
        c = start_at(backend, set_now, 72)
        refusal = c.pick(c.say(WITH_REASON.format(order="ORD1001", item="IT101")), "damaged")
        assert refusal.pending is None
        pending = c.pick(c.say(WITH_REASON.format(order="ORD1001", item="IT101")), "standard")
        assert pending.status == "needs_confirmation"
        assert "RET_" in c.confirm(pending).text
        assert [r["reason_category"] for r in rows(raw, "return_requests")] == ["standard"]  # what they chose


# ---------------------------------------------------------------------------
# D-06  Devanagari reasons
# ---------------------------------------------------------------------------

HINDI = ["यह प्रोडक्ट काम नहीं कर रहा है", "मुझे गलत प्रोडक्ट मिला है", "इसमें आवाज़ नहीं आ रही है"]
ENGLISH = "The colour is too bright for me"
HINGLISH = "yeh product kaam nahi kar raha hai"


class TestReasonText:
    @pytest.mark.parametrize("reason", HINDI + [ENGLISH, HINGLISH, "ठीक नहीं", "123", "size 9", "it's broken"])
    def test_a_meaningful_reason_is_an_explanation(self, reason):
        assert guard.has_explanation(reason) is True

    @pytest.mark.parametrize("reason", ["", "   ", "\n\t", "!!!", "...", "?", "।", "-- ?? --", "क", "a", "'",
                                        "I want to return it", "I want to return item IT101 from order ORD1001",
                                        "yes", "please"])
    def test_nothing_or_only_request_words_is_not_an_explanation(self, reason):
        assert guard.has_explanation(reason) is False

    @pytest.mark.parametrize("text", [
        "I'd like to return it", "I'd like to return it, it's broken", "I want to return it", "yes", "it's",
        "return it please", "item IT101 from ORD1001 is broken", "wrong colour", "a b c", "x", "", "   ", "!!",
        "don't", "'tis", "COLOUR IS WRONG", "size 9", "9", "ok ok",
    ])
    def test_ascii_behaviour_is_identical_to_the_previous_tokeniser(self, text):
        import re

        tokens = re.findall(r"[a-z0-9']+", guard._ID_TOKEN.sub(" ", text.lower()))  # the pre-fix implementation
        legacy = any(t not in guard._REQUEST_WORDS and len(t) > 1 for t in tokens)
        assert guard.has_explanation(text) is legacy


class TestStoredReasonInOtherScripts:
    @pytest.mark.parametrize("reason", HINDI + [ENGLISH, HINGLISH])
    def test_a_bare_request_then_the_reason_is_stored_exactly(self, backend, raw, reason):
        c = Client(backend, "C101")
        asked = c.say("I want to return item IT101 from order ORD1001")
        assert asked.status == "ok" and "why you want to return" in asked.text
        choice = c.say(reason)
        assert choice.status == "needs_choice", choice.text
        pending = c.pick(choice, "standard")
        assert pending.status == "needs_confirmation" and reason in pending.pending.summary
        assert rows(raw, "return_requests") == []
        assert "RET_" in c.confirm(pending).text
        assert rows(raw, "return_requests")[0]["reason"] == reason  # byte for byte, never rewritten

    def test_a_reason_in_the_first_message_is_accepted_and_kept(self, backend, raw):
        text = "I want to return item IT101 from order ORD1001 क्योंकि यह काम नहीं कर रहा है"
        c = Client(backend, "C101")
        pending = c.pick(c.say(text), "standard")
        assert "RET_" in c.confirm(pending).text
        assert rows(raw, "return_requests")[0]["reason"] == text

    @pytest.mark.parametrize("junk", ["!!!", "...", "।", "?"])
    def test_a_message_with_no_words_is_still_asked_again(self, backend, raw, junk):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        again = c.say(junk)
        assert again.status == "ok" and again.choice is None
        assert "why you want to return" in again.text or "still waiting for your reason" in again.text
        assert rows(raw, "return_requests") == []

    def test_whitespace_only_is_refused_before_it_can_be_stored(self, backend, raw):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        assert c.say("   ").error_code == "invalid_request"
        assert rows(raw, "return_requests") == []

    def test_the_length_limit_still_applies_to_other_scripts(self, backend, raw):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        too_long = "खराब " * 201  # 1005 code points
        reply = c.say(too_long)
        assert reply.choice is None and "at most 1000" in reply.text
        assert rows(raw, "return_requests") == []
        assert c.say("खराब " * 100).status == "needs_choice"  # 500 code points are fine

    def test_a_question_in_the_reason_step_is_still_not_stored(self, backend, raw):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        reply = c.say("What is your refund policy?")
        assert "still waiting for your reason" in reply.text and reply.choice is None
