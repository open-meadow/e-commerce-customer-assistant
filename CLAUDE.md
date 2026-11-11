# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Capstone: a text customer-support assistant (Streamlit chat, RAG over `data/source/FAQ.md` and `Policies.md`, tool calling over SQLite). Synthetic data only. `AGENTS.md` holds the working conventions and the model/review workflow; read it first. Deadline is about 2026-11-13.

## Commands

This is a **uv** project on Python 3.13 with no runtime dependencies (only `pytest` in the dev group). Do not use pip or create a separate venv. Run everything from the repository root: imports (`from app.db...`) and the default database path `data/app.db` are cwd-relative.

```bash
uv run python -m pytest tests/ -q
```

```bash
uv run python -m pytest tests/test_returns.py::TestClassName::test_name -q
```

Use `python -m pytest`, not bare `pytest`, so the repo root is on `sys.path`. There is no linter, formatter, coverage tool, `conftest.py` or pytest config.

**Never open, seed, reset or write `data/app.db`.** It is gitignored and holds a stray test ticket. Two traps: `python -m app.db.seed_data` and `main.py` both call `get_connection()` against the real path.

## Architecture

The database layer is the only backend code that exists. `app/tools`, `app/rag` and `app/orchestrator` are empty directories. `app/ui/` holds an early Streamlit app with stub replies (not connected to a backend). `src/ecommerce_support_assistant/` is an unused uv stub that `pyproject.toml` builds. The backend, tools, LLM and RAG are specified only in `docs/03_backend_architecture.md` (tracked design, Revision 3.2, the approved baseline pending the `reason_category` amendment in D2). Do not treat anything in it as implemented, and do not start a checkpoint that has not been authorized.

Database layer (`app/db/`):
- `connection.get_connection(db_path)` is the only way to open a connection: it enforces and verifies `PRAGMA foreign_keys = ON`, sets `sqlite3.Row`, and raises `ConnectionError_`. Never call `sqlite3.connect()` elsewhere. It also creates the parent directory and an empty file when missing.
- `schema.create_schema(connection)` builds five tables (`customers`, `orders`, `order_items`, `return_requests`, `support_tickets`) with composite foreign keys and the partial unique index `uq_active_return_per_item`. It takes a connection and never opens one.
- `seed_data.seed_database(connection, reference_time=None)` is deterministic and not idempotent (raises if tables are non-empty). `None` means the fixed baseline `2026-11-07T00:00:00Z`; all order dates are offsets from the reference time. `product_id` values are validated against `data/source/Products.json`, which is read-only.
- `repository.py` is the authorization boundary. Every function takes `authenticated_customer_id` (from trusted app context, never message or LLM text) and filters on it in SQL, so cross-customer reads return "not found". Reads open their own connection by calling `get_connection()` with no arguments, and propagate `sqlite3.Error`. The two writes (`create_return_request`, `create_support_ticket`) use `BEGIN IMMEDIATE`, report `created` only after COMMIT, map database errors to a generic `failed`, and re-raise non-database exceptions after rollback. Wrong types raise `TypeError`; invalid values raise `ValueError`.
- `reason_category` (`damaged`, `wrong_product`, `standard`) selects the return window (48h, 48h, 168h, inclusive) and is **not stored** today. A dedicated column is approved in principle but not yet documented or implemented.

Conventions: money is integer paise; timestamps are TEXT `YYYY-MM-DDTHH:MM:SSZ` (UTC); `orders.status` values come from `FAQ.md` Q1.4.

## Tests

Four files under `tests/`. Tests never use the real database: they build temp databases with the real `create_schema` and `seed_database`, and monkeypatch `repository.get_connection` to point at them. `test_returns.py` and `test_tickets.py` also have an autouse fixture that patches `sqlite3.connect` to fail if the real path is opened (a string comparison from the cwd). `test_authorization.py` only `os.stat`s `data/app.db` to assert it is unchanged. Follow this pattern for any new database test.

## Document authority

Order: user-approved decisions, then `docs/01_database_design.md`, `docs/02_database_implementation_plan.md`, `PROJECT_STATUS.md`, `AGENTS.md`, then code. If code conflicts with an approved document, report the conflict instead of choosing. The status statements in `docs/01`, `docs/02`, `AGENTS.md` and `PROJECT_STATUS.md` were reconciled with the code in the D1 documentation baseline (2026-11-11). Database Checkpoints 1–10 are done and merged into `master`. The recorded 497-passed test result is historical, not freshly verified. Still verify against files and Git rather than trusting any status text.

`data/source/FAQ.md` and `Policies.md` are the policy source of truth. `support@example.com` in them is a placeholder and must not be presented to customers as a real channel.
