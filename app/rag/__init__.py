"""Retrieval over the FAQ and policy documents using persistent Chroma vectors."""

from app.rag.retriever import (
    Chunk,
    KnowledgeBase,
    KnowledgeBaseError,
    SearchHit,
    default_knowledge_base,
    load_chunks,
)

__all__ = ["Chunk", "KnowledgeBase", "KnowledgeBaseError", "SearchHit", "default_knowledge_base",
           "load_chunks"]
