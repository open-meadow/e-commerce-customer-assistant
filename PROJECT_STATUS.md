# Project Status

**Project:** AI E-Commerce Customer Support Assistant
**Deadline:** 12 days from Nov 1, 2026 → due ~Nov 13, 2026
**Current phase:** Phase 2 — Database Implementation. **Checkpoint 1 approved. Checkpoint 2 implemented, awaiting review. Checkpoint 3 not started.**

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
- Manual check on an in-memory connection from `get_connection()` (`uv run`, `PRAGMA foreign_keys` read back `1`): all five tables were created. A `return_requests` row whose `(order_id, customer_id)` did not match `orders` was rejected with `FOREIGN KEY constraint failed`. No return row was stored. **Awaiting Technical Lead approval.**

## In progress
- Checkpoint 2 is implemented and waiting for review. Checkpoint 3 has not been started.

## Pending work
- **Checkpoint 2 review** before any later checkpoint.
- **Checkpoint 3 — `test_schema.py`:** automated verification of the schema itself, including the corrected foreign-key-enforcement test. **Not started.**
- **Checkpoint 4 — `app/db/seed_data.py`:** synthetic customers/orders/items, including the fixed C101/C102/ORD1003 authorization scenario, with `product_id` values validated against `Products.json` (failing loudly on any unknown ID). **Not started.**
- **Checkpoint 5 — read-only repository functions.** **Not started.**
- **Checkpoint 6 — `test_authorization.py`** (cross-customer access test; must pass before any orchestration or UI code is written). **Not started.**
- **Checkpoints 7–11 — write repository functions, return/ticket integrity tests, full-suite verification.** **Not started.**
- RAG ingestion pipeline — **not started**
- Orchestrator (LangGraph) implementation — **not started**
- Streamlit UI — **not started**
- Evaluation set expansion (6 → ~60–80 queries) — **not started**
- Docker setup — **not started**
- Technical report, presentation deck — **not started**

Schema code exists in `app/db/schema.py` and the manual foreign-key check above passed. It is not approved yet. Schema tests, seed data, repository functions, RAG, LLM orchestration, and the Streamlit UI are not started.

## Open decisions (see `docs/01_database_design.md` Section L)
1. ~~Confirm no separate payment/shipment tables needed~~ — **Resolved:** single-table model approved.
2. Confirm simulated-login approach to identity (no real auth system)
3. Confirm no teacher-provided code/schema exists to incorporate
4. Confirm synthetic data volume (~5–8 customers, 2–3 orders each)
5. Non-returnable product category has no current seed product in `Products.json` — still open, `Products.json` not modified
6. `return_requests.status` and `support_tickets.status`/`priority` enums still need explicit sign-off

## Files inspected
- `Sample_Ecommerce_Capstone_Dataset.zip` → `FAQ.md`, `Policies.md`, `Products.json`, `customer_queries_sample.csv`, `README_DATASET.md`
- Two "master prompt" documents describing an expanded (voice + full auth) version of this project — treated as aspirational/future scope, not current requirements, per student's explicit decision

## Next recommended action
Review Checkpoint 2 (`app/db/schema.py`). Do not begin Checkpoint 3 or later until Checkpoint 2 is approved.
