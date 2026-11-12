# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Capstone: a text customer-support assistant. A modular Python monolith: Streamlit chat UI, an in-process backend (orchestrator, customer-scoped tools, BM25 retrieval over `app/db/data/source/FAQ.md` and `Policies.md`, a pluggable LLM layer) and a SQLite database. Synthetic data only. `AGENTS.md` holds the working conventions and the model/review workflow; read it first. Deadline is about 2026-11-13. `PROJECT_STATUS.md` is the current snapshot.

## Where the code stands

Everything is merged on `master`: Person A's database layer and backend, Person B's UI, Docker, evaluation and report work, the UI wired to the real backend (`app/ui/client.py`; `UI_BACKEND=fake` still selects the canned `app/ui/fake_backend.py`) and the OpenAI Responses API adapter (`f25b7c4`, merge `967cfe9`, pushed). **Local `master` is one commit ahead of `origin/master`:** `66c60fe`, the real-backend evaluation runner (A3), not pushed. Verify with `git status -sb`.

The default assistant is the offline `RuleBasedLLM` (safe demo path, no key, no network). A real provider has been exercised only by one guarded one-request probe (reported by the technical lead); full live application and tool-calling evaluation has **not** been done. Docker still runs the fake-backend UI and does not use the database. The fake-backend eval runner (`run_eval.py`) measures nothing; the real-backend runner (`real_eval.py`) measures mechanical checks only (see "Evaluation").

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

**Before starting Streamlit, check where `APP_DB_PATH` resolves.** The UI opens that database. A real environment variable overrides `.env`, and `.env.example` (so a `.env` copied from it) sets `APP_DB_PATH=app/db/data/app_d2.db`. Make sure it points at a temporary or demo database (for example, in PowerShell, `$env:APP_DB_PATH = "demo\demo.db"` for the session). If it resolves to `app_d2.db`, do not start the application without the technical lead's explicit authorization. `UI_BACKEND=fake` needs no database.

```bash
uv run python scripts/eval/run_eval.py
```

```bash
uv run python scripts/eval/real_eval.py --output <path>.json
```

```bash
docker compose up --build
```

Use `python -m pytest` and `python -m streamlit`, not the bare commands, so the repo root is on `sys.path`. There is no linter, formatter, coverage tool or pytest config. Shared fixtures live in `tests/conftest.py`.

To create a database for the real backend, use `python -m app.db.seed_data [--db-path PATH] [--reference-time T]` (without `--db-path` it uses `APP_DB_PATH`, default `app/db/data/app_d2.db`; the parent directory must exist; it refuses `app/db/data/app.db` and any existing database). For development, seed a temporary or demo file with `--db-path` (for example `demo/demo.db`; `demo/` is git-ignored). Running the command without `--db-path` creates `app_d2.db` when `APP_DB_PATH` is unset or points there, which needs the technical lead's explicit authorization (see "Database safety"). `scripts/init_demo_db.py` was fixed to use `get_initialization_connection` (`c9035c5`) and is covered by `tests/test_init_demo_db.py`.

**Database safety (hard rules).**
- **`app/db/data/app.db` must NEVER be opened or accessed** (the legacy database, protected: no connection factory opens it). No authorization changes this.
- **`app/db/data/app_d2.db` is protected** (the technical lead's real working database, git-ignored). It must not be opened, read, initialized, seeded or modified unless the technical lead explicitly authorizes that specific access. That includes starting Streamlit while `APP_DB_PATH` resolves to it and running the seed command without `--db-path`.
- Normal development, tests, evaluation and UI verification use a **temporary or demo database** (`temp_db` in tests; `real_eval.py` builds its own; a seeded `demo/demo.db` for the UI). `tests/conftest.py` fails any test that opens either protected file.
- Settings come from a git-ignored `.env` at the project root (loaded with python-dotenv by the UI and the seed command; `.env.example` is the template). The evaluation runners do not read `.env`. Normal startup never creates a database: the health check reports `not_initialized` until an operator runs the seed command.

**Git rules.** Do not stage, commit, push, amend, rebase, merge or change branches without the user's explicit approval for that operation; approval for one does not extend to the next. No live LLM call without separate, explicit authorization (see "Evaluation").

Branches: `master` contains everything; every other local feature branch is already merged. Local and remote `rukum/db` and `rukum/backend` still exist, and `AGENTS.md` still names `rukum/db` for database work; that rule predates the merge, so ask before assuming where new work should go.

## Architecture

```
app/ui/          Streamlit chat (Person B)   -> client.py -> app.backend (default) or fake_backend.py (UI_BACKEND=fake)
app/backend/     public API for a frontend   -> orchestrator, tools, rag, catalog, llm -> app/db   (tested; used by the UI by default)
app/db/          SQLite layer + seed
app/db/data/source/     FAQ.md, Policies.md, Products.json (read-only source of truth)
app/db/data/eval/       70 synthetic eval cases + sample demo logs
scripts/         init_demo_db.py, eval/run_eval.py (fake backend only), eval/real_eval.py (real backend, offline by default)
```

`src/ecommerce_support_assistant/` is an unused uv stub that `pyproject.toml` builds.

### UI (`app/ui/`)

- `streamlit_app.py` is the entry point: a startup gate (`backend_status()`: shows a customer-safe message and stops when the database is missing or incompatible or a provider is misconfigured), the simulated login from `list_login_options()` (six customers on the real backend), a per-customer chat list in the sidebar, `needs_choice` buttons, Confirm/Cancel with the pending expiry, and error handling by `error_code` (the banner shows `reply.text`; an `invalid_session` restarts the chat; an error reply leaves open choices intact). It imports with a package-or-script fallback (`from .client` / `from client`).
- `client.py` picks the backend once at import from `UI_BACKEND` (`real` default, `fake`), re-exports the contract types, `list_login_options`, `start_session`, `handle_message`, and adds `BackendStatus`/`backend_status()` and `BACKEND_MODE`. It puts the project root on `sys.path`. `handle_turn` was removed. It loads `.env` (python-dotenv, `override=False`). To run: `cp .env.example .env`, point `APP_DB_PATH` at a demo file (the template points it at the protected `app_d2.db`), seed that file (`python -m app.db.seed_data --db-path demo/demo.db --reference-time <now>`) and start Streamlit (see the check above).
- `fake_backend.py` (used only with `UI_BACKEND=fake`, the eval runner and `tests/ui/test_fake_backend.py`) defines its own frozen dataclasses (`LoginOption`, `ConversationState`, `ChatReply`, `PendingView`, `ChoiceView`, `Citation`, ...) shaped like `app/backend/types.py` but separate classes (its `ConversationState` carries demo flow fields, not an opaque handle). It returns canned text and a demo return-choice then confirmation flow, and writes nothing.
- `session.py` (backend-independent; no backend imports at runtime) holds the Streamlit session-state helpers: authenticated customer, a dict of chats (each with messages, `ConversationState`, pending and choice views), active chat, and UI error and notice keys. Chat history is temporary session state.
- Connecting the real backend means switching `client.py` to `app.backend`, handling `LLMConfigError` and `check_backend()` at startup, and launching with `python -m streamlit` so `app` is importable. `docs/04_frontend_handover.md` is the contract.

### Database layer (`app/db/`)

- Three connection factories in `connection.py`; never call `sqlite3.connect()` elsewhere. `get_connection(db_path=None)` opens an existing, initialized, current-version database read-write and **never creates a file** (a missing file raises `DatabaseNotInitializedError`). `get_readonly_connection` opens read-only. `get_initialization_connection(db_path)` is the only creator and refuses the protected `app/db/data/app.db`. The path comes from `APP_DB_PATH` (default `<project>/app/db/data/app_d2.db`). Schema version is `PRAGMA user_version = 1`; there is no legacy migration.
- `schema.create_schema(connection)` builds the five tables (`customers`, `orders`, `order_items`, `return_requests`, `support_tickets`) with canonical-timestamp, exact-text and consistency CHECKs, the stored `reason_category` and `uq_active_return_per_item`. `health.check_database_health()` reports `ready`, `not_initialized`, `schema_incompatible` or `unavailable` and never raises.
- `repository.py` is the authorization boundary: every function takes `authenticated_customer_id` and filters on it in SQL. Writes (`create_return_request`, `create_support_ticket`) use `BEGIN IMMEDIATE`, recheck eligibility inside the transaction and return compact results (`{"outcome","return_id"}`, `{"outcome":"created","ticket_id"}`). Failures raise typed `DatabaseError` subclasses (`app/db/errors.py`), not a generic `failed` result. `check_return_eligibility` is the read-only advisory twin; `list_customers_for_login` feeds the login selector only.
- `seed_data.seed_database(connection, reference_time=None)` is deterministic and not idempotent (6 customers C101 to C106, 15 orders; `None` means the baseline `2026-11-07T00:00:00Z`). Product IDs are validated against `Products.json`.
- `reason_category` (`damaged`, `wrong_product`, `standard`) selects the return window (48 h, 48 h, 168 h, inclusive). Return reasons are limited to 1,000 characters; ticket description and summary to 2,000. Text is validated exactly and never modified.

### Backend (`app/backend`, `app/orchestrator`, `app/tools`, `app/rag`, `app/llm`, `app/catalog`)

- `app.backend` is the only module a frontend should import: `list_login_options`, `start_session`, `handle_message(state, message=None, *, decision=None, choice=None)`, `end_session`, `check_backend`, `get_backend`, and the result types. Sessions are in-memory (one process); the `ConversationState` handle is a bearer credential. `app.backend` and `app.orchestrator` import each other.
- The LLM can only propose tool calls, text and an issue-relation label; `app/orchestrator/engine.py` validates all of it and owns every state change. Writes happen only after an explicit `ConfirmationDecision` on a pending action bound to its session (10-minute expiry, claimed once). Return reasons are the customer's own message, stored exactly, never the LLM's argument. Choice menus and reason requests expire after 10 minutes; typed `cancel` cancels the open prompt. Tool schemas contain no customer ID.
- The default LLM is the offline `RuleBasedLLM` (a deterministic stand-in, not a language model). `OpenAIResponsesClient` (OpenAI Responses API, `POST {OPENAI_BASE_URL}/responses`, stateless with `store: false`, native `function_call` / `function_call_output` items joined by `call_id`) and `AnthropicClient` are chosen by `LLM_PROVIDER` plus `LLM_MODEL` and a key variable; a misconfigured real provider raises `LLMConfigError` rather than falling back. A response with status `incomplete` is rejected (`category="incomplete"`). Provider failures raise `LLMError` with a `category` (`auth`, `bad_request`, `not_found`, `rate_limited`, `server_error`, `timeout`, `dns`, `network`, `invalid_json`, `invalid_response`, `incomplete`, ...) and log one safe warning on logger `app.llm`; nothing is retried. Adapter tests use a mocked transport. The only live evidence is one guarded one-request OpenAI probe (reported by the technical lead; it is not an application or tool-calling evaluation), and the Anthropic adapter has not been run live.
- RAG is lexical BM25 over `FAQ.md` and `Policies.md` (no embeddings, no vector store). A missing or invalid source raises `KnowledgeBaseError` or `CatalogError`, and only the features that need it report "unavailable".

Conventions: money is integer paise; timestamps are TEXT `YYYY-MM-DDTHH:MM:SSZ` (UTC); `orders.status` values come from `FAQ.md` Q1.4.

### Evaluation

Two runners read the same 70-case file `app/db/data/eval/queries.csv` (columns `id,query,intent,category,example_expected_action,notes,customer_id,expected_behavior,expected_tool`). Full detail: `docs/04_frontend_handover.md` Section 10.

- **`scripts/eval/run_eval.py`: fake-runner.** Runs each case against the canned fake UI backend. Every score is `not_assessed` and `observed_tool_calls` is null. It smoke-tests the data and case isolation; it measures nothing about the real assistant.
- **`scripts/eval/real_eval.py`: real-backend runner.** Runs each case in its own session through `app.backend.Backend` on a **temporary seeded database** with both clocks fixed at the seed baseline, plus five multi-turn scenarios (ticket confirmed, ticket declined, return confirmed, return outside window, return of another customer's item). It records proposed and executed tool calls and scores mechanical checks only, each tagged `backend_code`, `llm_proposals`, `llm_text` or `provider_and_backend`. **Semantic answer quality is never scored**; replies are recorded for human review.
- Options: `--cases`, `--output`, `--llm {rule_based,env}` (default `rule_based`, offline), `--allow-live`, `--compare FIRST SECOND`.
- **`--llm env` requires `LLM_PROVIDER` to name a real provider** (`openai` or `anthropic`) plus `LLM_MODEL` and its key; unset, blank or `rule_based` is an error, never a silent fallback to the stand-in. A real provider also needs **`--allow-live`**, because it makes live, billed requests. **Do not run it, or make any live LLM call, without the user's explicit authorization.**
- With `RuleBasedLLM` the `llm_*` checks measure a keyword stand-in, not a language model. Do not quote offline numbers as model quality. Known limits (outdated CSV labels, `tool_selection` penalizing safe refusals, exact-word status matching, contact details in policy text, a heuristic secret scan, `live_requests_made` meaning authorization) are in `docs/04` Section 10.

### Known breakage and stale config

- `docker-compose.yml` sets `UI_BACKEND=fake`, `LLM_PROVIDER` (default `openai`) and `OPENAI_API_KEY`. The obsolete `DATABASE_PATH`, `VECTOR_STORE_DIR` and the `/app/data` and vector-store volumes were removed (no code reads them; the database variable is `APP_DB_PATH`, and retrieval is BM25 with no vector store). The `Dockerfile` copies only `src/` and `app/`, so the image has `app/db/data/source` but no `scripts` and no database; it runs the fake-backend UI on port 8501 and does not use the LLM settings. Docker does not read `.env` (it is in `.dockerignore`).
- `scripts/eval/run_eval.py` runs against the **fake** backend and measures nothing (see "Evaluation").
- Open gaps found by the real-backend evaluation, deliberately not fixed yet: the guard does not treat "Can you cancel ORD1002?" as an unsupported cancel (the pattern needs the word "order"; Q034); a catalog product name makes "The charger is faulty yaar" count as specific so it is not clarified (Q070); `FAQ.md` Q6.2 and `Policies.md` Section 7 contain the placeholder `support@example.com`, which replies repeat unredacted (Q033, Q045, Q048, Q055, Q057).

## Tests

Latest measured (2026-11-12, offline): `uv run --offline --no-sync python -m pytest tests -q -p net_guard_plugin` gave **1172 passed**, nothing failed or skipped; the targeted run `tests/test_real_eval.py tests/test_eval_runner.py` gave **59 passed**. `net_guard_plugin` is a scratch plugin outside the repository that counts external network attempts (0); the documented command is `uv run python -m pytest tests -q`. Earlier counts in older notes (939, 990, 1023, 1028, 1119, 1143) are historical.

- `tests/` root: database tests (`test_authorization`, `test_returns`, `test_schema`, `test_tickets`, `test_connection`, `test_clock_validation`, `test_seed`) and backend tests (`test_backend*`, `test_choice_expiry`, `test_components`, `test_issues`, `test_providers`, `test_retrieval*`, `test_return_reason`).
- `tests/ui/` covers the fake backend, the real-backend client and the session helpers; `tests/test_eval_runner.py` covers the fake runner; `tests/test_real_eval.py` covers the real-backend runner (scoring, environment cleanup on failure, live-call gating, provenance and deliberately bad models); `tests/test_init_demo_db.py` covers `scripts/init_demo_db.py`.
- `tests/conftest.py` has an autouse guard that fails any test that opens `app/db/data/app.db` or `app/db/data/app_d2.db`, controls the database clock (`set_now`) and the backend clock (`bnow`), builds seeded temp databases (`temp_db`, which also sets `APP_DB_PATH`) and provides `protected_sentinel` for protected-path tests (a temporary stand-in file plus a mocked low-level opener; never launch a subprocess with a real protected path). Backend tests use `tests/backend_helpers.py` (`Client`, `ScriptedLLM`). Adapter tests mock `urllib.request.urlopen` and are **not** live provider verification. Follow these patterns for new tests.

## Documents

Order of authority: user-approved decisions, then `docs/01_database_design.md`, `docs/02_database_implementation_plan.md`, `PROJECT_STATUS.md`, `AGENTS.md`, then code. If code conflicts with an approved document, report the conflict instead of choosing. `docs/03_backend_architecture.md` (backend design and implementation record) and `docs/04_frontend_handover.md` (frontend contract) describe Person A's backend. `README.md`, `docs/architecture.md`, `docs/technical_report.md` and `docs/presentation.*` are Person B's and describe the demo UI. `AGENTS.md` and `PROJECT_STATUS.md` were refreshed on 2026-11-12 (A4). `docs/01` to `docs/03` still contain pre-merge path and status text (for example `data/...` paths and "uncommitted on `rukum/backend`"); corrections are pending approval. Verify against files and Git rather than trusting any status text.

`app/db/data/source/FAQ.md` and `Policies.md` are the policy source of truth. `support@example.com` in them is a placeholder and must not be presented to customers as a real channel.
