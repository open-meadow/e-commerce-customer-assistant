"""Topic-scoped retrieval regressions (final source review, 2026-11-11).

1. "What is the delivery window?" used to return the RETURN policy (Q3.1)
   first: the bridge expanded "window" to "within / days / eligible" for every
   topic. The bridge is now scoped: "window" means "within 7 days" only when
   the query is about returns, and "3-6 business days" when it is about
   delivery. Unrelated policies must not outrank the right one.
2. "Can I return a non-returnable product?" must be answered from the passage
   that states the exceptions, never from an unconditional "7 days" statement.
"""

from __future__ import annotations

import pytest

from app.backend import Backend, Settings
from app.rag import load_chunks
from backend_helpers import Client, LazyKnowledgeBase, ScriptedLLM

KB = LazyKnowledgeBase()  # resolved at call time: no key, index or network at import
CHUNKS = {(c.source, c.section): c for c in load_chunks()}

Q22 = ("FAQ.md", "Q2.2")        # How long does delivery take? (3-6 business days)
Q31 = ("FAQ.md", "Q3.1")        # What is your return policy?
P2 = ("Policies.md", "2. Returns & Refunds Policy")
P1 = ("Policies.md", "1. Shipping Policy")


def ranked(query, k=6):
    return [(h.chunk.source, h.chunk.section) for h in KB.search(query, k=k)]


class TestDeliveryWindow:
    @pytest.mark.parametrize("query", [
        "What is the delivery window?", "what is the delivery period", "delivery timeline",
        "what is the delivery timeframe", "how long is the delivery window",
    ])
    def test_the_delivery_time_passage_is_first(self, query):
        order = ranked(query)
        assert order[0] == Q22, (query, order)
        assert "3–6 business days" in CHUNKS[Q22].text

    @pytest.mark.parametrize("query", ["What is the delivery window?", "what is the delivery period"])
    def test_return_and_cancellation_passages_do_not_outrank_it(self, query):
        order = ranked(query)
        unrelated = [("FAQ.md", s) for s in ("Q3.1", "Q3.2", "Q3.3", "Q3.4", "Q5.1")] + [P2]
        for passage in unrelated:
            if passage in order:
                assert order.index(passage) > order.index(Q22), (query, passage, order)

    def test_the_shipping_window_is_answered_from_shipping_text_not_cancellation_or_returns(self):
        order = ranked("how long is the shipping window")
        assert order[0] in (P1, Q22)
        for passage in (Q31, P2, ("FAQ.md", "Q5.1"), ("FAQ.md", "Q3.4")):
            if passage in order:
                assert order.index(passage) > order.index(order[0])

    def test_the_same_word_still_means_the_return_period_for_returns(self):
        assert ranked("what is the return window")[0] == Q31
        assert ranked("what is the return deadline")[0] == Q31
        assert "7 days" in CHUNKS[Q31].text

    def test_and_the_refund_period_for_refunds(self):
        assert ranked("how long is the refund period")[0] == ("FAQ.md", "Q3.3")
        assert ranked("refund timeline")[0] == ("FAQ.md", "Q3.3")

    def test_without_a_topic_the_generic_word_matches_nothing(self):
        assert KB.search("what is the window", k=3) == []
        assert KB.search("what is the period", k=3) == []

    def test_warranty_period_is_unaffected(self):
        order = ranked("what is the warranty period")
        assert set(order[:2]) == {("FAQ.md", "Q6.1"), ("Policies.md", "4. Warranty Policy")}
        assert Q31 not in order

    @pytest.fixture
    def client(self, temp_db):
        return Client(Backend(llm=ScriptedLLM(), settings=Settings()), "C101")

    def test_the_backend_answers_with_the_delivery_passage_and_cites_it(self, client):
        reply = client.say("What is the delivery window?")
        assert reply.status == "ok"
        assert [(c.source, c.section) for c in reply.citations] == [Q22]
        assert CHUNKS[Q22].text in reply.text
        assert "business days" in reply.text and "within 7 days" not in reply.text


class TestNonReturnableProduct:
    QUESTIONS = ["Can I return a non-returnable product?", "can i return a product that is non-returnable"]

    @pytest.mark.parametrize("query", QUESTIONS)
    def test_the_exceptions_passages_are_the_top_two(self, query):
        order = ranked(query)
        assert set(order[:2]) == {P2, Q31}, order
        assert order[0] == P2  # the passage that lists the non-returnable examples

    def test_both_top_passages_state_the_condition(self):
        assert "Non-returnable examples" in CHUNKS[P2].text and "Hygiene" in CHUNKS[P2].text
        assert "may not be returnable" in CHUNKS[Q31].text

    def test_unrelated_policies_do_not_outrank_them(self):
        order = ranked("Can I return a non-returnable product?")
        for other in (("FAQ.md", "Q3.4"), ("FAQ.md", "Q6.1"), ("FAQ.md", "Q3.3"), ("FAQ.md", "Q5.1")):
            if other in order:
                assert order.index(other) > order.index(Q31) and order.index(other) > order.index(P2)

    @pytest.fixture
    def client(self, temp_db):
        return Client(Backend(llm=ScriptedLLM(), settings=Settings()), "C101")

    def test_the_reply_is_conditional_never_a_bare_seven_day_answer(self, client):
        reply = client.say("Can I return a non-returnable product?")
        assert reply.status == "ok"
        assert reply.citations and (reply.citations[0].source, reply.citations[0].section) in (P2, Q31)
        text = reply.text
        assert "returnable within 7 days" in text or "within 7 days" in text  # the general rule is stated
        assert "Non-returnable examples" in text or "may not be returnable" in text  # with its exceptions
        assert "Hygiene/personal care items" in text or "hygiene products" in text

    def test_the_cited_passage_is_quoted_in_full_so_conditions_cannot_be_dropped(self, client):
        reply = client.say("Can I return a non-returnable product?")
        cited = (reply.citations[0].source, reply.citations[0].section)
        assert CHUNKS[cited].text in reply.text
