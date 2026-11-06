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

Phase 0–1 docs approved. Phase 2 plan approved. **Checkpoint 1 only:**

- `app/db/connection.py`: `get_connection()` sets `PRAGMA foreign_keys = ON`, verifies it is `1`, `row_factory = sqlite3.Row`, raises `ConnectionError_`. Default path `data/app.db` (gitignored).
- `app/db/__init__.py` re-exports `get_connection`. `main.py` is a smoke call.

## Not built

Do not treat these as existing. Next authorized step is **Checkpoint 2 only** (`app/db/schema.py`). Do not start 3+ until 2 is approved (`docs/02` §7).

| Checkpoint | File | Status |
|---|---|---|
| 2 schema | `app/db/schema.py` | not started |
| 3 schema tests | `tests/test_schema.py` | not started |
| 4 seed | `app/db/seed_data.py` | not started |
| 5–8 reads/writes | `app/db/repository.py` | not started |
| 6 auth tests | `tests/test_authorization.py` | not started; must pass before orchestrator or UI |
| 9–11 return/ticket tests | `tests/` | not started |

Also absent: RAG, LangGraph orchestrator, Streamlit UI, eval set (~60–80; seed CSV has 6 rows), Docker, report, deck. `pyproject.toml` has no runtime deps. `src/ecommerce_support_assistant/` is an unused uv stub.

## Constraints

- Five tables: `customers`, `orders`, `order_items`, `return_requests`, `support_tickets`. No payment or shipment tables. Design: `docs/01` §C.
- Every connection via `get_connection()`. Never `sqlite3.connect()` elsewhere.
- Money: INTEGER paise. Timestamps: TEXT ISO-8601 UTC.
- `orders.status`: Processing, Packed, Shipped, Out for Delivery, Delivered, Cancelled, Returned (`FAQ.md` Q1.4).
- Repository functions take `authenticated_customer_id` and filter in SQL. Composite FKs backstop consistency, not authorization.
- Seed must include C101→ORD1001 (Delivered, in window) and ORD1002; C102→ORD1003. `product_id` must exist in `Products.json` or fail. Do not edit `Products.json`.
- Return rules: 7 days (`Policies.md` §2); 48 hours if damaged/wrong (`FAQ.md` Q3.4).
- Do not claim a write succeeded without the backend confirming it.
