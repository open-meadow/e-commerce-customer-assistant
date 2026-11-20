"""The ticket clarification must not loop, and a request to change delivery details must offer a ticket and ask for a description.

Reported chat (the customer's own words, kept exactly):
    "i have got half refund, so pls help me to update that ticket" -> asks what the issue is
    "order ord1010"        -> asks what went wrong
    "i got half refund"    -> asked AGAIN (the same sentence)
    "for refund", "yes for refund", an insult -> asked again, every time with the same sentence.
Cause: the scope gate needed a complaint word and "half" was not one; every later reply was appended to the buffer and checked
against the same rule, so the customer could never get out of the question. Fixed: "half/partial/only" count as complaint words
beside a transaction word, a reply that names one of the categories the question listed is an ANSWER to it, and a reply that
names nothing is never added to the description (it gets a more specific request to describe the problem instead).

Also: "I want to change my address" now offers a ticket and explicitly asks the customer to write the details; the customer's
original words stay at the start of the ticket description.

Scripted model output only; no network.
"""

from __future__ import annotations

import pytest

from app.backend import Backend, Settings
from app.llm.base import LLMResult, ToolCall
from app.orchestrator import guard
from app.tools import flow_texts
from backend_helpers import Client, ScriptedLLM

LANGS = ("en", "hinglish", "hi")


def sentences(key):
    return {flow_texts.text(key, lang) for lang in LANGS}


SCOPE_ASK = sentences("ticket_scope_ask")
DESCRIBE_ASK = sentences("ticket_describe_ask")
SERVICE_ASK = sentences("ticket_service_ask")
DECLINED = sentences("ticket_declined")
OUT_OF_SCOPE = sentences("ticket_out_of_scope")
ASK_ISSUE = sentences("ask_issue")


class TicketModel(ScriptedLLM):
    """Answers every message with a start_support_ticket proposal (the worst case for the gate)."""

    interprets_language = True

    def __init__(self):
        super().__init__(chat=self._chat)

    def _chat(self, system, messages, tools):
        if messages and messages[-1].get("role") == "tool":
            return LLMResult(text="ok")
        return LLMResult(tool_calls=(ToolCall("start_support_ticket", {"issue_type": "general"}, "t1"),))


@pytest.fixture(autouse=True)
def _database(temp_db):
    """Every test runs on a seeded temporary database (never the default one)."""
    return temp_db


def client():
    return Client(Backend(llm=TicketModel(), settings=Settings()), "C102")


def tickets(raw):
    return [dict(r) for r in raw.execute("SELECT * FROM support_tickets").fetchall()]


def card(reply):
    return reply.pending is not None and reply.pending.kind == "support_ticket"


class TestReportedChat:
    def test_the_whole_reported_conversation_ends_in_a_card(self, raw):
        c = client()
        first = c.say("i have got half refund, so pls help me to update that ticket")
        # the customer already said what it is about (a half refund): no need to ask again
        assert card(first) or first.text in ASK_ISSUE
        if not card(first):
            r = c.say("order ord1010")
            assert not card(r) and r.text in SCOPE_ASK
            r = c.say("i got half refund")
            assert card(r), r.text
        else:
            r = first
        assert "half refund" in r.pending.summary
        assert tickets(raw) == []  # nothing is written before Confirm

    @pytest.mark.parametrize("answer", ["i got half refund", "for refund", "yes for refund", "refund", "its about refund",
                                        "partial refund only", "refund me aadha mila"])
    def test_an_answer_naming_a_listed_category_gets_the_card(self, raw, answer):
        c = client()
        r = c.say("order ord1010")
        assert not card(r) and r.text in SCOPE_ASK
        r = c.say(answer)
        assert card(r), (answer, r.text)
        assert "order ord1010" in r.pending.summary or "ord1010" in r.pending.summary.lower()
        assert tickets(raw) == []

    @pytest.mark.parametrize("answer", ["for product", "its about delivery", "payment", "for the return", "about my order"])
    def test_every_listed_category_is_an_answer(self, answer):
        c = client()
        c.say("order ord1010")
        r = c.say(answer)
        assert card(r), (answer, r.text)

    @pytest.mark.parametrize("insult", ["madarchod", "you are useless", "bakwas", "fuck this"])
    def test_an_insult_is_never_added_to_the_description_and_never_repeats_the_same_question(self, raw, insult):
        c = client()
        asked = c.say("order ord1010")
        assert asked.text in SCOPE_ASK
        r = c.say(insult)
        assert not card(r)
        assert r.text in DESCRIBE_ASK and r.text not in SCOPE_ASK  # a different, more specific request
        r = c.say("i got half refund")
        assert card(r)
        assert insult not in r.pending.summary
        assert tickets(raw) == []

    def test_a_chain_of_non_answers_never_builds_a_card_and_stays_polite(self, raw):
        c = client()
        c.say("order ord1010")
        for text in ("ok", "hmm", "yes", "whatever", "madarchod"):
            r = c.say(text)
            assert not card(r) and r.text in DESCRIBE_ASK, (text, r.text)
        assert tickets(raw) == []

    def test_confirming_the_card_saves_the_customers_own_words(self, raw):
        c = client()
        c.say("order ord1010")
        r = c.say("i got half refund")
        assert card(r)
        done = c.confirm(r)
        rows = tickets(raw)
        assert len(rows) == 1 and "order ord1010" in rows[0]["description"].lower()
        assert "i got half refund" in rows[0]["description"]
        assert done.pending is None

    def test_a_personal_reply_is_still_refused(self, raw):
        c = client()
        c.say("order ord1010")
        r = c.say("i am nervous about my wedding")
        assert not card(r) and r.text in OUT_OF_SCOPE
        assert tickets(raw) == []

    def test_a_question_after_the_ask_is_answered_normally_not_added_to_the_ticket(self, raw):
        c = client()
        c.say("order ord1010")
        r = c.say("what is the refund policy?")
        assert not card(r)
        assert tickets(raw) == []


class TestGateWording:
    @pytest.mark.parametrize("text", ["i got half refund", "i have got half refund", "got only half of my refund for ORD1010",
                                      "partial refund received on ord1010", "my refund is incomplete",
                                      "refund adha mila ORD1010"])
    def test_a_short_refund_is_a_store_complaint(self, text):
        assert guard.ticket_scope(text) == "store", text

    @pytest.mark.parametrize("text", ["half my day was wasted", "i only want to relax", "the film was too short",
                                      "i am less confident about my interview", "partial to a good joke"])
    def test_those_words_alone_never_authorise_a_ticket(self, text):
        assert guard.ticket_scope(text) != "store", text

    @pytest.mark.parametrize("latest,combined,expected", [
        ("for refund", "order ord1010 for refund", "store"),
        ("yes", "order ord1010 yes", "unclear"),
        ("madarchod", "order ord1010 madarchod", "unclear"),
        ("ORD1010", "order ORD1010", "unclear"),  # an order ID is not an answer to "what went wrong"
        ("for payment", "my wedding for payment", "personal"),  # personal words, no store evidence
    ])
    def test_answering_the_question(self, latest, combined, expected):
        assert guard.ticket_scope_answered(latest, combined) == expected


class TestAddressChange:
    def test_it_offers_a_ticket_and_asks_for_the_details(self, raw):
        c = client()
        r = c.say("I want to change my address")
        assert not card(r) and r.text in SERVICE_ASK
        assert "write" in r.text.lower() and "ticket" in r.text.lower()
        assert tickets(raw) == []

    def test_yes_then_details_gives_a_card_that_keeps_the_original_request(self, raw):
        c = client()
        c.say("I want to change my address")
        r = c.say("yes")
        assert not card(r) and r.text in DESCRIBE_ASK
        r = c.say("ORD1010, the new address is 12 MG Road, Pune 411001")
        assert card(r), r.text
        summary = r.pending.summary
        assert "change my address" in summary and "12 MG Road" in summary
        assert tickets(raw) == []
        c.confirm(r)
        rows = tickets(raw)
        assert len(rows) == 1
        assert "change my address" in rows[0]["description"] and "12 MG Road" in rows[0]["description"]

    def test_details_straight_away_also_work(self, raw):
        c = client()
        c.say("I want to change my address")
        r = c.say("please ship ORD1010 to 12 MG Road, Pune instead")
        assert card(r), r.text
        assert "change my address" in r.pending.summary

    def test_no_declines_and_nothing_is_created(self, raw):
        c = client()
        c.say("I want to change my address")
        r = c.say("no")
        assert not card(r) and r.text in DECLINED
        assert tickets(raw) == []
        again = c.say("ORD1010 12 MG Road")
        assert not card(again)  # the offer is over; this is a fresh message, not a description

    def test_the_offer_is_not_made_for_a_question_or_a_personal_message(self, raw):
        c = client()
        r = c.say("can I change my address after ordering?")
        assert r.text not in SERVICE_ASK
        r = c.say("how do I change my address in my resume")
        assert r.text not in SERVICE_ASK and not card(r)
        assert tickets(raw) == []

    def test_a_typed_cancel_closes_the_offer(self, raw):
        c = client()
        c.say("I want to change my email")
        r = c.say("cancel")
        assert not card(r)
        later = c.say("ORD1010 new email a@b.example")
        assert not card(later)
