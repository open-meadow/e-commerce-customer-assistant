# Agent guide

Capstone: text customer-support assistant. Synthetic data only. No real store, payments, or shipping. Deadline ~2026-11-13. `docs/01` and `docs/02` are canonical for database design and checkpoint authorization; `.claude/plans/rest-plan.md` tracks delivery tasks. See `PROJECT_STATUS.md` for the current snapshot and the documented discrepancy between the checkpoint 7–9 status in `docs/02` and files present in this checkout. Do not silently resolve it.

## Aim

Target: Streamlit chat for order, delivery, payment, return, refund, product, and policy questions. Current chat behavior is a local fake-backend demo, not a connected AI support assistant.

- Planned: RAG over `data/source/FAQ.md` and `Policies.md` (policy source of truth).
- Planned: product lookup from `data/source/Products.json` (read-only; not RAG; not a DB FK).
- Database/repository operations exist, but the chat UI does not call them. The current fake backend performs no real order lookups, returns, ticket writes, or policy retrieval.
- Simulated login (Streamlit session) is not production authentication. Any future identity must come from application context, never message or LLM text.
- Out of MVP: marketplace, cart, checkout, real auth, real refunds/couriers, voice (stretch only).

## Implemented

The following are present in this checkout:

- Streamlit UI: `app/ui/streamlit_app.py`, session handling, and `app/ui/fake_backend.py` with canned responses and demo-only return-choice/confirmation flows.
- Database foundation: `app/db/connection.py`, `schema.py`, `seed_data.py`, repository functions, and tests.
- Docker setup: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, and `.env.example`. The Compose environment values are provisional and unused by the current chat.
- Safe demo DB initializer: `scripts/init_demo_db.py` and `tests/test_init_demo_db.py`.
- Evaluation: 70 synthetic cases in `data/eval/queries.csv`, a fake-backend smoke runner in `scripts/eval/run_eval.py`, sample logs, and tests. The runner does not assess correctness, safety, hallucinations, or tool calls.
- At the B5-6 verification on 2026-11-11, the full test suite passed
  (545 tests), the demo evaluator processed 70 cases, `uv sync --check`
  passed, and `docker compose config --quiet` passed.

## Not implemented or integrated

- No real backend orchestration, LLM, RAG, product lookup, tool execution, scope guard, or tool-call telemetry is wired to the chat.
- No real store, payment, refund, shipping, courier, or customer data integration exists.
- No production authentication exists.
- B5 documentation deliverables now present: `docs/architecture.md`,
  `docs/technical_report.md`, and `docs/presentation.md` plus
  `docs/presentation.pptx`. The B5-6 consistency review is complete.

### Database checkpoint status discrepancy

`docs/02_database_implementation_plan.md` currently records checkpoint 7
(write implementation) and checkpoints 8–9 as not started and not authorized.
However, `app/db/repository.py` contains `create_return_request` and
`create_support_ticket`, `tests/test_returns.py` and `tests/test_tickets.py`
are present, and the full test suite passed locally. Treat the code/tests as
present, but do not infer formal authorization or edit the canonical status
without Technical Lead direction.

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