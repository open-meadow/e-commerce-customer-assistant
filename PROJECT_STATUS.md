# Project Status

**Project:** AI E-Commerce Customer Support Assistant
**Deadline:** 12 days from Nov 1, 2026 → due ~Nov 13, 2026
**Current phase:** Database completion (Checkpoint D series). Database Checkpoints 1–10 are complete per the recorded history and Git (`master` at `3c3626a`). **The current task is D1, the documentation baseline** (this reconciliation). The `reason_category` column (D2) is approved in principle but not implemented. Do not begin D2 or any later checkpoint until asked.

*(Status baseline D1, 2026-11-11. The body below was written during Checkpoints 1 and 2. Where it says a checkpoint is "not started", the "Checkpoint status as of D1" section below is the current statement.)*

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

## Checkpoint status as of D1 (2026-11-11)
- **Complete:** Checkpoint 3 (`tests/test_schema.py`), 4 (`app/db/seed_data.py`), 5 (read functions), 6 (`tests/test_authorization.py`), 7A (write contracts, `8ce2d43`), 7 (write functions, `6a2c11a`), 8 (`tests/test_returns.py`, `tests/test_tickets.py`, `0f27739`), 9 (full-suite verification) and 10 (read-only inspection).
- **Test result:** **497 passed is a historical result** recorded earlier. It was not re-run for the D1 baseline and is not freshly verified.
- **Backend architecture:** `docs/03_backend_architecture.md` Revision 3.2 is the approved baseline, pending one amendment for the `reason_category` column (D2). It is design only.
- **Not built:** `app/tools`, `app/rag`, `app/orchestrator`, the backend service, the LLM adapter and RAG. `app/ui/` is an early Streamlit app with stub replies.
- **Pending (D2):** a dedicated `reason_category` column on `return_requests`. Approved in principle, not implemented, not yet documented as a design amendment.

## In progress
- D1 documentation baseline (documentation only).

## Pending work
- Database Checkpoints 3–10 are complete (see "Checkpoint status as of D1" above). No database checkpoint from the original plan remains pending.
- **D2 — `reason_category` column on `return_requests`:** approved in principle, **not started**, not yet documented as a design amendment.
- RAG ingestion pipeline — **not started**
- Orchestrator (LangGraph) implementation — **not started**
- Streamlit UI — **early stub only** (`app/ui/`, stub replies, not connected to a backend)
- Evaluation set expansion (6 → ~60–80 queries) — **not started**
- Docker setup — **not started**
- Technical report, presentation deck — **not started**

Database Checkpoints 1–10 are complete. RAG, LLM orchestration, the tool layer and the backend service are not started. The Streamlit UI is an early stub.

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
D1 (documentation baseline) is the current task. After it is reviewed, the next step is D2 (`reason_category` design amendment and implementation plan). Do not begin D2 until asked.
