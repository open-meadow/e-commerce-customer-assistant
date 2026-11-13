# Retrieval, embeddings and how they are tested

**Date:** 2026-11-13. Describes the code on the `rohan/voice` branch plus the injectable-embedding testability
refactor described below. This document does not record whether the refactor has been committed; check `git status`
and `git log`.

## 1. Two separate provider requirements

The assistant has two independent provider layers. Do not read "offline" for one as "offline" for the other.

| Layer | Selected by | Needs a key | Offline option |
|---|---|---|---|
| Language model (replies, tool proposals) | `LLM_PROVIDER` (`rule_based`, `openai`, `anthropic`), `LLM_MODEL` | only for `openai` / `anthropic` | `RuleBasedLLM` (a deterministic keyword stand-in, not a language model) |
| Policy / FAQ retrieval (embeddings) | fixed: OpenAI `text-embedding-3-small` through Chroma (`VECTOR_STORE_DIR`) | **always `OPENAI_API_KEY`** | none in production; tests and offline evaluation inject a local embedding |

So with `LLM_PROVIDER=rule_based` the application still needs `OPENAI_API_KEY` to answer policy questions. Orders,
returns, tickets and product lookups need neither key.

## 2. Production behaviour without `OPENAI_API_KEY`

- Importing application modules never needs a key or touches the network (the Chroma index is built lazily on the
  first policy search).
- `default_knowledge_base()` raises `VectorIndexError` ("OPENAI_API_KEY is required for vector embeddings"), a
  `KnowledgeBaseError`. The failure is not cached and nothing is created on disk. There is **no silent fallback to
  BM25**; the lexical `KnowledgeBase` class remains in the module but is not used by the default retriever.
- The tools report `unavailable` (`knowledge_unavailable`) with no passages, and the backend answers that it cannot
  look up the FAQ and policies, offers a ticket, and cites nothing. No policy text is invented.
- Order lookups, returns, tickets and product lookups keep working. `check_backend()` stays `ready` for the database
  and its notes say which embeddings are configured and that production needs `OPENAI_API_KEY`.

## 3. Dependency injection of the embedding function

`ChromaKnowledgeBase(chunks, directory, *, api_key=None, embedding_function=None, max_distance=...)` uses the
OpenAI embedding function when no `embedding_function` is given (key required) and the injected one otherwise. All
other code (collection, persistence, distance cut-off, citation metadata, failure handling) is shared.
`app.rag.configure_embeddings(factory, max_distance=..., label=...)` injects an embedding factory into
`default_knowledge_base()` and returns the previous state for `restore_embeddings`. Nothing in production calls it.
`app.rag.release_default_knowledge_base()` closes the cached Chroma client (needed before a temporary store can be
deleted on Windows).

## 4. The deterministic test embedding

`app/rag/local_embeddings.py` (`LocalDeterministicEmbedding`) gives each passage a vector of BM25 term weights over the
fitted corpus, padded with one balancing coordinate so cosine similarity ranks like the BM25 score, and gives a query
its bridge-weighted term vector. It is exact and identical on every run: no hashing, no randomness, no key, no network,
no model download.

**It is lexical, not semantic.** Passing tests that use it show that indexing, ranking plumbing, the cut-off,
citations and failure handling work. They do **not** show how well OpenAI embeddings retrieve paraphrased questions,
and no offline number should be quoted as embedding quality. It is paired with a looser distance cut-off
(`DEFAULT_MAX_DISTANCE = 0.97`) than production (`MAX_COSINE_DISTANCE = 0.7`, calibrated for OpenAI vectors).

## 5. How the tests use it

- `tests/conftest.py` has an autouse fixture that injects the local embedding with a session-wide temporary Chroma
  folder (`VECTOR_STORE_DIR`), restores production configuration after every test, and removes the folder at the end of
  the run after closing the Chroma clients. `tests/conftest.py` (through `tests/telemetry_guard.py`) forces `ANONYMIZED_TELEMETRY=False` at import time, before any test module imports `chromadb`
  (its native core reads the setting once, so a fixture would be too late), overriding a developer's `True` and restoring
  it at session end; production telemetry is not changed.
- `production_embeddings` (a fixture) switches one test back to the real production configuration with the key removed.
- No test module builds a knowledge base at import time (`tests/backend_helpers.py` `LazyKnowledgeBase`), so test
  collection needs no key. `tests/test_import_without_credentials.py` proves it in fresh processes with the key unset,
  blank, and with `PYTHON_DOTENV_DISABLED=1`, while refusing every network call.
- `tests/test_vector_retrieval.py`: synthetic FAQ/policy passages, ranking, cut-off, persistence and reuse, rebuild on
  change, malformed or failing Chroma results.
- `tests/test_retrieval_production_config.py`: the production missing-key behaviour above.
- The original retrieval, citation, fallback, degradation and readiness tests are kept and run through the same Chroma
  code with the injected embedding.

## 6. Evaluation

`scripts/eval/real_eval.py` always runs retrieval through a temporary Chroma store (never the application's).
`--retrieval local` (default, offline) injects the test embedding; `--retrieval production` uses OpenAI embeddings and
requires `--allow-live` and `OPENAI_API_KEY`; without a key policy questions report retrieval as unavailable. The report's
`retrieval` section names the mode and embeddings, sets `embedding_quality_assessed: false`, and the limitations list
repeats that local retrieval is lexical and says nothing about OpenAI embedding quality. `--compare` shows and compares
both the LLM mode and the retrieval mode of two reports, says explicitly when the retrieval modes differ, and reports
the mode as unknown (never guessed) for an older report without a `retrieval` section.

## 7. Known limits

- Real OpenAI retrieval has never been run in this project; its quality (including paraphrases and the 0.7 cut-off) is
  unmeasured.
- The Streamlit and Chroma telemetry settings are the branch's accepted production configuration and were not changed.
- `create_persistent_index` (kept for compatibility) does not close its client; `ChromaKnowledgeBase.close()` does.
