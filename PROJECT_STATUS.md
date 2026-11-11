# Project Status

**Project:** AI E-Commerce Customer Support Assistant
**Deadline:** 12 days from Nov 1, 2026 → due ~Nov 13, 2026
**Current phase:** Database completion (Checkpoint D series). Database Checkpoints 1–10 are merged. D1 (documentation baseline) is `d980594` on `rukum/db`. **D2 (contract) and D3–D8 (database implementation and regression) are implemented in this commit; D9 (independent review) is pending.** The backend will be committed on a new branch, `rukum/backend`, to be created from the final `rukum/db` HEAD (it does not exist yet). All database work uses branch `rukum/db`.

*(The body below was written during Checkpoints 1 and 2 and is a historical record: its statements about `get_connection()` and a "not started" checkpoint describe that time. The "Status as of 2026-11-12" section at the end is the current statement.)*

*(Phase numbering note: this line is corrected from the previous revision, which still labeled upcoming work "Phase 1." The docs folder's own numbering is the source of truth — `docs/00_project_discovery.md` = Phase 0, `docs/01_database_design.md` = Phase 1 (approved), `docs/02_database_implementation_plan.md` = Phase 2 (in progress). This file now matches that numbering.)*

## Scope decision (confirmed by student)
- Text chat via Streamlit — **in scope for MVP**
- Real-time voice — **deferred / stretch goal only**, built if time remains after MVP is complete, tested, and documented
- Code is modular throughout specifically so voice can be added later without reworking `tools/`, `db/`, or `orchestrator/`
- Data model: **single `orders` table** carrying `status`, `payment_status`, `refund_status`, `refund_amount`, `delivered_at` — no separate payment/shipment tables unless a demonstrated requirement emerges. A dedicated `return_requests` table is kept for return lifecycle tracking (customer, order, item, reason, status, timestamp).

## Completed work

**Documentation (Phases 0–2 planning)**
- Official college brief confirmed (text chatbot, web or CLI, 12-day deadline)
- Dataset inspected: `FAQ.md`, `Policies.md`, `Products.json`, `customer_queries_sample.csv`, `README_DATASET.md`
- Intent taxonomy drafted (11 intents)
- High-level architecture + repo structure proposed
- Tool contracts drafted for `get_order_status`, `create_return_request`, `get_refund_policy`
- Discovery report written and revised (`docs/00_project_discovery.md`) — **Phase 0 approved**
- Database design written and revised across two Technical Lead review rounds, including the single-`orders`-table model, composite foreign keys, corrected enums, and storage conventions (`docs/01_database_design.md`) — **Phase 1 approved**
- Database implementation plan written and revised (`docs/02_database_implementation_plan.md`) — **approved**

**Project setup**
- Initial project structure created (`app/`, `app/db/`, `data/`, `docs/`, `data/knowledge_base/`)
- Dataset files added to the repository
- Git repository initialized
- Initial commit created

**Phase 2, Checkpoint 1 — `app/db/connection.py`**
- `app/__init__.py` and `app/db/__init__.py` created (package markers only)
- `app/db/connection.py` implemented: centralized `get_connection()` function; executes `PRAGMA foreign_keys = ON;` on every connection; reads the pragma back and verifies it is exactly `1` before returning the connection; sets `row_factory = sqlite3.Row`; translates filesystem and SQLite failures into a dedicated `ConnectionError_` with the original exception chained as cause; the foreign-key-verification failure path raises `ConnectionError_` with no underlying cause, since it is an application-level check, not a wrapped error
- Two Technical Lead review rounds applied: connection-cleanup failures are now suppressed so they cannot mask the original setup error (`_close_quietly()`), and the `ConnectionError_` docstring accurately distinguishes filesystem-caused, SQLite-caused, and application-detected (no-cause) failures
- Verified in two separate environments:
  - **Development sandbox (Linux):** success path, directory-failure path, and the close-masking fix all exercised directly against the real function, with actual output reviewed, including one real bug found and fixed during verification (unhandled `OSError` on directory creation)
  - **Target environment (Windows, `D:\ecommerce-support-assistant`):** an actual PowerShell smoke test was run by the Technical Lead against the real `get_connection()` function; `PRAGMA foreign_keys` returned `1`; `row_factory` confirmed as `sqlite3.Row`; smoke test passed
- **Checkpoint 1 is approved and complete.**

**Phase 2, Checkpoint 2 — `app/db/schema.py`**
- `create_schema(connection)` creates `customers`, `orders`, `order_items`, `return_requests`, and `support_tickets` with the composite foreign keys, CHECK constraints, and indexes from `docs/01_database_design.md` Section C.
- The function does not call `sqlite3.connect()`. Callers pass a connection from `get_connection()`.
- Manual check on an in-memory connection from `get_connection()` (`uv run`, `PRAGMA foreign_keys` read back `1`): all five tables were created. A `return_requests` row whose `(order_id, customer_id)` did not match `orders` was rejected with `FOREIGN KEY constraint failed`. No return row was stored.
- **Checkpoint 2 is approved (2026-11-06).**

## Status as of 2026-11-12 (database)

- **Database:** Checkpoints 1–10 are merged. The D2 contract (D3 connections, D4 schema, D5 writes, D6 reads, D7 seed, D8 regression) is **implemented** in this commit: three connection factories, `APP_DB_PATH`, `user_version = 1`, persisted `reason_category`, exact-text validation, canonical timestamps, eligibility outcomes, `RET_`/`TKT_` identifiers, typed errors, `check_database_health`, `list_customers_for_login`, and a seed CLI that refuses `data/app.db`. The default database path is `data/app_d2.db` (override: `APP_DB_PATH`); the legacy `data/app.db` is protected and never opened. The database does not exist until an operator runs `python -m app.db.seed_data` (parent directory must exist); the health check reports `not_initialized` until then. `data/app_d2.db` has not been created.
- **Tests:** `uv run python -m pytest tests/test_authorization.py tests/test_returns.py tests/test_schema.py tests/test_tickets.py tests/test_clock_validation.py tests/test_connection.py tests/test_seed.py -q` gives **412 passed** (7 database test files, run 2026-11-12). Per file: test_authorization 29, test_clock_validation 63, test_connection 55, test_returns 100, test_schema 83, test_seed 24, test_tickets 58. The legacy 497-test suite was replaced. No test opens `data/app.db` or `data/app_d2.db` (an autouse guard enforces this; protected-path tests use a temporary sentinel and a mocked opener).
- **Not on this branch:** the backend (`app/tools`, `app/backend`, `app/orchestrator`, `app/rag`, `app/llm`, `app/catalog`) will be committed on a new branch, `rukum/backend`, to be created from the final `rukum/db` HEAD. `app/ui/` is an early Streamlit stub that is not connected to a backend.
- **Review state:** D9 independent review has not happened.

## Pending work
- Person A: D9 review of this database commit; the backend on a new `rukum/backend` branch (to be created from the final `rukum/db` HEAD); choose the LLM provider.
- Person B: Streamlit UI against the backend once it exists, Docker, evaluation set expansion (6 to about 60–80 queries), report, deck.
