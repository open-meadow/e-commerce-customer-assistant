"""Closing a Chroma knowledge base must release its files before the folder is deleted (Windows).

Root cause of the intermittent ``PermissionError [WinError 32]`` on ``chroma.sqlite3`` in
``tests/test_real_eval.py``: ``Client.close()`` stops Chroma's system, but the Rust-backed objects that hold the
SQLite connections are only released when Python's garbage collector runs, so deleting the folder straight after
``close()`` fails intermittently. ``ChromaKnowledgeBase.close()`` therefore also collects garbage.

Local deterministic embeddings and temporary folders only; no network and no protected path.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from app.rag import local_embeddings
from app.rag import vector_index
from app.rag.retriever import load_chunks
from app.rag.vector_index import ChromaKnowledgeBase

CYCLES = 30
SEARCHES = 30


def _open(folder: Path):
    chunks = load_chunks()
    return ChromaKnowledgeBase(chunks, folder / "vector-store", embedding_function=local_embeddings.factory(chunks),
                               max_distance=local_embeddings.DEFAULT_MAX_DISTANCE)


def test_close_collects_chroma_objects_after_closing_the_client(monkeypatch, tmp_path):
    """Deterministic: the files are only released once the objects are collected, so close() must collect."""
    order: list[str] = []
    knowledge_base = _open(tmp_path)
    real_close = knowledge_base._client.close
    monkeypatch.setattr(knowledge_base._client, "close", lambda: (order.append("client.close"), real_close())[1])
    monkeypatch.setattr(vector_index.gc, "collect", lambda *a, **k: order.append("gc.collect") or 0)
    knowledge_base.close()
    assert order == ["client.close", "gc.collect"]


def test_close_is_idempotent(tmp_path):
    knowledge_base = _open(tmp_path)
    knowledge_base.close()
    knowledge_base.close()


def test_the_folder_can_be_removed_straight_after_close_every_time(tmp_path):
    """Repeated open, search, close, delete cycles: no cleanup error may ever occur (before the fix about one in
    ten cycles failed on Windows with WinError 32). Teardown errors are not suppressed or retried."""
    failures = []
    for cycle in range(CYCLES):
        folder = Path(tempfile.mkdtemp(prefix="release-", dir=tmp_path))
        knowledge_base = _open(folder)
        for n in range(SEARCHES):
            knowledge_base.search(f"how long do I have to return an item {n}", k=3)
        knowledge_base.close()
        try:
            shutil.rmtree(folder)
        except OSError as error:
            failures.append((cycle, repr(error)))
    assert failures == []
