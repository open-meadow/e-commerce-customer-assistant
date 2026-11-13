"""Persistent Chroma index for FAQ and policy chunks."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Optional, Sequence

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


def _embedding_label(embedding_function: Any) -> str:
    try:
        return f"injected:{embedding_function.name()}"
    except Exception:  # noqa: BLE001 - a label only; never let it break indexing
        return f"injected:{type(embedding_function).__name__}"


def create_persistent_index(
    chunks: Sequence[Chunk],
    persist_directory: Path,
    *,
    api_key: str | None = None,
    embedding_function: Optional[Any] = None,
) -> chromadb.Collection:
    """Create or refresh the persistent collection for *chunks* (see ``_open_index``)."""
    return _open_index(
        chunks, persist_directory, api_key=api_key, embedding_function=embedding_function
    )[1]


def _open_index(
    chunks: Sequence[Chunk],
    persist_directory: Path,
    *,
    api_key: str | None = None,
    embedding_function: Optional[Any] = None,
) -> tuple[Any, chromadb.Collection]:
    """Create or refresh the persistent collection for *chunks*; return ``(client, collection)``.

    Production (``embedding_function`` omitted) requires ``OPENAI_API_KEY`` and uses
    the OpenAI embedding model; a missing key raises ``VectorIndexError`` and nothing
    else is substituted. ``embedding_function`` lets a caller inject a different
    Chroma embedding function (tests and offline evaluation pass a local
    deterministic one from ``app.rag.local_embeddings``); the collection, storage
    and every other step are identical. No key is needed or read in that case.
    """
    if not chunks:
        raise VectorIndexError("the vector index requires at least one source chunk")
    if embedding_function is None and (not api_key or not api_key.strip()):
        raise VectorIndexError("OPENAI_API_KEY is required for vector embeddings")

    client = None
    try:
        if embedding_function is None:
            embedding_function = OpenAIEmbeddingFunction(
                api_key=api_key,
                model_name=EMBEDDING_MODEL,
            )
            label = EMBEDDING_MODEL
        else:
            label = _embedding_label(embedding_function)
        client = chromadb.PersistentClient(path=str(persist_directory))
        fingerprint = _source_fingerprint(chunks)
        metadata = {
            "hnsw:space": "cosine",
            "source_sha256": fingerprint,
            "embedding_model": label,
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
            return client, collection

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
        return client, collection
    except (APIError, ChromaError, OSError) as exc:
        if client is not None:
            try:  # do not leak the client (and its file handles) when indexing fails
                client.close()
            except Exception:  # noqa: BLE001
                pass
        raise VectorIndexError("the FAQ and policy vector index is unavailable") from exc


class ChromaKnowledgeBase:
    """Search FAQ and policy chunks using their persistent Chroma embeddings."""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        persist_directory: Path,
        *,
        api_key: str | None = None,
        embedding_function: Optional[Any] = None,
        max_distance: float = MAX_COSINE_DISTANCE,
    ) -> None:
        self._client, self._collection = _open_index(
            chunks,
            persist_directory,
            api_key=api_key,
            embedding_function=embedding_function,
        )
        self._max_distance = max_distance
        self.embedding_label = (
            EMBEDDING_MODEL if embedding_function is None else _embedding_label(embedding_function)
        )

    def close(self) -> None:
        """Release the Chroma client's file handles (needed before deleting a temporary store on Windows)."""
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

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
            if distance <= self._max_distance:
                hits.append(
                    SearchHit(
                        Chunk(source, section, title, document),
                        1.0 - distance,
                    )
                )

        hits.sort(key=lambda hit: (-hit.score, hit.chunk.source, hit.chunk.section))
        return hits
