"""Regression tests for retrieval relevance and answer grounding.

Origin: "what is the return window" used to rank Q3.2 (how to start a return)
above Q3.1 (the return policy). Cause: the customer says "window" but the
policy says "within 7 days", so only the word "return" matched, and Q3.2
repeats "return" more often in fewer words. Fixes: a small domain
vocabulary bridge (window/deadline/timeline/cost -> the words the documents
use, at half weight) and a stemmer that unifies charge/charges and
cancel/cancelled/cancellation. Retrieval stays lexical BM25.

Each case also checks that the passage that was retrieved really contains
the fact an answer needs, and that the backend's reply quotes that passage.
"""

from __future__ import annotations

import pytest

from app.backend import Backend, Settings
from app.rag import load_chunks
from backend_helpers import Client, LazyKnowledgeBase, ScriptedLLM

KB = LazyKnowledgeBase()  # resolved at call time: no key, index or network at import
CHUNKS = {(c.source, c.section): c for c in load_chunks()}

# (question, acceptable top passages, a fact the passage must contain)
CASES = [
    # Standard return window
    ("what is the return window", [("FAQ.md", "Q3.1")], "7 days"),
    ("how many days do I have to return an item", [("FAQ.md", "Q3.1")], "7 days"),
    ("what is the return deadline", [("FAQ.md", "Q3.1")], "7 days"),
    ("what is your return policy", [("FAQ.md", "Q3.1")], "7 days"),
    # Damaged / wrong product return window
    ("what is the return window for a damaged item", [("FAQ.md", "Q3.4")], "48 hours"),
    ("how long do I have to report a wrong product", [("FAQ.md", "Q3.4")], "48 hours"),
    ("what if I receive a damaged or wrong product", [("FAQ.md", "Q3.4")], "48 hours"),
    # Refund processing time
    ("how long does a refund take to process", [("FAQ.md", "Q3.3")], "3–7 business days"),
    ("refund processing time", [("FAQ.md", "Q3.3")], "3–7 business days"),
    ("when will I receive my refund", [("FAQ.md", "Q3.3")], "3–7 business days"),
    # Cancellation policy
    ("what is your cancellation policy", [("Policies.md", "3. Cancellation Policy"), ("FAQ.md", "Q5.1")], "ship"),
    ("can I cancel my order", [("FAQ.md", "Q5.1")], "until shipped"),
    ("is cancellation possible after shipment", [("Policies.md", "3. Cancellation Policy"), ("FAQ.md", "Q5.1")], "ship"),
    # Shipping charges
    ("what are the shipping charges", [("FAQ.md", "Q2.1")], "₹79"),
    ("how much does shipping cost", [("FAQ.md", "Q2.1")], "₹999"),
    ("is there a shipping fee", [("FAQ.md", "Q2.1")], "₹79"),
]

UNRELATED = [
    "what is the capital of France", "tell me about the moon landing", "do you sell washing machines",
    "Do you sell refrigerators in Antarctica?", "who won the cricket match", "qwertyuiop zxcvbnm",
]


class TestRanking:
    @pytest.mark.parametrize("query,acceptable,fact", CASES)
    def test_best_passage_is_the_right_one_and_contains_the_fact(self, query, acceptable, fact):
        hits = KB.search(query, k=3)
        assert hits, query
        top = (hits[0].chunk.source, hits[0].chunk.section)
        assert top in acceptable, (query, top, [(h.chunk.section, round(h.score, 2)) for h in hits])
        assert fact in hits[0].chunk.text

    def test_the_original_defect_is_fixed(self):
        order = [h.chunk.section for h in KB.search("what is the return window", k=3)]
        assert order.index("Q3.1") < (order.index("Q3.2") if "Q3.2" in order else 99)

    @pytest.mark.parametrize("query", UNRELATED)
    def test_unrelated_questions_return_nothing(self, query):
        assert KB.search(query, k=3) == []

    def test_synonyms_never_outweigh_the_customers_own_words(self):
        from app.rag.retriever import query_weights
        weights = query_weights("return window")
        # The topic word keeps full weight; the generic word and its expansions count half.
        assert weights["return"] == 1.0 and weights["window"] == 0.5
        extras = {term: w for term, w in weights.items() if term not in ("return", "window")}
        assert extras and set(extras.values()) == {0.5}
        # Without a topic word the bridge does not fire at all.
        assert query_weights("window") == {"window": 1.0}

    @pytest.mark.parametrize("query", ["window", "cost", "the window of my bedroom"])
    def test_the_vocabulary_bridge_alone_never_creates_a_match(self, query):
        assert KB.search(query, k=3) == []

    @pytest.mark.parametrize("a,b", [("charge", "charges"), ("cancel", "cancelled"), ("cancel", "cancellation"),
                                     ("return", "returned"), ("refund", "refunds")])
    def test_stemmer_unifies_word_forms(self, a, b):
        from app.rag.retriever import tokenize
        assert tokenize(a) == tokenize(b)


class TestAnswersAreSupportedByTheRetrievedPassage:
    """Through the whole backend: the reply quotes the cited passage and the fact is in it."""

    @pytest.fixture
    def client(self, temp_db):
        return Client(Backend(llm=ScriptedLLM(), settings=Settings()), "C101")

    @pytest.mark.parametrize("query,acceptable,fact", CASES)
    def test_reply_quotes_the_cited_passage(self, client, query, acceptable, fact):
        reply = client.say(query + "?")
        assert reply.status == "ok", reply.text
        assert reply.citations, reply.text
        cited = (reply.citations[0].source, reply.citations[0].section)
        assert cited in acceptable, (query, cited)
        passage = CHUNKS[cited]
        assert passage.text in reply.text  # the answer is the retrieved text, not invented
        assert fact in passage.text and fact in reply.text

    @pytest.mark.parametrize("query", UNRELATED)
    def test_unrelated_question_gets_the_safe_fallback_and_no_citation(self, client, query):
        reply = client.say(query + "?")
        assert reply.citations == ()
        assert reply.status == "ok" and ("can't confirm" in reply.text or "only help" in reply.text)

    def test_a_cancellation_question_is_answered_from_policy_not_refused(self, client):
        reply = client.say("Can I cancel my order?")
        assert "not able to" not in reply.text and "until shipped" in reply.text

    def test_a_cancellation_request_is_still_an_honest_refusal(self, client):
        reply = client.say("please cancel my order ORD1002")
        assert "not able to" in reply.text and "ticket" in reply.text.lower()
