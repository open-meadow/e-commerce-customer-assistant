"""The PRODUCTION retrieval configuration: OpenAI embeddings through Chroma, which need OPENAI_API_KEY.

These tests use the ``production_embeddings`` fixture (no injected embedding, key removed). They show that
without the key the policy capability reports itself unavailable, never invents policy text or citations, and
that everything that does not need retrieval keeps working. The LLM and the embeddings have separate provider
requirements: RuleBasedLLM needs no key, retrieval always does. No request to any service is ever made.
"""

from __future__ import annotations

import pytest

from app import rag
from app.backend import Backend, Settings
from app.rag import local_embeddings
from app.rag.retriever import KnowledgeBaseError, load_chunks
from app.rag.vector_index import EMBEDDING_MODEL, ChromaKnowledgeBase, VectorIndexError, _open_index
from app.tools import CustomerContext, execute_tool
from backend_helpers import Client, ScriptedLLM

POLICY_QUESTIONS = ["How long do refunds take?", "What is your refund policy?", "What is the return window?",
                    "How can I contact customer support?"]


class TestProductionInitialization:
    def test_the_configuration_says_it_needs_the_key(self, production_embeddings):
        description = rag.embedding_configuration()
        assert "openai" in description and "requires OPENAI_API_KEY" in description
        assert "injected" not in description  # the test embedding is not active here

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_a_missing_or_blank_key_fails_gracefully(self, production_embeddings, monkeypatch, value):
        if value is not None:
            monkeypatch.setenv("OPENAI_API_KEY", value)
        with pytest.raises(VectorIndexError, match="OPENAI_API_KEY is required") as raised:
            rag.default_knowledge_base()
        assert isinstance(raised.value, KnowledgeBaseError)  # the same typed error every caller already handles

    def test_there_is_no_silent_fallback_to_lexical_search(self, production_embeddings):
        with pytest.raises(KnowledgeBaseError):
            rag.default_knowledge_base()  # not a BM25 object, not an empty one: an error
        assert rag.default_knowledge_base.cache_info().currsize == 0  # and the failure is not cached

    def test_nothing_is_created_on_disk_without_a_key(self, production_embeddings, tmp_path):
        with pytest.raises(VectorIndexError):
            rag.default_knowledge_base()
        assert not (tmp_path / "production-store").exists()

    def test_with_a_key_the_openai_embedding_function_is_what_gets_used(self, tmp_path, monkeypatch):
        """Production wiring, with the OpenAI class replaced by a local one so nothing leaves the machine."""
        import app.rag.vector_index as vector_index

        seen = {}
        chunks = load_chunks()

        def fake_openai(api_key, model_name):
            seen["api_key"], seen["model"] = api_key, model_name
            return local_embeddings.factory(chunks)

        monkeypatch.setattr(vector_index, "OpenAIEmbeddingFunction", fake_openai)
        client, collection = _open_index(chunks, tmp_path / "wired", api_key="placeholder-not-a-real-key")
        try:
            assert seen == {"api_key": "placeholder-not-a-real-key", "model": EMBEDDING_MODEL}
            assert collection.metadata["embedding_model"] == EMBEDDING_MODEL  # labelled as the production model
        finally:
            client.close()


class TestUnavailableMeansUnavailable:
    ctx = CustomerContext("C101", "ref")

    def test_the_policy_tool_reports_unavailability_with_no_passages(self, production_embeddings):
        result = execute_tool(self.ctx, "search_policies", {"query": "how long do refunds take"})
        assert (result.status, result.code) == ("unavailable", "knowledge_unavailable")
        assert result.data == {} and "cannot confirm" in result.note

    def test_the_refund_tool_without_an_order_is_unavailable(self, production_embeddings):
        result = execute_tool(self.ctx, "get_refund_policy", {})
        assert (result.status, result.code) == ("unavailable", "knowledge_unavailable")

    def test_the_refund_tool_with_an_order_gives_only_the_order_facts(self, production_embeddings, temp_db):
        result = execute_tool(self.ctx, "get_refund_policy", {"order_id": "ORD1001"})
        assert result.status == "ok" and result.data["passages"] == []
        assert result.data["policy_unavailable"] is True and result.data["order"]["order_id"] == "ORD1001"

    def test_non_policy_tools_keep_working(self, production_embeddings, temp_db):
        for name, arguments in (("list_my_orders", {}), ("get_order_status", {"order_id": "ORD1001"}),
                                ("get_my_returns", {}), ("get_my_tickets", {}),
                                ("lookup_product", {"query": "earbuds"})):
            assert execute_tool(self.ctx, name, arguments).status == "ok", name


class TestTheBackendDegradesHonestly:
    @pytest.fixture
    def client(self, production_embeddings, temp_db):
        # RuleBasedLLM-based, offline LLM: it still cannot answer policy questions without embeddings.
        return Client(Backend(llm=ScriptedLLM(), settings=Settings()), "C101")

    @pytest.mark.parametrize("question", POLICY_QUESTIONS)
    def test_a_policy_question_is_not_answered_and_cites_nothing(self, client, question):
        reply = client.say(question)
        assert reply.status == "ok" and reply.citations == ()
        assert "can't look up our FAQ and policies" in reply.text
        for invented in ("7 days", "business days", "48 hours", "₹", "support@example.com"):
            assert invented not in reply.text, (question, invented)

    def test_order_lookup_keeps_working(self, client):
        reply = client.say("Where is my order ORD1001?")
        assert reply.status == "ok" and "Delivered" in reply.text and reply.citations == ()

    def test_a_refund_question_about_an_order_still_gives_the_order_facts_but_no_policy(self, client):
        reply = client.say("what is the refund status of ORD1001?")
        assert reply.status == "ok" and "ORD1001" in reply.text and reply.citations == ()

    def test_returns_and_tickets_are_unaffected(self, client):
        assert client.say("show my returns").status == "ok"
        assert client.say("show my tickets").status == "ok"

    def test_readiness_states_the_embedding_requirement_separately_from_the_llm(self, production_embeddings,
                                                                               temp_db):
        health = Backend(llm=ScriptedLLM(), settings=Settings()).check_backend()
        assert health.status == "ready"  # the database is fine
        notes = " ".join(health.notes)
        assert "requires OPENAI_API_KEY" in notes and "Embeddings:" in notes


class TestRestoration:
    def test_the_injected_test_embedding_is_active_in_ordinary_tests(self):
        assert "injected" in rag.embedding_configuration()
        assert isinstance(rag.default_knowledge_base(), ChromaKnowledgeBase)

    def test_configure_embeddings_returns_the_previous_state_for_restoration(self):
        previous = rag.configure_embeddings(None)
        try:
            assert "requires OPENAI_API_KEY" in rag.embedding_configuration()
        finally:
            rag.restore_embeddings(previous)
        assert "injected" in rag.embedding_configuration()
