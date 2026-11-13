"""Deterministic, local, offline embeddings for tests and offline evaluation ONLY.

This is a *substitute for the embedding function*, not for Chroma: the retrieval
code (``ChromaKnowledgeBase``, the persistent collection, the distance cut-off,
citation metadata and every failure path) is exactly the code production uses.
Only the step that turns text into vectors is replaced, so no API key, no
network request and no model download is needed.

What it is
    A lexical embedding. Each document becomes a vector of BM25 term weights over
    the vocabulary of the corpus it was fitted on (same tokenizer, stemmer and
    domain vocabulary bridge as the retained lexical ``KnowledgeBase``), padded
    with one balancing coordinate so every document vector has the same length.
    A query becomes its (bridge-weighted) term vector. Cosine similarity between
    the two then ranks documents exactly as the BM25 score does. The vectors are
    exact and identical on every run: no hashing, no randomness.

What it is not
    It is **not semantic** and does not measure, approximate or predict the
    quality of OpenAI embeddings. Passing tests that use it show that the
    application wires retrieval, thresholds, citations and failure handling
    correctly; they say nothing about how well ``text-embedding-3-small``
    retrieves paraphrased questions. Nothing selects it automatically: it is only
    used when a test or an offline evaluation injects it explicitly through
    ``app.rag.retriever.configure_embeddings``.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

from app.rag.retriever import _B, _K1, Chunk, query_weights, tokenize

NAME = "support-local-deterministic-bm25-vectors"
LABEL = "local deterministic BM25-weighted vectors (test/offline only, lexical, not semantic)"
# Cosine-distance cut-off paired with this embedding. Production uses
# ``vector_index.MAX_COSINE_DISTANCE`` (0.7), which is calibrated for OpenAI vectors.
DEFAULT_MAX_DISTANCE = 0.97


class LocalDeterministicEmbedding(EmbeddingFunction[Documents]):
    """BM25-weighted vectors fitted on a corpus; see the module docstring for the limits."""

    def __init__(self, chunks: Sequence[Chunk] = ()) -> None:
        self._titles = {chunk.text: chunk.title for chunk in chunks}
        documents = [tokenize(c.text) + tokenize(c.title) for c in chunks]
        self._average = (sum(len(d) for d in documents) / len(documents)) if documents else 0.0
        frequency: dict[str, int] = {}
        for terms in documents:
            for term in set(terms):
                frequency[term] = frequency.get(term, 0) + 1
        total = len(documents)
        self._idf = {term: math.log(1 + (total - df + 0.5) / (df + 0.5)) for term, df in frequency.items()}
        self._index = {term: i for i, term in enumerate(sorted(self._idf))}
        # Layout: [vocabulary ..., balance, unknown]. ``balance`` equalizes document norms so
        # that cosine ranks like the dot product; ``unknown`` only queries use (nothing matches).
        self._balance = len(self._index)
        self._unknown = self._balance + 1
        self._dimensions = self._balance + 2
        self._raw = [self._weights(text) for text in (c.text for c in chunks)]
        self._max_norm = max((math.sqrt(sum(v * v for v in w.values())) for w in self._raw), default=1.0) or 1.0

    @classmethod
    def fit(cls, chunks: Sequence[Chunk]) -> "LocalDeterministicEmbedding":
        return cls(chunks)

    def _weights(self, text: str) -> dict[int, float]:
        """BM25 term weights of one document (the chunk's title counts as extra text)."""
        terms = tokenize(text) + tokenize(self._titles.get(text, ""))
        length = len(terms) or 1
        weights: dict[int, float] = {}
        for term in set(terms):
            index = self._index.get(term)
            if index is None:
                continue  # a word the corpus was not fitted on
            frequency = terms.count(term)
            norm = frequency * (_K1 + 1) / (
                frequency + _K1 * (1 - _B + _B * length / (self._average or 1)))
            weights[index] = self._idf[term] * norm
        return weights

    def _document(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        weights = self._weights(text)
        for index, value in weights.items():
            vector[index] = value
        norm_squared = sum(v * v for v in weights.values())
        vector[self._balance] = math.sqrt(max(self._max_norm ** 2 - norm_squared, 0.0))
        return vector

    def _query(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for term, weight in query_weights(text).items():
            index = self._index.get(term)
            if index is not None:
                vector[index] = weight
        if not any(vector):
            vector[self._unknown] = 1.0  # no known word: orthogonal to every document
        return vector

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 - Chroma's parameter name
        return [self._document(text) for text in input]

    def embed_query(self, input: Documents) -> Embeddings:  # noqa: A002
        return [self._query(text) for text in input]

    @staticmethod
    def name() -> str:
        return NAME

    def default_space(self) -> str:  # type: ignore[override]
        return "cosine"

    def supported_spaces(self) -> list[str]:  # type: ignore[override]
        return ["cosine", "l2", "ip"]

    def get_config(self) -> dict[str, Any]:
        return {"kind": "bm25-weighted-vectors"}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "LocalDeterministicEmbedding":
        # The vectors depend on the corpus this embedding was fitted on, which a stored
        # configuration does not contain. Returning an unfitted instance would silently make
        # every query match nothing, so refuse explicitly.
        raise NotImplementedError(
            "LocalDeterministicEmbedding cannot be rebuilt from stored configuration: it is fitted on a "
            "corpus. Create it with LocalDeterministicEmbedding.fit(chunks) and pass that instance."
        )

    def is_legacy(self) -> bool:
        """Chroma stores such a function as "legacy" (not rebuildable from stored configuration), so it
        must be handed this fitted instance each time. Declared here so Chroma does not have to call
        ``build_from_config`` (which refuses) just to find that out."""
        return True

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        return None


def factory(chunks: Sequence[Chunk]) -> LocalDeterministicEmbedding:
    """The callable ``configure_embeddings`` expects: chunks in, embedding function out."""
    return LocalDeterministicEmbedding.fit(chunks)
