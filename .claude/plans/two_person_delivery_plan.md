# Two-person delivery plan, derived from docs/03_backend_architecture.md

**Person A (backend and AI):** Rukum. **Person B (frontend and delivery):** teammate.
Base branch: `master` at `01b82af` (includes the database layer, `docs/03`, and Person B's first Streamlit UI work).

## Context
`docs/03_backend_architecture.md` (Revision 3.2) is a design for one engineer and is too large for the ~3 days left before ~2026-11-13. This plan cuts it to an MVP and splits it into two workstreams that touch **different directories**, joined by one frozen interface (`app.backend`) so they can run in parallel.

State of `master` when this plan was written:
- Done: the database layer in `app/db/` (schema, connection, seed, repository, 497 tests recorded historically); `docs/03` committed; `streamlit>=1.67.0` added to `pyproject.toml`.
- Started by Person B: `app/ui/` (`streamlit_app.py`, `session.py`, `client.py`) with a simulated customer selector, a chat transcript and **stub replies**. `app/ui/client.py::handle_turn(authenticated_customer_id, message, history) -> str` is a placeholder. It does not match the docs/03 contract below and is to be replaced by calls to `app.backend`.
- Not built: tools, backend service, RAG, LLM adapter, orchestrator, evaluation set, Docker, README setup, report, deck.

Decisions taken: MVP cut; provider-agnostic LLM adapter (provider chosen later by the Technical Lead); split Backend and AI vs Frontend and Delivery.

## MVP cut (what docs/03 keeps and defers)
Keep: `CustomerContext` and strict tool schemas; `app.backend` entry points; in-memory session registry and pending-action store (single process, one lock, atomic claim, expiry, replay protection); customer-selected return category; confirmation for all writes; return prechecks; `get_my_tickets`; minimal deterministic scope guard; honest unsupported-operation replies; RAG over `FAQ.md` and `Policies.md`; product lookup from `Products.json`.
Defer (document as limitations in the report): durable store and idempotency, write-capacity reservation (keep a simple committed counter), read-only health connection and demo-readiness diagnostic, clarification-context promotion, optional classifier, absolute session lifetime.

## Gate 0 (both people, first, about 1 hour)
1. Each person works on their own branch cut from `master`: Person A `feat/backend`, Person B `feat/delivery`. Merge to `master` through pull requests, rebasing first.
2. Person A writes and pushes the **frozen contract** (types and signatures only, plus a `FakeBackend` returning canned replies): `app/backend/types.py` and `app/backend/api.py` with `list_login_options()`, `start_session(customer_id)`, `handle_message(state, message=None, *, decision=None, choice=None) -> ChatReply`, and the `PendingView`, `ChoiceView`, `ConfirmationDecision`, `ChoiceSelection` types from docs/03 Section 4. After this, signature changes need both people's agreement.
3. Person B retargets `app/ui` from `client.handle_turn(...)` to the contract above (the UI never passes a customer ID into message handling; identity comes from `start_session`).
4. Dependency ownership, to avoid `pyproject.toml` and `uv.lock` conflicts: A adds LLM, RAG and embedding packages; B adds UI and evaluation packages. Rebase before each commit and regenerate `uv.lock`, never hand-edit it.
5. Decide the `reason_category` path (open decision below).

## Person A: backend and AI (owns `app/db`, `app/backend`, `app/tools`, `app/llm`, `app/rag`, `app/orchestrator`, and their tests)
- **A1 Database follow-ups:** `reason_category` change or deferral (see decision); add read-only `check_return_eligibility` to `app/db/repository.py` sharing the window table with `create_return_request`; add `list_customers_for_login` (login selector). Reuse existing helpers and test fixtures in `tests/test_returns.py`. All 290 existing return and ticket tests stay unchanged.
- **A2 Tool layer (`app/tools`):** `CustomerContext`, input sanitization and the approved D4 limits (1,000 / 4,000 / 2,000 / 100), `ToolResult`, tools: `list_my_orders`, `get_order_status`, `get_my_returns`, `get_my_tickets`, `start_return_request`, `start_support_ticket`, `get_refund_policy`, `lookup_product`. Catalog loader for `Products.json` (rupees to paise). Map `sqlite3.Error` and `ConnectionError_` to `unavailable`. Reuse the `app/db/repository.py` reads; never return stored free text to the LLM.
- **A3 Backend service (`app/backend`):** session registry and pending-action store under one process-global lock; atomic claim before write; confirmation, expiry, replay handling; outcome-to-message templates (docs/03 Sections 7.5, 8.6, 10); no placeholder contact address in any template; caps and counters; deterministic scope guard (greetings, out-of-scope redirect, ambiguous prompt). Replace `FakeBackend` with the real implementation behind the same signatures.
- **A4 RAG (`app/rag`):** loader and chunker for `data/source/FAQ.md` and `Policies.md`, embeddings, vector store with persistence under a configurable directory, retrieval with citations, `search_policies` tool.
- **A5 Orchestrator and LLM adapter (`app/llm`, `app/orchestrator`):** thin provider interface plus one adapter chosen by the Technical Lead; system prompt; tool-calling loop with the 6-calls-per-turn cap; fallback and human escalation to `start_support_ticket`; a deterministic fake LLM for tests.
- **A6 Tests:** per docs/03 Section 16 for the kept features, using temp databases and the existing `sqlite3.connect` guard pattern. No test opens `data/app.db`.

## Person B: frontend and delivery (owns `app/ui`, Docker files, `data/eval`, `scripts`, README, report, deck)
- **B1 Streamlit app (`app/ui`):** extend the existing app: login selector from `list_login_options`, chat view, confirm and cancel buttons from `PendingView`, category buttons from `ChoiceView`, loading and error states, plain-text rendering of replies. Imports only `app.backend`, never `app.db` or `sqlite3`. Built against `FakeBackend` first, then the real backend.
- **B2 Docker:** `Dockerfile` (Python 3.13, uv), `docker-compose.yml`, `.dockerignore`, `.env.example` (names only), volumes for `data/` and the vector store, health check, one run command. Needs A's config names (database path, vector directory, provider variables) from Gate 0.
- **B3 Demo data and ops script (`scripts/`):** initialize and reset the demo database with a supplied `reference_time` (uses `create_schema` and `seed_database(connection, reference_time=...)`); never run automatically; note the 12-hour life of the 48-hour demo scenario.
- **B4 Evaluation (`data/eval`, `scripts/eval`):** expand `customer_queries_sample.csv` from 6 to about 60–80 labeled queries (intent, expected tool or action, safety and hallucination cases, cross-customer attempts, prompt injection, out-of-scope); a runner that drives `handle_message` and reports accuracy, tool-call correctness, hallucination and safety checks; sample conversation logs.
- **B5 Documents:** README (setup, run, Docker, limitations), refresh stale status text (`PROJECT_STATUS.md`, `AGENTS.md`), architecture diagram, technical report, presentation deck.

## Shared rules
- Directory ownership as above. Anyone needing a change in the other person's directory asks for it, to avoid merge conflicts.
- Only the docs/03 Section 4 types cross the boundary. B must not read repository dicts; A must not depend on Streamlit.
- Sync points: **M1** (end of day 1) contract live, UI on `FakeBackend`, tool and RAG skeletons. **M2** (mid day 2) real backend and RAG integrated, UI on the real backend. **M3** (end of day 2) orchestrator with a real LLM, evaluation runs. **M4** (day 3) Docker run from a clean clone, report and deck.
- Do not modify `Products.json`. Do not touch `data/app.db`; use temp databases in tests and a separate file path for the demo.

## Open decisions for the Technical Lead
1. `reason_category`: approve the `docs/01` and `docs/02` amendment and schema column now (needs a reset of the local demo database and test updates), or ship the MVP with the docs/03 reason-prefix convention and add the column later.
2. LLM provider and embedding model, needed by about M2.
3. Whether the docs/03 items deferred above are acceptable as documented limitations.

## Verification
- A: `uv run python -m pytest tests/ -q` from the repo root (existing suite plus new tool, backend and RAG tests; no real database access).
- B: run the Streamlit app against `FakeBackend`, then the real backend: log in as C101, ask order status, request a return, pick a category, confirm, check the replies; try a cross-customer order ID and an out-of-scope message.
- End to end: the evaluation runner over the labeled set, then `docker compose up` from a clean checkout and repeat the C101 return flow.
