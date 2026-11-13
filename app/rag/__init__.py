"""Retrieval over the FAQ and policy documents using persistent Chroma vectors."""

from app.rag.retriever import (
    Chunk,
    KnowledgeBase,
    KnowledgeBaseError,
    SearchHit,
    configure_embeddings,
    default_knowledge_base,
    embedding_configuration,
    load_chunks,
    release_default_knowledge_base,
    restore_embeddings,
)

__all__ = ["Chunk", "KnowledgeBase", "KnowledgeBaseError", "SearchHit", "configure_embeddings",
           "default_knowledge_base", "embedding_configuration", "load_chunks", "release_default_knowledge_base", "restore_embeddings"]
