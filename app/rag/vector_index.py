"""Persistent Chroma index for FAQ and policy chunks."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Sequence

import chromadb
from chromadb.errors import ChromaError
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
from openai import APIError

from app.rag.retriever import Chunk, KnowledgeBaseError, SearchHit

COLLECTION_NAME = "support_knowledge"
EMBEDDING_MODEL = "text-embedding-3-small"
MAX_COSINE_DISTANCE = 0.7


class VectorIndexError(KnowledgeBaseError):
    """The vector index cannot be created or used as configured."""


def _source_fingerprint(chunks: Sequence[Chunk]) -> str:
    content = [
        (chunk.source, chunk.section, chunk.title, chunk.text)
        for chunk in chunks
    ]
    serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _chunk_id(chunk: Chunk) -> str:
    identity = f"{chunk.source}\0{chunk.section}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def create_persistent_index(
    chunks: Sequence[Chunk],
    persist_directory: Path,
    *,
    api_key: str | None,
) -> chromadb.Collection:
    """Create or refresh the persistent collection for *chunks*.

    Indexing requires ``OPENAI_API_KEY`` and uses OpenAI embeddings.
    """
    if not chunks:
        raise VectorIndexError("the vector index requires at least one source chunk")
    if not api_key or not api_key.strip():
        raise VectorIndexError("OPENAI_API_KEY is required for vector embeddings")

    try:
        embedding_function = OpenAIEmbeddingFunction(
            api_key=api_key,
            model_name=EMBEDDING_MODEL,
        )
        client = chromadb.PersistentClient(path=str(persist_directory))
        fingerprint = _source_fingerprint(chunks)
        metadata = {
            "hnsw:space": "cosine",
            "source_sha256": fingerprint,
            "embedding_model": EMBEDDING_MODEL,
        }
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=embedding_function,
            metadata=metadata,
        )

        if (
            collection.metadata is not None
            and all(collection.metadata.get(key) == value for key, value in metadata.items())
            and collection.count() == len(chunks)
        ):
            return collection

        client.delete_collection(name=COLLECTION_NAME)
        collection = client.create_collection(
            name=COLLECTION_NAME,
            embedding_function=embedding_function,
            metadata=metadata,
        )
        collection.add(
            ids=[_chunk_id(chunk) for chunk in chunks],
            documents=[chunk.text for chunk in chunks],
            metadatas=[
                {
                    "source": chunk.source,
                    "section": chunk.section,
                    "title": chunk.title,
                }
                for chunk in chunks
            ],
        )
        return collection
    except (APIError, ChromaError, OSError) as exc:
        raise VectorIndexError("the FAQ and policy vector index is unavailable") from exc


class ChromaKnowledgeBase:
    """Search FAQ and policy chunks using their persistent Chroma embeddings."""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        persist_directory: Path,
        *,
        api_key: str | None,
    ) -> None:
        self._collection = create_persistent_index(
            chunks,
            persist_directory,
            api_key=api_key,
        )

    def search(self, query: str, k: int = 3) -> list[SearchHit]:
        if not isinstance(query, str) or not query.strip() or k <= 0:
            return []

        try:
            count = self._collection.count()
            if count == 0:
                return []
            result = self._collection.query(
                query_texts=[query],
                n_results=min(k, count),
                include=["documents", "metadatas", "distances"],
            )
        except (APIError, ChromaError, OSError) as exc:
            raise VectorIndexError("policy vector search is unavailable") from exc

        documents = result["documents"]
        metadatas = result["metadatas"]
        distances = result["distances"]
        if documents is None or metadatas is None or distances is None:
            raise VectorIndexError("policy vector search returned incomplete results")
        if len(documents) != 1 or len(metadatas) != 1 or len(distances) != 1:
            raise VectorIndexError("policy vector search returned invalid results")
        document_batch = documents[0]
        metadata_batch = metadatas[0]
        distance_batch = distances[0]
        if document_batch is None or metadata_batch is None or distance_batch is None:
            raise VectorIndexError("policy vector search returned incomplete results")
        if not document_batch:
            return []
        if not (
            len(document_batch) == len(metadata_batch) == len(distance_batch)
        ):
            raise VectorIndexError("policy vector search returned invalid results")

        hits: list[SearchHit] = []
        for document, metadata, distance in zip(
            document_batch,
            metadata_batch,
            distance_batch,
            strict=True,
        ):
            if (
                not isinstance(document, str)
                or metadata is None
                or not isinstance(distance, (int, float))
                or not math.isfinite(distance)
            ):
                raise VectorIndexError("policy vector search returned invalid results")

            source = metadata.get("source")
            section = metadata.get("section")
            title = metadata.get("title")
            if (
                not isinstance(source, str)
                or not isinstance(section, str)
                or not isinstance(title, str)
            ):
                raise VectorIndexError("policy vector search returned invalid source metadata")
            if distance <= MAX_COSINE_DISTANCE:
                hits.append(
                    SearchHit(
                        Chunk(source, section, title, document),
                        1.0 - distance,
                    )
                )

        hits.sort(key=lambda hit: (-hit.score, hit.chunk.source, hit.chunk.section))
        return hits
