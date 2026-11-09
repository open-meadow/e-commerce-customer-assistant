# Database Implementation Plan — AI E-Commerce Customer Support Assistant

**Phase:** 2 — Database Implementation
**Status:** Plan approved. Checkpoints 1–6 are complete, reviewed, tested, committed, and pushed (connection, schema, schema tests, seed data, customer-scoped read functions, authorization tests). Checkpoint 7A (documentation contract for the two write functions) is in progress: corrections are made, but 7A is not approved until the Technical Lead reviews the final diff. Checkpoint 7 (implementation of `create_return_request` and `create_support_ticket`) has not started. Checkpoints 8 onward are not yet approved. The sections below remain the specification; they are not a claim that later checkpoints exist.
**Input documents:** `docs/01_database_design.md` (Technical Lead approved, Phase 1), `PROJECT_STATUS.md`
**Scope of this document:** sequencing, contracts, and checkpoints for implementation. **No code, migrations, seed scripts, or test files are created in this task** — those are the work this plan schedules, not what it contains.

---

## 0. Boundaries Carried Forward From the Approved Design

This plan changes nothing about Phase 1's decisions. Restated here only so implementation tasks have a fixed target:

- Exactly five tables: `customers`, `orders`, `order_items`, `return_requests`, `support_tickets`.
- SQLite, with `PRAGMA foreign_keys = ON` required on every connection.
- Composite foreign keys: `return_requests(order_id, customer_id) → orders(order_id, customer_id)`, `return_requests(item_id, order_id) → order_items(item_id, order_id)`, `support_tickets(order_id, customer_id) → orders(order_id, customer_id)` (NULL-conditional).
- Money as INTEGER paise. Timestamps as TEXT, ISO-8601 UTC.
- Enums exactly as approved (Section D of the design doc) — `orders.status` is the one dataset-sourced fact; the other five enum sets are engineering proposals already signed off.
- `Products.json` is read-only. It is not modified, and no database foreign key targets it (`product_id` is validated at the application layer).
- Customer authentication remains simulated (Streamlit session selector); the database and repository layers only ever trust an `authenticated_customer_id` that the application layer hands them — they do not verify login.
- Return windows: 7-day standard (`Policies.md` §2), 48-hour damaged/wrong-item exception (`FAQ.md` Q3.4). No additional rules.
- Reference scenario fixed: `C101` owns `ORD1001` (Delivered, within window) and `ORD1002`; `C102` owns `ORD1003`.

Anything below that looks like a new decision is flagged explicitly as such, not folded in silently.

---

## 1. Implementation Architecture

### Layer responsibilities

| Layer | Responsibility | Must not do |
|---|---|---|
| **Database layer** (schema + SQLite file) | Hold the five tables, their constraints (PK, composite UNIQUE, composite FK, CHECK), and indexes exactly as specified in `01_database_design.md` Section C. Enforce relational *consistency* (an order_id/customer_id pair, or item_id/order_id pair, genuinely exists together) at the storage level. | Decide *who* is allowed to ask for a row. The schema has no concept of a current session. |
| **Connection layer** | Open SQLite connections, execute `PRAGMA foreign_keys = ON;` on every single connection before any other statement runs, and provide a single shared entry point so no code path can open a connection that skips the pragma. | Contain any business logic, ownership checks, or table-specific queries. |
| **Repository layer** | Expose one function per operation listed in Section 4 below. Every function that touches customer data takes `authenticated_customer_id` as an explicit, required parameter and uses it as a `WHERE`-clause filter in the SQL itself. This is where ownership is actually enforced for reads; for writes, it runs the pre-insert validation queries from the design doc (Sections F/G) before issuing the insert, inside a transaction. | Accept a customer ID from anywhere other than its own function parameter. Never parses a customer ID out of a user message, an LLM tool-call argument, or a product of conversation history — those are not valid inputs to this layer, by construction of the function signatures in Section 4. |
| **Test layer** | Exercise the database and repository layers directly, independent of any orchestration, UI, or LLM. Proves the schema's constraints and the repository's ownership filtering actually behave as designed, using the fixed reference scenario and an injectable reference time (design doc Section I). | Depend on Streamlit, LangGraph, or any LLM call — these tests must be runnable with nothing but Python and SQLite. |

### How `authenticated_customer_id` flows

```
Streamlit session state (simulated login selector)
        │
        │  passed explicitly as a function argument — never read implicitly,
        │  never inferred, never defaulted
        ▼
Repository function call, e.g.:
    list_customer_orders(authenticated_customer_id="C101")
        │
        │  used directly inside the SQL WHERE clause for this call
        ▼
    SELECT ... FROM orders WHERE customer_id = ?   -- parameter, not string-built
        │
        ▼
Rows returned are, by construction, only ever rows C101 owns.
```

This is a **design constraint on every repository function's signature**, not an optional convention: every function in Section 4 that touches `orders`, `order_items`, `return_requests`, or `support_tickets` has `authenticated_customer_id` as its first required parameter, and the parameter is documented as coming only from trusted application context — callers (the future orchestrator, in a later phase) are expected to pass the Streamlit session's value, never anything derived from the LLM or user text. This matches design doc Section E exactly; nothing new is introduced here.

### Where ownership checks happen

Two places, matching the design doc's two-layer model (Section B's composite-FK discussion):

1. **Repository layer, on every read and before every write** — the primary, always-active mechanism. A read for a row the caller doesn't own simply returns no rows (never a different row, never an error that reveals the row exists). A write first runs the validation queries from Sections F/G of the design doc; only if those pass does the insert proceed.
2. **Database layer, only on write, as a backstop** — the composite foreign keys reject an insert whose `(order_id, customer_id)` or `(item_id, order_id)` pairing isn't internally consistent, even if repository code had a bug and skipped its own check. As the design doc states, this backstop proves *consistency*, not *authorization* — it cannot and does not replace the repository-layer check.

---

## 2. Proposed Folder Structure

```
ecommerce-support-assistant/
├── app/
│   └── db/
│       ├── __init__.py          # Package marker only. No logic.
│       ├── schema.py            # DDL: CREATE TABLE statements for all five tables,
│       │                        #   exactly matching 01_database_design.md Section C
│       │                        #   (columns, types, defaults, PK/FK/composite FK/UNIQUE/CHECK).
│       │                        #   One function, e.g. create_schema(connection), that
│       │                        #   executes the DDL. No seed data here — schema only.
│       ├── connection.py        # The single function(s) responsible for opening a SQLite
│       │                        #   connection and immediately executing
│       │                        #   PRAGMA foreign_keys = ON;. Every other module that
│       │                        #   needs a connection imports from here — there must be
│       │                        #   no second code path that opens sqlite3.connect()
│       │                        #   directly, since that would bypass the pragma.
│       ├── seed_data.py         # Builds the fixed reference scenario (C101/C102/etc.) plus
│       │                        #   the broader coverage list from design doc Section J
│       │                        #   (every orders.status value, both payment/refund
│       │                        #   variations, both return-window cases, a multi-item
│       │                        #   order). Uses the injectable reference time (Section I)
│       │                        #   for all relative dates rather than hardcoded calendar
│       │                        #   dates. Does not touch Products.json's contents, only
│       │                        #   reads product_id values from it.
│       └── repository.py        # The functions listed in Section 4 below. The only module
│                                 #   that other parts of the application (a future
│                                 #   orchestrator) are meant to call for any database
│                                 #   access. No raw SQL is exposed outside this file.
├── tests/
│   ├── __init__.py
│   ├── test_schema.py           # Verifies the schema itself: all five tables exist with
│   │                             #   the right columns/types; composite UNIQUE and FK
│   │                             #   constraints exist and are enforced; CHECK constraints
│   │                             #   (delivered_at consistency, refund_amount/refund_status
│   │                             #   consistency) reject invalid rows. Maps to design doc
│   │                             #   Section K test 12 (corrected) plus schema-level parts
│   │                             #   of tests 5b and 11.
│   ├── test_authorization.py    # The priority test file named in PROJECT_STATUS.md.
│   │                             #   Covers the ownership-denial tests: 1, 2, 2b, 3, 4, 6
│   │                             #   from Section K — must pass before any orchestration
│   │                             #   or UI code is written. Preserved rule: the return
│   │                             #   (test 4) and ticket (test 6) ownership-denial cases
│   │                             #   must create actual return_requests and support_tickets
│   │                             #   rows belonging to more than one customer (not only
│   │                             #   orders), then query through the customer-scoped
│   │                             #   repository functions (get_customer_returns,
│   │                             #   get_customer_tickets) to verify each customer's
│   │                             #   results contain only their own records — not merely
│   │                             #   assert that a single cross-customer lookup is denied.
│   ├── test_returns.py          # Return request integrity and eligibility: 5, 5b, 7, 7b,
│   │                             #   8, 8b, 9 from Section K.
│   └── test_tickets.py          # Ticket integrity: 10 from Section K, plus the "ticket
│                                 #   still created, order_id cleared" behavior from design
│                                 #   doc Section G.
├── data/
│   └── app.db                   # The SQLite database file itself. Not checked into
│                                 #   version control (added to .gitignore); created fresh
│                                 #   by running schema.py + seed_data.py locally or at
│                                 #   container startup.
└── (existing docs/, data/knowledge_base/, data/Products.json, data/customer_queries_sample.csv
    — unchanged, already present from Phases 0–1)
```

**Why this and nothing more:** five files under `app/db/` map one-to-one onto the five responsibilities named in Section 1 (schema, connection, seed data, repository, and — in `tests/` — verification). No separate "models" layer, ORM, or service layer is introduced; the design doc's own repository-function contracts (Section 4 below) are plain functions taking and returning plain Python values, which is sufficient for five tables and a fixed set of operations. Adding an ORM or a models/schemas split now would be the kind of unnecessary abstraction the task explicitly asks to avoid — if a future phase's orchestrator needs richer typed objects, that can be layered on top of `repository.py`'s return values without changing this structure.

---

## 3. Implementation Sequence

**Corrected per Technical Lead review:** the prior draft of this section listed schema creation before connection setup, which was backward — a schema cannot be meaningfully created, let alone proven to enforce its composite foreign keys, on a connection that hasn't already had `PRAGMA foreign_keys = ON;` executed on it. This section now follows the same nine-step order already established in Section 7's checkpoints, restated here as a sequence rather than a checkpoint table. Each step depends on every step before it; no step can be meaningfully verified until all prior steps are in place.

1. **Connection setup** (`connection.py`) — the single connection-opening function, executing `PRAGMA foreign_keys = ON;` immediately on every new connection, before any other statement is ever run against it. This must exist first, since every later step — schema creation, seeding, repository reads and writes, and every test — depends on connections that actually enforce foreign keys; building or testing anything against an unconfirmed connection would make every downstream result unreliable.
2. **Schema creation** (`schema.py`) — all five tables, in dependency order (`customers` → `orders` → `order_items` → `return_requests`/`support_tickets`, since the latter two reference the former via composite FKs), with every constraint from design doc Section C, created only against connections from step 1.
3. **Schema tests** (`test_schema.py`) — the first automated verification in the sequence, run immediately after schema creation rather than deferred to the end: confirms the composite FKs, CHECK constraints, and the FK-pragma-enforcement behavior (design doc Section K test 12, corrected) actually work together, before any seed data or repository code is built on top of an unverified foundation.
4. **Synthetic seed-data generation** (`seed_data.py`) — populates the fixed reference scenario plus the full coverage list from design doc Section J, using the injectable reference time rather than hardcoded dates, and validating every `product_id` against `Products.json`, failing loudly on any unknown ID (see Section 8, item 4 — this is a preserved rule, not a new one).
5. **Read-only repository functions** (`list_customer_orders`, `get_order`, `get_order_items`, `get_customer_returns`, `get_customer_tickets` in `repository.py`) — implemented against the now-seeded database.
6. **Authorization tests** (`test_authorization.py`) — run against steps 1–5. Per `PROJECT_STATUS.md`, these must pass before any orchestration or UI code is written in a later phase.
7. **Write repository functions** (`create_return_request`, `create_support_ticket` in `repository.py`) — implemented after the authorization gate (step 6) passes, since the write paths' own validation logic depends on the same ownership-query pattern the read functions and authorization tests have already proven correct.
8. **Return and ticket integrity tests** (`test_returns.py`, `test_tickets.py`) — run against the write functions from step 7 and the eligibility-window seed data from step 4.
9. **Full-suite verification** — all test files run together, with actual output reviewed, not assumed.

This ordering is now identical to Section 7's checkpoint sequence; the two sections are cross-referenced rather than independently maintained; see Section 7 for acceptance criteria and explicit dependency statements per step.

---

## 4. Repository Contracts

All functions below take `authenticated_customer_id: str` as their first parameter except where noted, and are read-only unless marked **(write)**. Signatures are proposed Python type-hint style for clarity; actual implementation is a later task, not this document.

### `list_customer_orders(authenticated_customer_id: str) -> list[OrderRecord]`
- **Input:** the authenticated customer's ID only — no order ID, since this lists *all* their orders.
- **Output:** a list of order records (order_id, status, payment_status, refund_status, refund_amount, order_date, delivered_at, total_amount), filtered to `WHERE customer_id = :authenticated_customer_id`. Empty list if the customer has no orders — never an error.
- **Ownership check:** the `WHERE` filter itself; there is no other customer's data this query could ever return, by construction.

### `get_order(authenticated_customer_id: str, order_id: str) -> OrderRecord | None`
- **Input:** authenticated customer ID and the requested order ID.
- **Output:** the single order record if it exists **and** belongs to that customer; `None` otherwise.
- **Ownership check:** `WHERE customer_id = :authenticated_customer_id AND order_id = :order_id`. Per design doc Section E, a `None` result must be indistinguishable *at the repository return-value level* whether the order doesn't exist at all or belongs to someone else — this function's contract is exactly "return the order if and only if this customer owns it," which already gives callers no way to tell the two cases apart from the return value alone. This is a repository-layer guarantee only; whether a later orchestration layer phrases its customer-facing message identically for both cases is that later layer's own responsibility to implement and test, not something this function or this phase verifies (see Section 6, test 2b).

### `get_order_items(authenticated_customer_id: str, order_id: str) -> list[OrderItemRecord]`
- **Input:** authenticated customer ID and order ID.
- **Output:** list of item records (item_id, product_id, quantity, price) for that order — but only if the order belongs to the authenticated customer; empty list otherwise (same non-distinguishing behavior as `get_order`).
- **Ownership check:** per design doc Section E, implemented as a single query that JOINs `order_items` to `orders` and filters by both the authenticated customer ID (`orders.customer_id`) and the requested order ID. It is not a two-step lookup, and no `order_items` row is read without the ownership-filtered JOIN to `orders`.

### `create_return_request(authenticated_customer_id, order_id, item_id, reason, *, reason_category, reference_time=None)` **(write)**
Full contract: design doc Sections F, H, and I. Summary:
- **Input:** authenticated customer ID (trusted context only), order ID, item ID, free-text `reason` (stored as given, never interpreted), keyword-only `reason_category` (`damaged`, `wrong_product`, or `standard`; selects the window), and keyword-only optional `reference_time`.
- **Windows:** `damaged` and `wrong_product`: 48 hours; `standard`: 7 × 24 hours, measured from `orders.delivered_at`. Boundaries are inclusive. A NULL, future, or invalid `delivered_at` is `outside_window`.
- **Reference time:** `None` reads `datetime.now(timezone.utc)` exactly once. A supplied value must be a timezone-aware `datetime` or an ISO-8601 string with an explicit offset, normalized to UTC (naive datetime, or unparseable/offset-less string: `ValueError`; other types: `TypeError`). Whole-second precision means truncation (microseconds dropped, never rounded). One normalized instant serves both eligibility and `requested_at`; the clock is never read twice. (`seed_database(None)` keeps its fixed baseline `2026-11-07T00:00:00Z`; that is a different function.)
- **Validation:** wrong type: `TypeError`; invalid value, including blank or whitespace-only strings or an unknown `reason_category`: `ValueError`. Validation completes before any transaction opens.
- **Check order** (inside one `BEGIN IMMEDIATE` transaction): ownership (`not_found`), item belongs to order (`item_mismatch`), existing active return (`duplicate`), eligibility window (`outside_window`), insert.
- **Output:** exactly `{"outcome": ..., "return_id": ...}` where `outcome` is `created`, `not_found`, `item_mismatch`, `outside_window`, `duplicate`, or `failed`. Only `created` carries a non-null `return_id` (`RET-` plus 12 uppercase hex characters from a UUID4). `created` is returned only after `COMMIT` succeeds. `failed` is generic and carries no database text.
- **Known MVP limitation:** category-based non-returnable enforcement is not implemented because `Products.json` has no returnability metadata (design doc Section L, item 1). No `non_returnable` outcome or schema field is added.

### `create_support_ticket(authenticated_customer_id: str, issue_type: str, description: str, order_id: str | None = None, conversation_summary: str | None = None) -> SupportTicketResult` **(write)**
Full contract: design doc Sections G and H. Summary:
- **Input:** authenticated customer ID (trusted context only), required `issue_type` and `description`, optional `order_id`, optional `conversation_summary`.
- **Validation:** wrong type: `TypeError`; invalid value, including blank or whitespace-only strings: `ValueError`. Blank strings are never converted to `None`. `order_id=None` creates a general ticket; a non-empty string is checked for ownership; a blank string raises `ValueError`; a non-string raises `TypeError`. `conversation_summary=None` is stored as SQL NULL; a non-empty string is stored; a blank string raises `ValueError`; a non-string raises `TypeError`.
- **Ownership check:** when `order_id` is a non-empty string, the same ownership query as `get_order` runs inside the transaction. If the order is nonexistent or owned by another customer, the ticket is still created with `order_id = NULL` and the same result as for a nonexistent order. Existence of an unowned order is never disclosed.
- **Output:** `{"outcome": "created", "ticket_id": "TKT-<12 uppercase hex>", "order_id": <owned order ID or None>, "order_linked": <bool>}` after `COMMIT`; on database failure `{"outcome": "failed", "ticket_id": None, "order_id": None, "order_linked": False}`.
- **Timestamps:** `status`, `priority`, and `created_at` use the schema defaults. `created_at` defaults to `strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` (UTC, approved format, SQLite clock). No `reference_time` parameter.

### `get_customer_returns(authenticated_customer_id: str) -> list[ReturnRequestRecord]`
- **Input:** authenticated customer ID only.
- **Output:** all return requests belonging to that customer (`WHERE customer_id = :authenticated_customer_id`), regardless of status.
- **Ownership check:** the `WHERE` filter, same pattern as `list_customer_orders`.

### `get_customer_tickets(authenticated_customer_id: str) -> list[SupportTicketRecord]`
- **Input:** authenticated customer ID only.
- **Output:** all support tickets belonging to that customer.
- **Ownership check:** the `WHERE` filter, same pattern.

**Not included in this list, and deliberately so:** no function accepts a bare `return_id` or `ticket_id` without the authenticated customer ID also being required and filtered on — design doc Section E's ownership table (`return_requests`/`support_tickets` rows) is honored by every lookup going through the customer-scoped list functions above rather than adding single-record-by-ID lookups that would need their own separate ownership check to be written and could be forgotten.

---

## 5. Transaction and Error Handling

**Writes.** Both write operations (`create_return_request`, `create_support_ticket`) follow the same pattern, per design doc Section H:
1. Validate inputs first (wrong type: `TypeError`; invalid value, including blank strings: `ValueError`). No transaction is open yet.
2. Open the connection through `get_connection()` and run `BEGIN IMMEDIATE`.
3. Run the ownership and business checks, then the parameterized `INSERT`, in that one transaction. A business rejection rolls back and returns its result dictionary; no partial row remains.
4. Return `created` only after `COMMIT` succeeds.

**Exception handling.**
- `sqlite3.Error` during database operations: roll back if a transaction is open, then return the function's generic `failed` dictionary. The result carries no SQLite message, SQL text, or exception type.
- Any other exception (a programming error): roll back if a transaction is open, then re-raise. It is never converted to `failed` or any other result. Do not catch every exception.
- If rollback itself fails, the rollback error never replaces the original: on the `sqlite3.Error` path return `failed`; otherwise re-raise the original exception.
- `ConnectionError_` raised by `get_connection()` during connection acquisition returns the function's generic `failed` dictionary. It is not propagated. No transaction was opened, so nothing is rolled back.
- Raw database error details are never exposed through a write-operation result.

**Duplicate return submissions.** Handled at two levels, per design doc Section H:
1. The repository function checks for an existing *active* (`Requested` or `Approved`) return request for the same `item_id` before attempting the insert, and returns a `duplicate` result without touching the database further if one exists.
2. The database's unique partial index (`return_requests(item_id) WHERE status IN ('Requested','Approved')`) is the actual guarantee — if the application-level check were ever bypassed or raced, the database itself refuses the second insert. The repository function translates a `sqlite3.IntegrityError` caused by `uq_active_return_per_item` into the same `duplicate` result the application-level check would have produced, so the caller sees one consistent outcome regardless of which layer caught it. No raw SQLite text is exposed. An `IntegrityError` from any other constraint is a generic `failed`, not `duplicate`.

**Translating database errors into safe application-level results.** For the **write** functions, the repository layer is the only place that sees a raw `sqlite3` exception, and no `sqlite3.Error` propagates to the caller. Business outcomes (`not_found`, `item_mismatch`, `outside_window`, `duplicate`) come from explicit checks, plus the single `uq_active_return_per_item` translation above. Any other `sqlite3.Error` (e.g., a disk I/O failure or other constraint) becomes the generic `failed` result — the caller never receives a raw exception, a SQL error string, or any detail that could reveal schema internals or another customer's data. Non-database exceptions (programming errors) are rolled back and re-raised, never masked as results. The existing read functions propagate `sqlite3.Error` unmodified, as already implemented and tested; this section does not change them. This matters for the same reason as the "same not-found result" rule in Section E: an error message is itself a channel that could leak information if not deliberately controlled.

---

## 6. Testing Strategy

**Preserved rule (Technical Lead review):** wherever an authorization test concerns returns or tickets (tests 4 and 6 below), the test must create actual `return_requests` and `support_tickets` records belonging to **multiple distinct customers** in the seeded test database, then call the customer-scoped repository functions (`get_customer_returns`, `get_customer_tickets`) for each customer and assert that each customer's result set contains only their own records. A test that merely attempts one cross-customer lookup and checks it returns nothing is not sufficient on its own — the multi-customer, multi-record setup is what proves the `WHERE customer_id = :authenticated_customer_id` filter actually discriminates between customers, rather than happening to return nothing because only one customer's data exists in the test.

Every test from design doc Section K mapped to its implementation test and file:

| Design doc test # | Description | Implementation test | File |
|---|---|---|---|
| 1 | Valid customer order lookup | `test_valid_order_lookup` | `test_authorization.py` |
| 2 | Cross-customer order access denial | `test_cross_customer_order_denied` | `test_authorization.py` |
| 2b | **Not-found equivalence — repository scope only** (clarified per Technical Lead review) | `test_not_found_equivalence` — asserts `get_order("C101", "ORD1003")` and `get_order("C101", "ORD9999")` both return exactly `None`, with no distinguishing exception type, error code, or return-value shape between the two calls at the repository layer. **This test verifies repository-level equivalence only.** Whether the *user-facing* application response (the eventual chat message shown to the customer) is also worded identically for both cases is explicitly **not** tested here and is deferred to the later orchestration/application phase, where the response-generation logic that turns a `None` into customer-facing text will need its own equivalence test. This document makes no claim that application-level equivalence has been verified at this stage. | `test_authorization.py` |
| 3 | Cross-customer order-item access denial | `test_cross_customer_items_denied` | `test_authorization.py` |
| 4 | Cross-customer return access denial | `test_cross_customer_return_denied` — creates return requests for both C101 and C102, then asserts `get_customer_returns("C101")` contains only C101's records and `get_customer_returns("C102")` contains only C102's (multi-customer setup, per the preserved rule above) | `test_authorization.py` |
| 5 | Invalid return/order/item relationship | `test_item_order_mismatch_rejected` | `test_returns.py` |
| 5b | Composite FK rejection at DB layer | `test_composite_fk_rejects_invalid_pair` — inserts directly against the schema, bypassing `repository.py`, to prove the database-level backstop independent of repository code | `test_schema.py` |
| 6 | Cross-customer ticket access denial | `test_cross_customer_ticket_denied` — creates support tickets for both C101 and C102, then asserts `get_customer_tickets("C101")` contains only C101's records and `get_customer_tickets("C102")` contains only C102's (multi-customer setup, per the preserved rule above) | `test_authorization.py` |
| 7 | Valid return eligibility (standard window) | `test_eligible_standard_window` | `test_returns.py` |
| 7b | Valid return eligibility (48-hour window) | `test_eligible_damaged_window` | `test_returns.py` |
| 8 | Invalid return eligibility (expired standard window) | `test_ineligible_standard_window_expired` | `test_returns.py` |
| 8b | Invalid return eligibility (expired 48-hour window) | `test_ineligible_damaged_window_expired` | `test_returns.py` |
| 9 | Duplicate return submission | `test_duplicate_return_rejected` | `test_returns.py` |
| 10 | Ticket creation with unowned order_id | `test_ticket_unowned_order_cleared` | `test_tickets.py` |
| 11 | Refund consistency rule | `test_refund_amount_status_consistency` | `test_schema.py` |
| 12 | **Foreign-key enforcement on the actual connection path** (corrected per design doc revision round 2) | `test_fk_pragma_set_and_enforced` — opens a connection via the real `connection.py` entry point, asserts `PRAGMA foreign_keys` reads `1`, then asserts a composite-FK-violating insert is rejected on that connection; `test_fk_disabled_demonstrates_failure_mode` (optional, separate, clearly-labeled test) opens a connection that skips the pragma and documents that the same insert is accepted there, strictly as an illustration, never as evidence used by the first test | `test_schema.py` |

**Why `test_authorization.py` is its own file, not folded into `test_schema.py` or a general test file:** `PROJECT_STATUS.md` names it explicitly as the gate that must pass before any orchestration or UI code is written. Keeping it separate makes "has this gate passed" a one-command, unambiguous check (`pytest tests/test_authorization.py`) rather than something buried inside a larger test run.

**Reference time in tests:** every test touching return-eligibility windows (7, 7b, 8, 8b) passes an explicit `reference_time` to `create_return_request` (for example the seed baseline `2026-11-07T00:00:00Z` or an offset from it), per design doc Section I, so the suite stays reproducible regardless of when it is run. `create_return_request(reference_time=None)` uses the real current UTC time; at most one test may exercise that default, and it must assert only that `requested_at` is a well-formed `YYYY-MM-DDTHH:MM:SSZ` value within a tolerance of the real clock. `seed_database(None)` keeps its fixed baseline.

---

## 7. Implementation Checkpoints

Each checkpoint is small, has a clear pass/fail acceptance criterion, and lists what it depends on. No checkpoint after Checkpoint 2 can be verified until Checkpoints 1–2 pass, since the FK pragma must be confirmed active before any constraint-dependent test means anything.

| # | Task | Acceptance criteria | Depends on |
|---|---|---|---|
| 1 | Implement `connection.py` | A connection opened through this module, queried with `PRAGMA foreign_keys;`, reads back `1`. | None |
| 2 | Implement `schema.py` | All five tables created with no errors against a connection from Checkpoint 1; a manual attempt to insert a `return_requests` row with a mismatched `(order_id, customer_id)` pair is rejected (proves the composite FK is live, not just declared). | 1 |
| 3 | Write `test_schema.py` (design doc tests 5b, 11, 12) | All three pass against Checkpoints 1–2. This is the first automated proof that the pragma-plus-constraints combination actually works, not just a manual check. | 1, 2 |
| 4 | Implement `seed_data.py` | Running it against a fresh schema populates exactly the fixed reference scenario (C101/C102/ORD1001/ORD1002/ORD1003) plus the full coverage list from design doc Section J; re-running it from scratch produces the same data (deterministic, given the same injected reference time). | 2 |
| 5 | Implement read-only repository functions (`list_customer_orders`, `get_order`, `get_order_items`, `get_customer_returns`, `get_customer_tickets`) | Each function's contract from Section 4 is met against the seeded database; manual spot-check confirms `get_order("C101", "ORD1003")` returns `None`. | 4 |
| 6 | Write `test_authorization.py` (design doc tests 1, 2, 2b, 3, 4, 6) | All pass. This checkpoint is the one `PROJECT_STATUS.md` names as the gate before orchestration/UI work begins. | 5 |
| 7A | Finalize the write-operation documentation contract (design doc Sections F, G, H, I; this plan Sections 4 and 5) | Technical Lead reviews and approves the final diff. **In progress; not approved.** | 6 |
| 7 | Implement `create_return_request` and `create_support_ticket` in `repository.py` (**not started**) | Contracts from design doc Sections F–H and this plan's Section 4 met exactly: signatures, result dictionaries, validation (`TypeError` for wrong type, `ValueError` for invalid value), check order, `BEGIN IMMEDIATE`, `RET-`/`TKT-` IDs, `uq_active_return_per_item` `IntegrityError` translated to `duplicate`, generic `failed` for other `sqlite3.Error`, non-database exceptions re-raised, `created` only after `COMMIT`. No schema, seed, or catalog changes. | 4, 6, 7A |
| 8 | Write `test_returns.py` and `test_tickets.py` (design doc tests 5, 7, 7b, 8, 8b, 9, 10; 5b is already covered by Checkpoint 3) (**not yet approved**) | All pass. Returns tests: every outcome; 48-hour and 168-hour inclusive boundaries (exactly, and one second past); `damaged`, `wrong_product`, `standard`; NULL and future `delivered_at`; duplicate checked before eligibility; duplicate via the unique index when the pre-check is bypassed; wrong-type `TypeError` and invalid-value `ValueError` cases, including naive `datetime` and offset-less string for `reference_time`; explicit `reference_time` in every window test, `requested_at` equal to the normalized reference time; failure injection proving `failed` hides database text and programming errors re-raise with rollback. Tickets tests: owned order linked; unowned and nonexistent orders give identical results with NULL `order_id`; general ticket; `conversation_summary` None, valid, blank (`ValueError`), non-string (`TypeError`); blank and non-string `order_id`; `TKT-` ID format; failure handling. | 7 |
| 9 | Full-suite run and review (**not yet approved**) | `pytest` across all test files passes with no skips; Technical Lead reviews actual output, not a claim that tests "would pass." | 3, 6, 8 |

---

## 8. Risks and Unresolved Assumptions

1. ~~Checkpoint 2's "manual attempt" step is manual until Checkpoint 3~~ — **Resolved:** Checkpoint 3 (`tests/test_schema.py`) is complete and automates this proof.
2. ~~Checkpoint 2's composite FK proof needed data that did not yet exist~~ — **Resolved:** Checkpoint 4 seed data and the Checkpoint 3 tests are complete.
3. **Result-object shape (`OrderRecord`, `ReturnRequestResult`, etc.) is named but not specified field-by-field** in this document — Section 4 states what information each must carry in prose, but the exact Python type (dataclass, `TypedDict`, plain dict) is left to implementation, since choosing it doesn't affect any approved architecture decision and over-specifying it here would be exactly the unnecessary detail this planning phase should avoid.
4. ~~No decision yet on how `product_id` validation against `Products.json` is performed during seeding~~ — **Resolved per Technical Lead review: this is now a preserved implementation rule, not an open question.** `seed_data.py` must validate every `product_id` it uses against the IDs actually present in `Products.json` (read-only, per design doc Section C — the catalog itself is never modified), and **must fail loudly** — raise an error and stop — if a seed row references a `product_id` that doesn't exist in `Products.json`, rather than silently skipping that row or substituting a default. A dangling product reference in seed data is a bug to catch immediately, not an edge case to tolerate silently. This rule is now also reflected in Section 3, step 4.
5. **The non-returnable product gap (design doc Section L, item 1) is a documented MVP limitation.** `Products.json` has no returnability metadata. The write contract adds no `non_returnable` outcome, schema field, or catalog change, and no test covers category-based non-returnable enforcement. A separate approved design decision is required to change this.
6. **Test execution environment (plain `pytest` vs. Docker) is not decided in this plan.** Docker setup is listed as later-phase work in `PROJECT_STATUS.md`; this plan assumes tests are runnable locally against a local SQLite file first, with containerization applied afterward — flagged as an assumption, not a decision made here.
7. **No teacher-provided schema or code has been supplied for comparison at this phase either** — same standing note carried from Phases 0 and 1.

---

**Status of the stop:** Checkpoints 1–6 are complete. Checkpoint 7A is documentation-only and awaits Technical Lead approval. Do not begin Checkpoint 7 (or anything after it) until explicitly authorized.

---

## Revision Report — Round 1

**Sections changed:** Section 3 (Implementation Sequence), Section 4 (`get_order` contract note only), Section 6 (Testing Strategy — intro paragraph and rows 4 and 6), Section 8 (risk item 4), folder structure block in Section 2 (`test_authorization.py` comment), and the document header status line. All other sections (0, 1, the rest of 2, 5, 7, and risk items 1–3/5–7 in Section 8) are unchanged.

1. **Sequencing fix (Section 3):** rewritten so the nine listed steps now exactly match Section 7's checkpoint order: connection → schema → schema tests → seed data → read-only repositories → authorization tests → write repositories → return/ticket tests → full-suite verification. The prior draft listed schema creation before connection setup and claimed connection setup "must exist before step 1 is tested" while still numbering it step 2 — an internal contradiction, now resolved by renumbering connection setup as step 1. Section 3 now explicitly cross-references Section 7 instead of maintaining a separately-numbered, potentially-divergent sequence.
2. **Test 2b scope clarified (Sections 4 and 6):** both the `get_order` contract note and the Section 6 test-mapping row now state plainly that only repository-level equivalence (`None` returned in both cases, no distinguishing exception or shape) is verified at this phase, and that user-facing application-response equivalence is explicitly deferred to the later orchestration phase. No sentence in the document now claims or implies that application-level equivalence has already been tested.
3. **Preserved rules made explicit, not newly introduced:**
   - Product ID validation against `Products.json`, failing loudly on an unknown ID, is moved from a "flagged for confirmation" open risk (Section 8, former item 4) to a stated, settled implementation rule, and is now also referenced in Section 3, step 4.
   - The requirement that return/ticket authorization tests create records for multiple customers and verify customer-scoped repository results is now stated as an explicit rule at the top of Section 6, and both the `test_authorization.py` folder-structure comment (Section 2) and the Section 6 table rows for tests 4 and 6 describe the multi-customer setup directly, rather than leaving it implied by the words "cross-customer."

**Unchanged, as instructed:** the five-table schema, all approved enums and database constraints, `Products.json` (read, not modified), the architecture layers (database/connection/repository/test — no new layer introduced), and every other approved design decision from Phase 1 and from the first version of this plan. No Python code, migration, seed script, or test file was written in this task. Phase 2 implementation has not begun.

**Stopping here for Technical Lead approval.**
