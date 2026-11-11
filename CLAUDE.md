# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Capstone: a text customer-support assistant (Streamlit chat, RAG over `data/source/FAQ.md` and `Policies.md`, tool calling over SQLite). Synthetic data only. `AGENTS.md` holds the working conventions and the model/review workflow; read it first. Deadline is about 2026-11-13.

## Commands

This is a **uv** project on Python 3.13. The database layer uses only the standard library; the one runtime dependency in `pyproject.toml` is `streamlit` (Person B's UI) and the dev group has `pytest`. Do not use pip or create a separate venv. Run everything from the repository root: imports (`from app.db...`) and relative `APP_DB_PATH` values resolve from the project root.

```bash
uv run python -m pytest tests/ -q
```

```bash
uv run python -m pytest tests/test_returns.py::TestClassName::test_name -q
```

Use `python -m pytest`, not bare `pytest`, so the repo root is on `sys.path`. There is no linter, formatter, coverage tool, `conftest.py` or pytest config.

**All database work happens on branch `rukum/db`** (remote `origin/rukum/db`), not `master`.

**Never open, seed, reset, migrate or write `data/app.db`** (the legacy database, protected: no connection factory opens it). The application's database is `data/app_d2.db` by default or whatever `APP_DB_PATH` names. **Do not create or seed `data/app_d2.db` unless the Technical Lead asks**; tests use temporary databases only. Normal startup never creates a database: the health check reports `not_initialized` until an operator runs the seed command.

## Architecture

Implemented on this branch: the D2 database layer (`app/db/`). The backend, tools, RAG and LLM layer are **not part of this commit**; they will be committed on a new `rukum/backend` branch, to be created from the final `rukum/db` HEAD (design in `docs/03_backend_architecture.md`). `app/ui/` is still an early Streamlit app with stub replies (Person B's work, not connected). `src/ecommerce_support_assistant/` is an unused uv stub that `pyproject.toml` builds.

Database layer (`app/db/`):
- Three connection factories in `connection.py`; never call `sqlite3.connect()` elsewhere. `get_connection(db_path=None)` opens an existing, initialized, current-version database read-write and never creates a file. `get_readonly_connection` opens read-only. `get_initialization_connection(db_path)` is the only creator and refuses the protected `data/app.db`. The path comes from `APP_DB_PATH` (default `<project>/data/app_d2.db`; the legacy `data/app.db` is protected and refused by every factory). The schema version is `PRAGMA user_version = 1`; there is no legacy migration (fresh databases only).
- `schema.create_schema(connection)` builds the five tables with canonical-timestamp, exact-text and consistency CHECKs, the persisted `reason_category` column and `uq_active_return_per_item`. `health.check_database_health()` reports `ready`, `not_initialized`, `schema_incompatible` or `unavailable` and never raises.
- `repository.py` is the authorization boundary: every function takes `authenticated_customer_id` and filters on it in SQL. Reads call `get_connection()` per call. Writes (`create_return_request`, `create_support_ticket`) use `BEGIN IMMEDIATE`, recheck eligibility inside the transaction, and return compact results (`{"outcome","return_id"}` and `{"outcome":"created","ticket_id"}`). Database failures raise typed `DatabaseError` subclasses (`app/db/errors.py`), not a generic `failed` result. `check_return_eligibility` is the read-only advisory twin. `list_customers_for_login` feeds the login selector only.
- `seed_data.seed_database(connection, reference_time=None)` is deterministic and not idempotent. The command is `python -m app.db.seed_data [--db-path PATH] [--reference-time T]`; without `--db-path` it uses the application's path (`APP_DB_PATH`, default `data/app_d2.db`). The parent directory must already exist, and it refuses the protected `data/app.db` and any existing database.
- `reason_category` (`damaged`, `wrong_product`, `standard`) selects the return window (48h, 48h, 168h, inclusive) and is stored. Return reasons are limited to 1,000 characters; ticket description and summary to 2,000. Text is validated exactly and never modified.

Not on this branch: `app/backend`, `app/orchestrator`, `app/tools`, `app/rag`, `app/llm` and `app/catalog` do not exist here. They will be committed on a new branch, `rukum/backend`, to be created from the final `rukum/db` HEAD.

Conventions: money is integer paise; timestamps are TEXT `YYYY-MM-DDTHH:MM:SSZ` (UTC); `orders.status` values come from `FAQ.md` Q1.4.

## Tests

`uv run python -m pytest tests/test_authorization.py tests/test_returns.py tests/test_schema.py tests/test_tickets.py tests/test_clock_validation.py tests/test_connection.py tests/test_seed.py -q` (**412 passed** across the 7 database test files on 2026-11-12). `tests/conftest.py` has an autouse guard that fails any test that opens `data/app.db` or `data/app_d2.db`, controls the database clock (`set_now`), builds seeded temp databases (`temp_db`, which also sets `APP_DB_PATH`) and provides `protected_sentinel` for protected-path tests (a temporary stand-in file plus a mocked low-level opener; never launch a subprocess with a real protected path). Follow these patterns for new tests.

## Document authority

Order: user-approved decisions, then `docs/01_database_design.md`, `docs/02_database_implementation_plan.md`, `PROJECT_STATUS.md`, `AGENTS.md`, then code. If code conflicts with an approved document, report the conflict instead of choosing. The status statements in `docs/01`, `docs/02`, `AGENTS.md` and `PROJECT_STATUS.md` were reconciled with the code in the D1 documentation baseline (2026-11-11). Database Checkpoints 1–10 are done and merged into `master`. The recorded 497-passed test result is historical, not freshly verified. Still verify against files and Git rather than trusting any status text.

`data/source/FAQ.md` and `Policies.md` are the policy source of truth. `support@example.com` in them is a placeholder and must not be presented to customers as a real channel.
