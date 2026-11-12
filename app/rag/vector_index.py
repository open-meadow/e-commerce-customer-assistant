"""Persistent Chroma index for FAQ and policy chunks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Sequence

import chromadb
from chromadb.api.types import Documents, EmbeddingFunction
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

from app.rag.retriever import Chunk

COLLECTION_NAME = "support_knowledge"
EMBEDDING_MODEL = "text-embedding-3-small"


class VectorIndexError(Exception):
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
    embedding_function: EmbeddingFunction[Documents] | None = None,
) -> chromadb.Collection:
    """Create or refresh the persistent collection for *chunks*.

    ``embedding_function`` is injectable for local callers such as focused
    tests. Normal use requires ``OPENAI_API_KEY`` and uses OpenAI embeddings.
    """
    if not chunks:
        raise VectorIndexError("the vector index requires at least one source chunk")

    if embedding_function is None:
        if not api_key or not api_key.strip():
            raise VectorIndexError("OPENAI_API_KEY is required for vector embeddings")
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
        and collection.metadata.get("source_sha256") == fingerprint
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
