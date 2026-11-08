# Agent guide

Capstone: text customer-support assistant. Synthetic data only. No real store, payments, or shipping. Deadline ~2026-11-13. Status source: `PROJECT_STATUS.md` (prefer it over stale headers in `docs/02`).

## Aim

Streamlit chat that answers order, delivery, payment, return, refund, product, and policy questions.

- RAG over `data/source/FAQ.md` and `Policies.md` (policy source of truth).
- Product lookup from `data/source/Products.json` (read-only; not RAG; not a DB FK).
- SQLite tools, not open SQL: `get_order_status`, `create_return_request`, `get_refund_policy`.
- Simulated login (Streamlit session). Identity is app context, never message or LLM text.
- Escalation writes `support_tickets`. Cross-customer reads return no row ("not found").
- Out of MVP: marketplace, cart, checkout, real auth, real refunds/couriers, voice (stretch only).

## Built

Phase 0–1 docs approved. Phase 2 plan approved.

- **Checkpoint 1 (approved):** `app/db/connection.py`. `get_connection()` sets `PRAGMA foreign_keys = ON`, verifies it is `1`, `row_factory = sqlite3.Row`, raises `ConnectionError_`. Default path `data/app.db`. `app/db/__init__.py` re-exports `get_connection`. `main.py` is a smoke call.
- **Checkpoint 2 (approved 2026-11-06):** `app/db/schema.py`. `create_schema(connection)` creates the five tables. It does not open a connection.

## Not built

Do not treat these as existing. Checkpoint 3 is the next step in `docs/02` §7, but it is **not authorized**. Do not start it, or anything after it, until asked.

| Checkpoint | File | Status |
|---|---|---|
| 2 schema | `app/db/schema.py` | approved |
| 3 schema tests | `tests/test_schema.py` | not started |
| 4 seed | `app/db/seed_data.py` | not started |
| 5–8 reads/writes | `app/db/repository.py` | not started |
| 6 auth tests | `tests/test_authorization.py` | not started; must pass before orchestrator or UI |
| 9–11 return/ticket tests | `tests/` | not started |

Also absent: RAG, LangGraph orchestrator, Streamlit UI, eval set (~60–80; seed CSV has 6 rows), Docker, report, deck. `pyproject.toml` has no runtime deps. `src/ecommerce_support_assistant/` is an unused uv stub.

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