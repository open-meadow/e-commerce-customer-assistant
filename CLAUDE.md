# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Capstone: a text customer-support assistant (Streamlit chat, RAG over `app/db/data/source/FAQ.md` and `Policies.md`, tool calling over SQLite). Synthetic data only. `AGENTS.md` holds the working conventions and the model/review workflow; read it first. Deadline is about 2026-11-13.

## Where the code stands

Person A's database layer and backend (`rukum/db`, `rukum/backend`) and Person B's UI, Docker, evaluation and report work are all merged on `master` (merge commit `3b68b12`). **On branch `wire-ui-backend` (not yet merged to `master`) the Streamlit UI talks to the real backend** through `app/ui/client.py`; the plan is `docs/plans/ui_real_backend_plan.md`. `UI_BACKEND=fake` still selects the canned `app/ui/fake_backend.py`. `scripts` and Docker still do not use the database: the Docker image runs the fake (`UI_BACKEND=fake` in `docker-compose.yml`) and the eval runner uses the fake.

## Commands

This is a **uv** project on Python 3.13. The one runtime dependency in `pyproject.toml` is `streamlit`; the dev group has `pytest`. `requirements.txt` is empty. Do not use pip or create a separate venv. Run everything from the repository root: imports (`from app.db...`) and relative `APP_DB_PATH` values resolve from the project root.

```bash
uv run python -m pytest tests/ -q
```

```bash
uv run python -m pytest tests/test_returns.py::TestClassName::test_name -q
```

```bash
uv run python -m streamlit run app/ui/streamlit_app.py
```

```bash
uv run python scripts/eval/run_eval.py
```

```bash
docker compose up --build
```

Use `python -m pytest` and `python -m streamlit`, not the bare commands, so the repo root is on `sys.path`. There is no linter, formatter, coverage tool or pytest config. Shared fixtures live in `tests/conftest.py`.

To create a database for the real backend, use `python -m app.db.seed_data [--db-path PATH] [--reference-time T]` (without `--db-path` it uses `APP_DB_PATH`, default `app/db/data/app_d2.db`; the parent directory must exist; it refuses `app/db/data/app.db` and any existing database). **`scripts/init_demo_db.py` is broken against the current database layer** (see "Known breakage").

**Never open, seed, reset, migrate or write `app/db/data/app.db`** (the legacy database, protected: no connection factory opens it). The application's database is `app/db/data/app_d2.db` by default or whatever `APP_DB_PATH` names. The project has two people and the user is the technical lead: `app/db/data/app_d2.db` is the real working database (git-ignored) and may be seeded with the seed command; tests use temporary databases only. Settings come from a git-ignored `.env` at the project root (loaded with python-dotenv by the UI and the seed command; `.env.example` is the template). Normal startup never creates a database: the health check reports `not_initialized` until an operator runs the seed command.

Branches: `master` contains everything. Local and remote `rukum/db` and `rukum/backend` still exist, and `AGENTS.md` still says database work happens on `rukum/db`; that rule predates the merge, so ask before assuming where new work should go.

## Architecture

```
app/ui/          Streamlit chat (Person B)   -> client.py -> app.backend (default) or fake_backend.py (UI_BACKEND=fake)
app/backend/     public API for a frontend   -> orchestrator, tools, rag, catalog, llm -> app/db   (tested, not wired to the UI)
app/db/          SQLite layer + seed
app/db/data/source/     FAQ.md, Policies.md, Products.json (read-only source of truth)
app/db/data/eval/       70 synthetic eval cases + sample demo logs
scripts/         init_demo_db.py (broken), eval/run_eval.py (fake backend only)
```

`src/ecommerce_support_assistant/` is an unused uv stub that `pyproject.toml` builds.

### UI (`app/ui/`)

- `streamlit_app.py` is the entry point: a startup gate (`backend_status()`: shows a customer-safe message and stops when the database is missing or incompatible or a provider is misconfigured), the simulated login from `list_login_options()` (six customers on the real backend), a per-customer chat list in the sidebar, `needs_choice` buttons, Confirm/Cancel with the pending expiry, and error handling by `error_code` (the banner shows `reply.text`; an `invalid_session` restarts the chat; an error reply leaves open choices intact). It imports with a package-or-script fallback (`from .client` / `from client`).
- `client.py` picks the backend once at import from `UI_BACKEND` (`real` default, `fake`), re-exports the contract types, `list_login_options`, `start_session`, `handle_message`, and adds `BackendStatus`/`backend_status()` and `BACKEND_MODE`. It puts the project root on `sys.path`. `handle_turn` was removed. It loads `.env` (python-dotenv, `override=False`). To run: `cp .env.example .env`, seed (`python -m app.db.seed_data --reference-time <now>`) and start Streamlit.
- `fake_backend.py` (used only with `UI_BACKEND=fake`, the eval runner and `tests/ui/test_fake_backend.py`) defines its own frozen dataclasses (`LoginOption`, `ConversationState`, `ChatReply`, `PendingView`, `ChoiceView`, `Citation`, ...) shaped like `app/backend/types.py` but separate classes (its `ConversationState` carries demo flow fields, not an opaque handle). It returns canned text and a demo return-choice then confirmation flow, and writes nothing.
- `session.py` (backend-independent; no backend imports at runtime) holds the Streamlit session-state helpers: authenticated customer, a dict of chats (each with messages, `ConversationState`, pending and choice views), active chat, and UI error and notice keys. Chat history is temporary session state.
- Connecting the real backend means switching `client.py` to `app.backend`, handling `LLMConfigError` and `check_backend()` at startup, and launching with `python -m streamlit` so `app` is importable. `docs/04_frontend_handover.md` is the contract.

### Database layer (`app/db/`)

- Three connection factories in `connection.py`; never call `sqlite3.connect()` elsewhere. `get_connection(db_path=None)` opens an existing, initialized, current-version database read-write and **never creates a file** (a missing file raises `DatabaseNotInitializedError`). `get_readonly_connection` opens read-only. `get_initialization_connection(db_path)` is the only creator and refuses the protected `app/db/data/app.db`. The path comes from `APP_DB_PATH` (default `<project>/data/app_d2.db`). Schema version is `PRAGMA user_version = 1`; there is no legacy migration.
- `schema.create_schema(connection)` builds the five tables (`customers`, `orders`, `order_items`, `return_requests`, `support_tickets`) with canonical-timestamp, exact-text and consistency CHECKs, the stored `reason_category` and `uq_active_return_per_item`. `health.check_database_health()` reports `ready`, `not_initialized`, `schema_incompatible` or `unavailable` and never raises.
- `repository.py` is the authorization boundary: every function takes `authenticated_customer_id` and filters on it in SQL. Writes (`create_return_request`, `create_support_ticket`) use `BEGIN IMMEDIATE`, recheck eligibility inside the transaction and return compact results (`{"outcome","return_id"}`, `{"outcome":"created","ticket_id"}`). Failures raise typed `DatabaseError` subclasses (`app/db/errors.py`), not a generic `failed` result. `check_return_eligibility` is the read-only advisory twin; `list_customers_for_login` feeds the login selector only.
- `seed_data.seed_database(connection, reference_time=None)` is deterministic and not idempotent (6 customers C101 to C106, 15 orders; `None` means the baseline `2026-11-07T00:00:00Z`). Product IDs are validated against `Products.json`.
- `reason_category` (`damaged`, `wrong_product`, `standard`) selects the return window (48 h, 48 h, 168 h, inclusive). Return reasons are limited to 1,000 characters; ticket description and summary to 2,000. Text is validated exactly and never modified.

### Backend (`app/backend`, `app/orchestrator`, `app/tools`, `app/rag`, `app/llm`, `app/catalog`)

- `app.backend` is the only module a frontend should import: `list_login_options`, `start_session`, `handle_message(state, message=None, *, decision=None, choice=None)`, `end_session`, `check_backend`, `get_backend`, and the result types. Sessions are in-memory (one process); the `ConversationState` handle is a bearer credential. `app.backend` and `app.orchestrator` import each other.
- The LLM can only propose tool calls, text and an issue-relation label; `app/orchestrator/engine.py` validates all of it and owns every state change. Writes happen only after an explicit `ConfirmationDecision` on a pending action bound to its session (10-minute expiry, claimed once). Return reasons are the customer's own message, stored exactly, never the LLM's argument. Choice menus and reason requests expire after 10 minutes; typed `cancel` cancels the open prompt. Tool schemas contain no customer ID.
- The default LLM is the offline `RuleBasedLLM` (a deterministic stand-in, not a language model). `OpenAICompatibleClient` and `AnthropicClient` are chosen by `LLM_PROVIDER` plus `LLM_MODEL` and a key variable; a misconfigured real provider raises `LLMConfigError` rather than falling back. They have **not** been run against a live service.
- RAG is lexical BM25 over `FAQ.md` and `Policies.md` (no embeddings, no vector store). A missing or invalid source raises `KnowledgeBaseError` or `CatalogError`, and only the features that need it report "unavailable".

Conventions: money is integer paise; timestamps are TEXT `YYYY-MM-DDTHH:MM:SSZ` (UTC); `orders.status` values come from `FAQ.md` Q1.4.

### Known breakage and stale config

- `scripts/init_demo_db.py:95` calls `get_connection(...)`, which no longer creates files. Five tests in `tests/test_init_demo_db.py` fail for that reason, and `tests/e2e-db-test.sh` (which runs that script) would fail too. The fix is to use `get_initialization_connection`; `python -m app.db.seed_data` already does.
- `docker-compose.yml` sets `UI_BACKEND=fake`, `LLM_PROVIDER` (default `openai`) and `OPENAI_API_KEY`. The obsolete `DATABASE_PATH`, `VECTOR_STORE_DIR` and the `/app/data` and vector-store volumes were removed (no code reads them; the database variable is `APP_DB_PATH`, and retrieval is BM25 with no vector store). The `Dockerfile` copies only `src/` and `app/`, so the image has `app/db/data/source` but no `scripts` and no database; it runs the fake-backend UI on port 8501 and does not use the LLM settings. Docker does not read `.env` (it is in `.dockerignore`).
- `scripts/eval/run_eval.py` reads `app/db/data/eval/queries.csv` and runs each case against the **fake** backend. Every score is `not_assessed` and `observed_tool_calls` is null; it measures nothing about the real assistant.

## Tests

`uv run python -m pytest tests -q` on `wire-ui-backend`: **1023 passed, 5 failed** (990 tests when this file was first written; `tests/ui/conftest.py`, `test_client_real.py`, `test_streamlit_app_real.py` and `test_session_decoupled.py` are new) (all five in `tests/test_init_demo_db.py`, see above); nothing skipped.

- `tests/` root: database tests (`test_authorization`, `test_returns`, `test_schema`, `test_tickets`, `test_connection`, `test_clock_validation`, `test_seed`) and backend tests (`test_backend*`, `test_choice_expiry`, `test_components`, `test_issues`, `test_providers`, `test_retrieval*`, `test_return_reason`).
- `tests/ui/` covers the fake backend and the session helpers; `tests/test_eval_runner.py` covers the eval runner; `tests/test_init_demo_db.py` covers the broken script.
- `tests/conftest.py` has an autouse guard that fails any test that opens `app/db/data/app.db` or `app/db/data/app_d2.db`, controls the database clock (`set_now`) and the backend clock (`bnow`), builds seeded temp databases (`temp_db`, which also sets `APP_DB_PATH`) and provides `protected_sentinel` for protected-path tests (a temporary stand-in file plus a mocked low-level opener; never launch a subprocess with a real protected path). Backend tests use `tests/backend_helpers.py` (`Client`, `ScriptedLLM`). Adapter tests mock `urllib.request.urlopen` and are **not** live provider verification. Follow these patterns for new tests.

## Documents

Order of authority: user-approved decisions, then `docs/01_database_design.md`, `docs/02_database_implementation_plan.md`, `PROJECT_STATUS.md`, `AGENTS.md`, then code. If code conflicts with an approved document, report the conflict instead of choosing. `docs/03_backend_architecture.md` (backend design and implementation record) and `docs/04_frontend_handover.md` (frontend contract) describe Person A's backend. `README.md`, `docs/architecture.md`, `docs/technical_report.md` and `docs/presentation.*` are Person B's and describe the demo UI. `AGENTS.md` was written before the merge and still says the backend is uncommitted on `rukum/backend` (lines 36 and 39). Verify against files and Git rather than trusting any status text.

`app/db/data/source/FAQ.md` and `Policies.md` are the policy source of truth. `support@example.com` in them is a placeholder and must not be presented to customers as a real channel.
