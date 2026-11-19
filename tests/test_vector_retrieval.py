"""The Chroma retrieval implementation, exercised with synthetic documents and a deterministic
local embedding function (no API key, no network, temporary storage).

Production uses ``OpenAIEmbeddingFunction``; these tests inject ``app.rag.local_embeddings`` through the
same ``ChromaKnowledgeBase`` code path. They prove indexing, ranking, the distance cut-off, citation
metadata, persistence and failure handling. They do NOT measure OpenAI embedding quality.
"""

from __future__ import annotations

import pytest
from chromadb.errors import ChromaError

from app.rag import local_embeddings
from app.rag.retriever import Chunk
from app.rag.vector_index import (EMBEDDING_MODEL, MAX_COSINE_DISTANCE, ChromaKnowledgeBase, VectorIndexError,
                                  create_persistent_index)

# Synthetic FAQ and policy passages (not the store's real documents).
FAQ_REFUND = Chunk("FAQ.md", "S1", "How long do refunds take?",
                   "How long do refunds take? Refunds are issued within 5 business days after the parcel is inspected.")
FAQ_TRACK = Chunk("FAQ.md", "S2", "How can I track my parcel?",
                  "How can I track my parcel? Open the tracking page and enter your order number.")
POLICY_WARRANTY = Chunk("Policies.md", "P1. Warranty", "P1. Warranty",
                        "P1. Warranty. Electronics carry a twelve month manufacturer warranty against defects.")
POLICY_PRIVACY = Chunk("Policies.md", "P2. Privacy", "P2. Privacy",
                       "P2. Privacy. Customer records are used only to fulfil orders and are never sold.")
CORPUS = [FAQ_REFUND, FAQ_TRACK, POLICY_WARRANTY, POLICY_PRIVACY]


def build(chunks, folder, **options):
    options.setdefault("max_distance", local_embeddings.DEFAULT_MAX_DISTANCE)
    return ChromaKnowledgeBase(chunks, folder, embedding_function=local_embeddings.factory(chunks), **options)


@pytest.fixture
def kb(tmp_path):
    knowledge_base = build(CORPUS, tmp_path / "store")
    try:
        yield knowledge_base
    finally:
        knowledge_base.close()


class TestRanking:
    def test_the_relevant_faq_passage_is_first_with_its_citation(self, kb):
        hits = kb.search("how long do refunds take", k=3)
        top = hits[0]
        assert (top.chunk.source, top.chunk.section, top.chunk.title) == ("FAQ.md", "S1", FAQ_REFUND.title)
        assert top.chunk.text == FAQ_REFUND.text  # the stored passage, verbatim, is what is returned
        assert 0.0 < top.score <= 1.0

    def test_the_relevant_policy_passage_is_found(self, kb):
        hits = kb.search("what warranty do electronics have", k=3)
        assert (hits[0].chunk.source, hits[0].chunk.section) == ("Policies.md", "P1. Warranty")

    def test_results_are_best_first_and_repeatable(self, kb):
        first = kb.search("refunds and parcel tracking", k=4)
        second = kb.search("refunds and parcel tracking", k=4)
        assert [(h.chunk.section, h.score) for h in first] == [(h.chunk.section, h.score) for h in second]
        assert [h.score for h in first] == sorted((h.score for h in first), reverse=True)

    def test_k_limits_the_result_count(self, kb):
        assert len(kb.search("refunds warranty parcel privacy", k=1)) == 1

    @pytest.mark.parametrize("query", ["qwertyuiop zxcvbnm", "tell me about the moon landing", "", "   ", "?!?!"])
    def test_unrelated_or_empty_queries_return_nothing(self, kb, query):
        assert kb.search(query, k=3) == []

    @pytest.mark.parametrize("value", [None, 5, ["refund"], b"refund"])
    def test_non_string_queries_return_nothing(self, kb, value):
        assert kb.search(value, k=3) == []

    @pytest.mark.parametrize("k", [0, -1])
    def test_a_non_positive_k_returns_nothing(self, kb, k):
        assert kb.search("refunds", k=k) == []

    def test_the_distance_cutoff_is_injectable_and_enforced(self, tmp_path):
        strict = build(CORPUS, tmp_path / "strict", max_distance=0.0)
        try:
            assert strict.search("how long do refunds take", k=3) == []  # no match is exactly identical
        finally:
            strict.close()
        assert MAX_COSINE_DISTANCE == 0.7  # production calibration is unchanged


class TestInjectedEmbeddings:
    def test_the_embedding_is_deterministic_and_local(self):
        import numpy as np

        function = local_embeddings.factory(CORPUS)
        assert np.array_equal(function([FAQ_REFUND.text]), function([FAQ_REFUND.text]))
        assert np.array_equal(function.embed_query(["refund timeline"]), function.embed_query(["refund timeline"]))
        assert len(function([FAQ_REFUND.text])[0]) == len(function.embed_query(["anything"])[0])

    def test_rebuilding_the_embedding_from_stored_config_fails_loudly(self):
        """An unfitted instance would silently match nothing, so reconstruction is refused explicitly."""
        with pytest.raises(NotImplementedError, match="fitted on a corpus"):
            local_embeddings.LocalDeterministicEmbedding.build_from_config(
                local_embeddings.factory(CORPUS).get_config())
        # The normal injected path is unaffected: a fitted instance works and finds passages.
        fitted = local_embeddings.LocalDeterministicEmbedding.fit(CORPUS)
        assert len(fitted([FAQ_REFUND.text])) == 1 and fitted.is_legacy() is True

    def test_no_key_and_no_openai_object_are_needed(self, tmp_path, monkeypatch):
        import app.rag.vector_index as vector_index

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        def forbidden(*args, **kwargs):
            raise AssertionError("the OpenAI embedding function must not be built when one is injected")

        monkeypatch.setattr(vector_index, "OpenAIEmbeddingFunction", forbidden)
        knowledge_base = build(CORPUS, tmp_path / "no-key")
        try:
            assert knowledge_base.search("parcel tracking", k=1)
            assert knowledge_base.embedding_label == f"injected:{local_embeddings.NAME}"
            assert knowledge_base.embedding_label != EMBEDDING_MODEL  # never mistaken for the production model
        finally:
            knowledge_base.close()

    def test_the_index_is_stored_and_reused_without_re_embedding(self, tmp_path):
        class Counting(local_embeddings.LocalDeterministicEmbedding):
            documents_embedded = 0

            def __call__(self, input):  # noqa: A002 - Chroma's parameter name
                type(self).documents_embedded += len(input)
                return super().__call__(input)

        folder = tmp_path / "persist"
        first = ChromaKnowledgeBase(CORPUS, folder, embedding_function=Counting(CORPUS),
                                    max_distance=local_embeddings.DEFAULT_MAX_DISTANCE)
        first.close()
        assert Counting.documents_embedded == len(CORPUS)  # indexing embedded every passage once
        Counting.documents_embedded = 0
        again = ChromaKnowledgeBase(CORPUS, folder, embedding_function=Counting(CORPUS),
                                    max_distance=local_embeddings.DEFAULT_MAX_DISTANCE)
        try:
            assert again._collection.count() == len(CORPUS)
            assert Counting.documents_embedded == 0  # the stored vectors were reused, nothing re-embedded
            assert again.search("parcel tracking", k=1)
        finally:
            again.close()

    def test_the_index_is_rebuilt_when_the_source_text_changes(self, tmp_path):
        folder = tmp_path / "changing"
        build(CORPUS, folder).close()
        changed = [Chunk("FAQ.md", "S1", FAQ_REFUND.title,
                         "How long do refunds take? Refunds now take exactly nine business days."), *CORPUS[1:]]
        rebuilt = build(changed, folder)
        try:
            assert "nine business days" in rebuilt.search("how long do refunds take", k=1)[0].chunk.text
        finally:
            rebuilt.close()

    def test_an_empty_corpus_is_refused(self, tmp_path):
        with pytest.raises(VectorIndexError, match="at least one"):
            ChromaKnowledgeBase([], tmp_path / "empty", embedding_function=local_embeddings.factory(CORPUS))


class _Collection:
    """A stand-in for the Chroma collection returning chosen (possibly malformed) results."""

    def __init__(self, result=None, error=None, count=2):
        self._result, self._error, self._count = result, error, count

    def count(self):
        return self._count

    def query(self, **kwargs):
        if self._error:
            raise self._error
        return self._result


def _good(distance=0.1):
    return {"documents": [["text"]], "metadatas": [[{"source": "FAQ.md", "section": "S1", "title": "t"}]],
            "distances": [[distance]]}


class TestRetrievalFailuresAreTyped:
    """Search never returns partial or invented results; it raises a typed, user-safe error."""

    @pytest.fixture
    def knowledge_base(self, tmp_path):
        knowledge_base = build(CORPUS, tmp_path / "failures")
        try:
            yield knowledge_base
        finally:
            knowledge_base.close()

    def use(self, knowledge_base, collection):
        knowledge_base._collection = collection
        return knowledge_base

    def test_a_chroma_error_becomes_a_vector_index_error(self, knowledge_base):
        broken = _Collection(error=ChromaError("storage failure with a path C:/secret"))
        with pytest.raises(VectorIndexError) as raised:
            self.use(knowledge_base, broken).search("refunds")
        assert "C:/secret" not in str(raised.value)  # the underlying detail is not surfaced

    def test_an_empty_collection_returns_nothing(self, knowledge_base):
        assert self.use(knowledge_base, _Collection(count=0)).search("refunds") == []

    @pytest.mark.parametrize("result", [
        {"documents": None, "metadatas": [[{}]], "distances": [[0.1]]},
        {"documents": [["a"], ["b"]], "metadatas": [[{}]], "distances": [[0.1]]},
        {"documents": [["a", "b"]], "metadatas": [[{"source": "s", "section": "x", "title": "t"}]],
         "distances": [[0.1]]},
        {"documents": [["a"]], "metadatas": [[None]], "distances": [[0.1]]},
        {"documents": [["a"]], "metadatas": [[{"source": "FAQ.md", "section": "S1", "title": "t"}]],
         "distances": [[float("nan")]]},
        {"documents": [["a"]], "metadatas": [[{"source": 5, "section": "S1", "title": "t"}]], "distances": [[0.1]]},
        {"documents": [[7]], "metadatas": [[{"source": "FAQ.md", "section": "S1", "title": "t"}]],
         "distances": [[0.1]]},
    ])
    def test_malformed_results_are_rejected(self, knowledge_base, result):
        with pytest.raises(VectorIndexError):
            self.use(knowledge_base, _Collection(result=result)).search("refunds")

    def test_a_distant_match_is_dropped_not_cited(self, knowledge_base):
        assert self.use(knowledge_base, _Collection(result=_good(distance=0.99))).search("refunds") == []

    def test_a_close_match_is_returned_with_its_metadata(self, knowledge_base):
        hit = self.use(knowledge_base, _Collection(result=_good(0.2))).search("refunds")[0]
        assert (hit.chunk.source, hit.chunk.section, hit.chunk.title) == ("FAQ.md", "S1", "t")
        assert hit.score == pytest.approx(0.8)

    def test_creating_the_index_failure_closes_nothing_and_raises_typed(self, tmp_path, monkeypatch):
        import app.rag.vector_index as vector_index

        def boom(*args, **kwargs):
            raise ChromaError("cannot open the store")

        monkeypatch.setattr(vector_index.chromadb, "PersistentClient", boom)
        with pytest.raises(VectorIndexError, match="unavailable"):
            create_persistent_index(CORPUS, tmp_path / "x", embedding_function=local_embeddings.factory(CORPUS))
