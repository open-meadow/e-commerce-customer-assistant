# Agent guide

Capstone: text customer-support assistant. Synthetic data only. No real store, payments, or shipping. Deadline ~2026-11-13. `docs/01` and `docs/02` are canonical for database design and checkpoint authorization; `.claude/plans/rest-plan.md` tracks delivery tasks. See `PROJECT_STATUS.md` for the current snapshot and the documented discrepancy between the checkpoint 7–9 status in `docs/02` and files present in this checkout. Do not silently resolve it.

## Aim

Target: Streamlit chat for order, delivery, payment, return, refund, product, and policy questions. Current chat behavior is a local fake-backend demo, not a connected AI support assistant.

- Planned: RAG over `data/source/FAQ.md` and `Policies.md` (policy source of truth).
- Planned: product lookup from `data/source/Products.json` (read-only; not RAG; not a DB FK).
- Database/repository operations exist, but the chat UI does not call them. The current fake backend performs no real order lookups, returns, ticket writes, or policy retrieval.
- Simulated login (Streamlit session) is not production authentication. Any future identity must come from application context, never message or LLM text.
- Out of MVP: marketplace, cart, checkout, real auth, real refunds/couriers, voice (stretch only).

## Built

Phase 0–1 docs approved. Phase 2 plan approved. Database Checkpoints 1–10 complete and merged to `master`. The D2 database contract (D3–D8) is **committed and pushed** on `rukum/db` as `7dfd2f0` (412 database tests, 7 files). The backend (A2–A5) is implemented in the working tree of `rukum/backend` (created from `7dfd2f0`, not pushed) and is **uncommitted**. **Current full-suite result: 939 passed** (`uv run python -m pytest tests -q`, 2026-11-11). The older 497 and 207 figures are obsolete.

- **1** `app/db/connection.py` (rewritten for D2): three factories, `APP_DB_PATH`, default `data/app_d2.db`, `user_version` checks, typed errors; the legacy `data/app.db` is protected and never opened. `main.py` only prints the run command.
- **2** `app/db/schema.py`: `create_schema(connection)` creates the five tables. It does not open a connection.
- **3** `tests/test_schema.py`.
- **4** `app/db/seed_data.py`: deterministic seed, fixed baseline `2026-11-07T00:00:00Z`.
- **5** `app/db/repository.py`: five customer-scoped read functions.
- **6** `tests/test_authorization.py`.
- **7A** Write-function contracts approved, committed, and pushed (`8ce2d43`): `docs/01` §F–§H, `docs/02` §4–§5.
- **7** `create_return_request` and `create_support_ticket` in `app/db/repository.py` (`6a2c11a`).
- **8** `tests/test_returns.py` and `tests/test_tickets.py` (`0f27739`).
- **9** Full-suite verification (the 497-test suite was replaced by the D2 suite; current full-suite result 939 passed, see above). **10** Read-only repository inspection.
- **11A** `docs/03_backend_architecture.md` Revision 3.2 is the approved architecture baseline (design only; nothing in it is implemented).

## Not built, pending, or in progress

Do not treat these as existing. Do not start a checkpoint that has not been authorized.

- **Committed and pushed (`rukum/db`, `7dfd2f0`):** the D2 database contract: three connection factories, `APP_DB_PATH` (default `data/app_d2.db`), `user_version = 1`, persisted `reason_category`, exact-text validation, canonical timestamps, eligibility outcomes, typed errors, health check, `list_customers_for_login`, the seed command and the rewritten database tests (including `tests/conftest.py`).
- **Uncommitted on `rukum/backend`:** the tool layer, session registry and pending actions, scope guard, issue lifecycle, ticket summaries with fallback, lexical RAG, the provider-agnostic LLM layer and `app.backend`, with their tests, plus three audit fixes (confirm-time return-window message, typed `cancel` while a ticket description is awaited, graceful handling of a missing product catalog or FAQ source). See `docs/03` Section 19 and `docs/04_frontend_handover.md`.
- **Not verified:** the OpenAI-compatible and Anthropic adapters have never been run against a live service (no credentials). The rule-based LLM is a deterministic stand-in. RAG is lexical, not embeddings. Durable pending actions and idempotency are out of scope (single process, in-memory).
- **Open for Person B:** `app/ui/streamlit_app.py` (stub, not connected), Docker, evaluation set, report, deck. Person B's unmerged branches use `DATABASE_PATH` and `get_connection(db_path)`; they must move to `APP_DB_PATH` and the seed command (see `docs/04_frontend_handover.md`).
- **Current state:** branch `rukum/backend` (from `7dfd2f0`, not pushed). The backend code, its tests and the full-version documentation are uncommitted and not staged. `data/app.db` has not been opened or modified, and `data/app_d2.db` has not been created.

## Constraints

- This is a **uv** project. Run Python with `uv run`. Do not create a separate venv or use pip.
- SQLite is Python’s standard-library `sqlite3` module (confirmed via `uv run`; library 3.50.4). It is not a `pyproject.toml` dependency. Do not `uv add` a SQLite package.
- Five tables: `customers`, `orders`, `order_items`, `return_requests`, `support_tickets`. No payment or shipment tables. Design: `docs/01` §C.
- Every connection via the three factories in `app/db/connection.py`. Never `sqlite3.connect()` elsewhere. `APP_DB_PATH` selects the database; only the initialization factory creates files and it refuses `data/app.db`.
- **All database work happens on branch `rukum/db`** (remote `origin/rukum/db`, at `7dfd2f0`). Do not commit database work on `master`, and do not merge `rukum/db` into `master` without explicit authorization. Backend work happens on `rukum/backend`, created from `rukum/db` at `7dfd2f0` and not yet pushed.
- Never open, read, seed, reset, migrate or delete `data/app.db`. It is a legacy pre-D2 database (`user_version` 0, never stamped) and is protected in code: no connection factory opens it. The application's default database is `data/app_d2.db` (override with `APP_DB_PATH`); do not create or seed it unless the Technical Lead asks. Initialization is an explicit, separately authorized operator operation (`python -m app.db.seed_data`); there is no migration engine (M2).
- Money: INTEGER paise. Timestamps: TEXT ISO-8601 UTC.
- `orders.status`: Processing, Packed, Shipped, Out for Delivery, Delivered, Cancelled, Returned (`FAQ.md` Q1.4).
- Repository functions take `authenticated_customer_id` and filter in SQL. Composite FKs backstop consistency, not authorization.
- Seed must include C101→ORD1001 (Delivered, in window) and ORD1002; C102→ORD1003. `product_id` must exist in `Products.json` or fail. Do not edit `Products.json`.
- Return rules: 7 days (`Policies.md` §2); 48 hours if damaged/wrong (`FAQ.md` Q3.4).
- Do not claim a write succeeded without the backend confirming it.


# AI Model Selection and Engineering Workflow

## Project objective
Build a secure, maintainable, production-minded AI E-Commerce Customer Support Assistant. Prioritize correctness, security, testability, and simplicity over implementation speed.

## Model responsibilities

### Gemini 3.8 Flash
Use for:
- Routine implementation and debugging.
- Small isolated changes.
- Documentation.
- Test execution and ordinary error analysis.

Choose Low for simple explanations and edits, Medium for routine coding, and High for complex debugging or multi-file analysis.

### Claude Sonnet 4.6 Thinking
Use as the primary engineering implementation and review model for:
- Database and repository implementation.
- Business logic.
- Test design.
- Refactoring.
- Multi-file debugging.
- RAG and LLM orchestration.

### Claude Opus 4.6 Thinking
Reserve for:
- Security architecture.
- Customer data isolation.
- Authorization and IDOR prevention.
- Transaction and concurrency risks.
- Difficult architectural decisions.
- Final critical security reviews.

Do not use Opus for routine coding or documentation.

### GPT OSS 120B Medium
Use for:
- Independent code reviews.
- Additional test-case generation.
- Alternative technical approaches.
- Finding edge cases and challenging assumptions.

## Mandatory engineering workflow

1. Read the relevant project documentation and source files before proposing changes.
2. Identify the exact checkpoint and its acceptance criteria.
3. Do not change approved architecture or requirements without explicit approval.
4. Keep each task narrowly scoped.
5. Do not modify tests merely to make incorrect implementation pass.
6. Prefer minimal, maintainable changes over unnecessary abstractions.
7. Run the relevant tests and report actual command output.
8. Review the Git diff and list every modified file.
9. Distinguish verified facts from assumptions.
10. Never claim a task is complete solely because code was generated.

## Review workflow

For important changes:
- One model implements.
- A different model independently reviews.
- Resolve legitimate findings against the canonical project documents.
- Run tests locally.
- Obtain Technical Lead approval before moving to the next checkpoint.

## Context and token efficiency

- Read only files relevant to the current task.
- Avoid repeating entire project context when AGENTS.md and project documents already contain it.
- Do not inspect or modify unrelated modules.
- Prefer concise implementation reports and Git diffs.
- Never perform multiple unrelated checkpoints in one task.

## Authority hierarchy

1. User-approved decisions.
2. docs/01_database_design.md for database design.
3. docs/02_database_implementation_plan.md for implementation sequencing.
4. PROJECT_STATUS.md for current progress.
5. AGENTS.md for working conventions.
6. Current source code.

If source code conflicts with an approved document, report the conflict instead of silently treating the code as correct.