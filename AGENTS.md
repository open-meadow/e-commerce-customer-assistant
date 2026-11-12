# Agent guide

Capstone: text customer-support assistant. Synthetic data only. No real store, payments, or shipping. Deadline ~2026-11-13. `docs/01` and `docs/02` are canonical for database design and checkpoint authorization; `.claude/plans/rest-plan.md` tracks delivery tasks. See `PROJECT_STATUS.md` for the current snapshot (refreshed 2026-11-12). `docs/01` to `docs/03` still carry some pre-merge path and status wording; report such conflicts, do not silently resolve them.

## Aim

Target: Streamlit chat for order, delivery, payment, return, refund, product, and policy questions, as a **modular Python monolith**: Streamlit UI, an in-process backend, and SQLite.

- The UI talks to the real backend by default (`UI_BACKEND=real`); `UI_BACKEND=fake` keeps a canned demo. Docker still runs the fake-backend UI.
- The default assistant is the offline `RuleBasedLLM`, a deterministic keyword stand-in and **not a language model**. It is the safe offline demo path (no key, no network). A configurable provider layer selects it, the OpenAI Responses API or Anthropic through `LLM_PROVIDER`, `LLM_MODEL` and a key variable. A misconfigured real provider fails closed; it never falls back to the stand-in.
- RAG over `app/db/data/source/FAQ.md` and `Policies.md` (policy source of truth) is lexical BM25 (no embeddings, no vector store).
- Product lookup from `app/db/data/source/Products.json` (read-only; not RAG; not a DB FK).
- The LLM proposes; code decides. The engine validates every tool call and writes only after an explicit confirmation on a pending action. Identity comes from the simulated login (Streamlit session), never from message or LLM text; it is not production authentication.
- Out of MVP: marketplace, cart, checkout, real auth, real refunds/couriers, voice (stretch only).

## Built

Phase 0–1 docs approved. Phase 2 plan approved. Database Checkpoints 1–10 complete. The D2 database contract (D3–D8), the backend (A2–A5), Person B's UI/Docker/evaluation/report work, the UI wiring to the real backend and the OpenAI Responses adapter (`f25b7c4`, merge `967cfe9`, pushed) are all merged on `master`. **Local `master` is four commits ahead of `origin/master`:** A3 `66c60fe` (real-backend evaluation runner), A4 `746b9b1` (documentation refresh), A5 `e8ab746` (verification script and demo runbook) and A6 (clean-clone acceptance and status refresh, the commit on top of `e8ab746`). All four are committed locally; none has been pushed. **Latest full-suite result: 1181 passed** (A6 clean-clone acceptance of `e8ab746`, offline, 2026-11-12, exit code 0; `scripts/verify_real_backend.py` 28/28 passed in the same clone). Older figures (497, 207, 939 and others) are obsolete.

- **1** `app/db/connection.py` (rewritten for D2): three factories, `APP_DB_PATH`, default `app/db/data/app_d2.db`, `user_version` checks, typed errors; the legacy `app/db/data/app.db` is protected and never opened. `main.py` only prints the run command.
- **2** `app/db/schema.py`: `create_schema(connection)` creates the five tables. It does not open a connection.
- **3** `tests/test_schema.py`.
- **4** `app/db/seed_data.py`: deterministic seed, fixed baseline `2026-11-07T00:00:00Z`.
- **5** `app/db/repository.py`: five customer-scoped read functions.
- **6** `tests/test_authorization.py`.
- **7A** Write-function contracts approved, committed, and pushed (`8ce2d43`): `docs/01` §F–§H, `docs/02` §4–§5.
- **7** `create_return_request` and `create_support_ticket` in `app/db/repository.py` (`6a2c11a`).
- **8** `tests/test_returns.py` and `tests/test_tickets.py` (`0f27739`).
- **9** Full-suite verification (the 497-test suite was replaced by the D2 suite; current full-suite result 1181 passed, see above). **10** Read-only repository inspection.
- **11A** `docs/03_backend_architecture.md` Revision 3.2 is the approved architecture baseline; the backend it describes is now implemented and merged.
- **Evaluation (A3, `66c60fe`, local):** `scripts/eval/real_eval.py` and `tests/test_real_eval.py`. See "Evaluation" below.
- **Demo and verification (A5, `e8ab746`, local):** `scripts/verify_real_backend.py` (28 offline business-behaviour checks on a fresh temporary database, `RuleBasedLLM` only; run `uv run python scripts/verify_real_backend.py`), `tests/test_verify_real_backend.py` and the runbook `docs/06_demo_runbook.md`. **A6 clean-clone acceptance** (local clone of `e8ab746`; full suite 1181 passed, verification script 28/28, fake-backend Streamlit smoke test on loopback with a temporary `APP_DB_PATH`, all offline) is recorded in `PROJECT_STATUS.md`. It does not cover the real-backend UI path or any language model.

## Not built, not verified, or pending

Do not treat these as existing. Do not start a checkpoint that has not been authorized.

- **Not verified live:** the OpenAI Responses and Anthropic adapters are tested only with a mocked transport (`tests/test_providers.py`). One guarded one-request OpenAI probe is reported to have succeeded (technical lead); it is not an application, tool-calling or multi-turn evaluation. **No full live evaluation has been done, and none may be run without explicit authorization.**
- **Not a language model:** `RuleBasedLLM`. Offline evaluation results describe it, not a model.
- **Out of scope by design:** embeddings, durable pending actions and idempotency (single process, in-memory), real authentication, voice.
- **Pending:** review of Person B's work, Person B's report/diagram/README/deck refresh for the real backend, Docker from a clean clone (the image still runs the fake UI; not run in A6), publishing the four local commits (A3, A4, A5, A6), and path corrections in `docs/01` to `docs/03`. **A real-backend UI smoke test in a clean clone remains outstanding** (the A6 Streamlit smoke test passed on the fake backend only). Full live application and tool-calling evaluation with a real provider remains outstanding and needs explicit authorization; everything measured so far is offline (`RuleBasedLLM`).
- **Known assistant gaps (not fixed):** Q034 cancel pattern, Q070 clarification, and the placeholder `support@example.com` in the source documents repeated in replies. See `PROJECT_STATUS.md`.

## Evaluation

- `uv run python scripts/eval/run_eval.py` is the **fake-runner**: it exercises the canned fake UI backend; every score is `not_assessed`. It measures nothing about the real assistant.
- `uv run python scripts/eval/real_eval.py [--output PATH] [--cases PATH]` runs the 70 cases of `app/db/data/eval/queries.csv` plus five multi-turn scenarios against the **real backend** on a temporary seeded database, offline with `RuleBasedLLM`. Checks are mechanical and tagged `backend_code`, `llm_proposals`, `llm_text` or `provider_and_backend`; semantic answer quality is never scored. `--compare FIRST SECOND` compares two result files.
- `--llm env` requires an explicit real provider (`LLM_PROVIDER` of `openai` or `anthropic`, `LLM_MODEL`, the key) and `--allow-live`. An unset, blank or `rule_based` provider is an error. **Never run it, or any live LLM call, without explicit authorization.** Never write credentials to logs or result files.
- Detail, limits and evidence: `docs/04_frontend_handover.md` Section 10.

## Constraints

- This is a **uv** project. Run Python with `uv run`. Do not create a separate venv or use pip.
- SQLite is Python’s standard-library `sqlite3` module (confirmed via `uv run`; library 3.50.4). It is not a `pyproject.toml` dependency. Do not `uv add` a SQLite package.
- Five tables: `customers`, `orders`, `order_items`, `return_requests`, `support_tickets`. No payment or shipment tables. Design: `docs/01` §C.
- Every connection via the three factories in `app/db/connection.py`. Never `sqlite3.connect()` elsewhere. `APP_DB_PATH` selects the database; only the initialization factory creates files and it refuses `app/db/data/app.db`.
- **Branches and Git.** Database work was done on `rukum/db` and backend work on `rukum/backend`; both are now merged into `master`, and that branch rule predates the merge. Do not create or switch branches, stage, commit, push, amend, rebase or merge without the user's explicit approval for that operation; approval for one operation does not extend to the next. Ask where new work should go.
- **`app/db/data/app.db` must NEVER be opened or accessed** (open, read, seed, reset, migrate or delete). It is a legacy pre-D2 database (`user_version` 0, never stamped) and is protected in code: no connection factory opens it. No authorization changes this.
- **`app/db/data/app_d2.db` is protected.** It is the application's default database (override with `APP_DB_PATH`; git-ignored). It must not be opened, read, initialized, seeded or modified unless the technical lead explicitly authorizes that specific access (initializing or seeding it is an operator action, `python -m app.db.seed_data`).
- Normal development, tests, evaluation and UI verification use a **temporary or demo database** (`temp_db` in tests; `real_eval.py` builds its own; a seeded `demo/demo.db` for the UI). Before running Streamlit, check that `APP_DB_PATH` (real environment first, then `.env`, whose template `.env.example` sets `app/db/data/app_d2.db`) resolves to such a database. If it resolves to `app_d2.db`, do not start the application without the technical lead's explicit authorization. There is no migration engine (M2).
- **No live LLM call** without explicit authorization. Offline (`RuleBasedLLM`) is the default for tests, evaluations and demos. Never log or store credentials.
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