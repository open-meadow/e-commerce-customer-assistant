# Agent guide

Capstone: text customer-support assistant. Synthetic data only. No real store, payments, or shipping. Deadline ~2026-11-13. Status source: `docs/01` and `docs/02` are canonical; `PROJECT_STATUS.md` may be outdated.

## Aim

Streamlit chat that answers order, delivery, payment, return, refund, product, and policy questions.

- RAG over `data/source/FAQ.md` and `Policies.md` (policy source of truth).
- Product lookup from `data/source/Products.json` (read-only; not RAG; not a DB FK).
- SQLite tools, not open SQL: `get_order_status`, `create_return_request`, `get_refund_policy`.
- Simulated login (Streamlit session). Identity is app context, never message or LLM text.
- Escalation writes `support_tickets`. Cross-customer reads return no row ("not found").
- Out of MVP: marketplace, cart, checkout, real auth, real refunds/couriers, voice (stretch only).

## Built

Phase 0–1 docs approved. Phase 2 plan approved. Database Checkpoints 1–10 complete per the recorded history and Git (`master` at `3c3626a`, D1 baseline 2026-11-11). **Historical test result: 497 passed** (recorded earlier; the older "207 passing" figure was from Checkpoint 6 and is obsolete). The suite was **not re-run** when this file was updated, so 497 is not a freshly verified result.

- **1** `app/db/connection.py`: `get_connection()` sets and verifies `PRAGMA foreign_keys = ON`, `row_factory = sqlite3.Row`, raises `ConnectionError_`. Default path `data/app.db`. `app/db/__init__.py` re-exports `get_connection`. `main.py` is a smoke call.
- **2** `app/db/schema.py`: `create_schema(connection)` creates the five tables. It does not open a connection.
- **3** `tests/test_schema.py`.
- **4** `app/db/seed_data.py`: deterministic seed, fixed baseline `2026-11-07T00:00:00Z`.
- **5** `app/db/repository.py`: five customer-scoped read functions.
- **6** `tests/test_authorization.py`.
- **7A** Write-function contracts approved, committed, and pushed (`8ce2d43`): `docs/01` §F–§H, `docs/02` §4–§5.
- **7** `create_return_request` and `create_support_ticket` in `app/db/repository.py` (`6a2c11a`).
- **8** `tests/test_returns.py` and `tests/test_tickets.py` (`0f27739`).
- **9** Full-suite verification (historical 497 passed, see above). **10** Read-only repository inspection.
- **11A** `docs/03_backend_architecture.md` Revision 3.2 is the approved architecture baseline (design only; nothing in it is implemented).

## Not built, pending, or in progress

Do not treat these as existing. Do not start a checkpoint that has not been authorized.

- **D2 (pending):** a dedicated `reason_category` column on `return_requests` is approved in principle, but it is **not implemented** and its design amendment is not made. Today `create_return_request` validates `reason_category` to choose the window and does not store it.
- **Backend (not built):** `app/tools`, `app/rag`, `app/orchestrator` and `app/backend` do not exist beyond empty directories or design text in `docs/03`. No LLM provider is chosen.
- **UI (partial, other person's work):** `app/ui/` holds a first Streamlit app with stub replies. It is not connected to a backend.
- **Also absent:** RAG, orchestrator, eval set (~60–80; seed CSV has 6 rows), Docker, report, deck. `src/ecommerce_support_assistant/` is an unused uv stub.
- **Current next task:** D1 documentation baseline (this reconciliation), then D2.

## Constraints

- This is a **uv** project. Run Python with `uv run`. Do not create a separate venv or use pip.
- SQLite is Python’s standard-library `sqlite3` module (confirmed via `uv run`; library 3.50.4). It is not a `pyproject.toml` dependency. Do not `uv add` a SQLite package.
- Five tables: `customers`, `orders`, `order_items`, `return_requests`, `support_tickets`. No payment or shipment tables. Design: `docs/01` §C.
- Every connection via `get_connection()`. Never `sqlite3.connect()` elsewhere.
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