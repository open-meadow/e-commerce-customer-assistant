# Database Design — AI E-Commerce Customer Support Assistant

**Phase:** 1 — Database Design
**Status:** Phase 1 design approved. Checkpoint 2 (`app/db/schema.py`) implements Section C and was approved on 2026-11-06. Checkpoint 7A (the write-function contracts in Sections F, G, and H) is approved and was committed and pushed in `8ce2d43`. Those functions are not implemented; Checkpoint 7 implementation has not started and is not authorized.
**Scope of this document:** design only. No application code, migrations, seed scripts, UI, RAG, LangGraph, or voice work is included, per the Phase 1 task boundary. Checkpoint 7A changes the write contracts only; it does not change Section C.

---

## 0. Documents and Files Inspected

- `PROJECT_STATUS.md` — confirms Phase 0 approved, single-`orders`-table model approved, voice deferred, simulated auth.
- `docs/00_project_discovery.md` — source of the approved entity list and Section 6a schema sketch.
- `data/knowledge_base/FAQ.md` — source of the seven canonical order statuses (Q1.4) and return rules (Q3.1–Q3.4).
- `data/knowledge_base/Policies.md` — source of the 7-day return window, non-returnable categories, and cancellation policy.
- `data/Products.json` — product catalog (5 items: Wireless Earbuds, Over-Ear Headphones, 65W GaN Charger, 24-inch Monitor, Power Bank; categories: Audio, Accessories, Computers).
- `data/customer_queries_sample.csv` — referenced for query shape, not for schema.

**Not available to inspect:** no teacher-provided code, migrations, ER diagrams, or architecture files exist in the repository. None are assumed. If such materials exist outside this conversation, they should be provided before this design is finalized, per the standing instruction not to silently replace teacher patterns.

**Scope preserved:** this design supports the approved MVP capabilities only (order lookup, delivery/payment/refund questions, returns, product info, FAQ/policy RAG, ticketing, data isolation). No cart, checkout, browsing, or marketplace functionality is introduced.

---

## A. Requirements

### Functional database requirements
- FR-DB1: Store customers, orders, order line items, return requests, and support tickets.
- FR-DB2: Support lookup of an order's status, payment status, refund status, and refund amount by order ID.
- FR-DB3: Support listing all orders for a given customer.
- FR-DB4: Support creating a return request tied to a specific order and item.
- FR-DB5: Support creating a support ticket, optionally tied to an order.
- FR-DB6: Support determining return eligibility from `orders.delivered_at` and the approved `reason_category` window (`Policies.md` §2, `FAQ.md` Q3.4). Category-based non-returnable enforcement is an MVP limitation, not part of the current write contract (Sections F and L).
- FR-DB7: Prevent duplicate return requests for the same order item.

### Data ownership and security requirements
- SEC-DB1: Every query that returns customer-specific data (orders, order items, return requests, tickets) must filter by `customer_id` supplied from trusted application context — never from user text or LLM output.
- SEC-DB2: A request for a record that exists but belongs to another customer must return the same "not found" result as a request for a record that does not exist at all. The system must not reveal existence or ownership of another customer's data.
- SEC-DB3: No component exposes arbitrary/raw SQL to the LLM. All access goes through parameterized repository functions with a fixed, reviewed set of query shapes.
- SEC-DB4: Logs may record order IDs, status values, and outcomes, but must not record full customer contact details or any payment-like identifiers unnecessarily.

### MVP assumptions and limitations
- Authentication is simulated via a Streamlit-session customer selector, not a real login system. The database layer trusts whatever `customer_id` the application layer passes in; it does not itself verify a password or token, because none exists in this MVP. This is a stated limitation, not a hidden gap.
- Products remain in `Products.json`, not a database table (rationale in Section I... see Section C note under `order_items`).
- No payment gateway, courier, or email/SMS integration exists; all such effects (e.g., "refund processed") are represented purely as status fields, not external calls.
- The dataset's non-returnable examples ("hygiene/personal care items," "software licenses") do not correspond to any category currently in `Products.json` (Audio, Accessories, Computers). `Products.json` has no product-level returnability field. Checkpoint 7A does not add a `non_returnable` outcome, a schema column, or a catalog change. Category-based non-returnable enforcement remains an MVP limitation until a separate design decision approves it (Section L). The supplied `Products.json` stays unmodified.

---

## B. Entity Relationship Design

### Entities

- **customers** — one row per support-eligible customer. Root of all ownership checks.
- **orders** — one row per order; carries status, payment, and refund state directly (approved single-table simplification — no separate payment or shipment tables).
- **order_items** — one row per line item in an order; references the product by ID (product catalog itself stays in `Products.json`, not duplicated into the database).
- **return_requests** — one row per return attempt; references the customer, the order, and the specific item being returned, so a single order with multiple items can have independent return requests.
- **support_tickets** — one row per escalation; always tied to a customer, optionally tied to an order.

### Relationships and cardinalities

| Relationship | Cardinality | Why |
|---|---|---|
| customers → orders | 1-to-many | A customer places multiple orders over time. |
| orders → order_items | 1-to-many | An order can contain multiple products (seed plan requires this explicitly). |
| customers → return_requests | 1-to-many | Direct FK kept (see note below) even though it's reachable via orders. |
| orders → return_requests | 1-to-many | One order can have more than one return request, one per returned item. |
| order_items → return_requests | 1-to-1 (per request) | Each return request targets exactly one item; an item should not have more than one *active* return request (enforced via constraint, Section H). |
| customers → support_tickets | 1-to-many | A customer can raise multiple issues over time. |
| orders → support_tickets | 1-to-many, optional | A ticket may or may not relate to a specific order. |

**Why `return_requests.customer_id` is kept as a direct foreign key, not only derived through `order_id`:** it lets an ownership check on a return request be a single indexed filter (`WHERE customer_id = :auth_id`) instead of always requiring a join through `orders`. Per this revision, that consistency is no longer just an application-level check — it is additionally enforced by the database itself through composite foreign keys (see below).

**Revision — composite foreign key strategy (Technical Lead request):**

The original design verified that a return request's `order_id` belongs to its `customer_id`, and that its `item_id` belongs to its `order_id`, entirely in application code before the insert. This revision strengthens that guarantee at the schema level using composite uniqueness plus composite foreign keys, so the database itself rejects an inconsistent row even if application code has a bug:

1. **`UNIQUE(order_id, customer_id)` on `orders`.** Since `order_id` is already the primary key, this composite unique constraint is logically implied (a PK is already unique on its own), but declaring it explicitly creates the exact two-column target that a composite foreign key from `return_requests` needs to reference. This is the mechanism, not redundant decoration.
2. **`UNIQUE(item_id, order_id)` on `order_items`.** Same reasoning: `item_id` is already the PK, but this composite unique gives `return_requests` a two-column target proving a specific item belongs to a specific order.
3. **`return_requests` gets two composite foreign keys:**
   - `FOREIGN KEY (order_id, customer_id) REFERENCES orders(order_id, customer_id)` — the database rejects any insert where the stated `order_id` and `customer_id` don't actually belong together, which is a stronger guarantee than two independent single-column FKs (which only prove each ID individually exists somewhere, not that they're linked to each other).
   - `FOREIGN KEY (item_id, order_id) REFERENCES order_items(item_id, order_id)` — the database rejects any insert where the stated `item_id` doesn't actually belong to the stated `order_id`.
4. **`support_tickets` gets the equivalent protection when `order_id` is supplied:** `FOREIGN KEY (order_id, customer_id) REFERENCES orders(order_id, customer_id)`, but only enforceable when `order_id` is not NULL (SQLite foreign keys are not checked when any referencing column is NULL, which works in our favor here — see Section G for how this interacts with the "clear don't reject" ticket behavior).

**Why application-level ownership validation is still required even with these constraints:** the composite FKs prove internal *consistency* (that the IDs genuinely relate to each other as claimed) — they do not, and cannot, prove *authorization* (that the row being inserted belongs to the customer making the request right now). A composite FK will happily accept `(order_id='ORD1003', customer_id='C102')` because that pairing is genuinely true in the `orders` table — but if the authenticated session is C101, that insert would still be wrong to allow, and only an application-level check comparing the *session's* `authenticated_customer_id` against the *requested* `customer_id`/`order_id` catches that. In other words: the database enforces "these IDs are consistent with each other," while the application enforces "these IDs are consistent with who is actually logged in." Both layers are necessary; neither alone is sufficient. This mirrors the standard defense-in-depth principle of never trusting a single layer for an authorization decision.

### Mermaid ER Diagram

Mermaid's `erDiagram` syntax does not have native notation for composite foreign keys, so the diagram below shows the entity-level relationships as before; the composite FK mechanics (which exact column pairs enforce consistency) are specified in prose above and in the table definitions in Section C.

```mermaid
erDiagram
    CUSTOMERS ||--o{ ORDERS : places
    ORDERS ||--|{ ORDER_ITEMS : contains
    CUSTOMERS ||--o{ RETURN_REQUESTS : files
    ORDERS ||--o{ RETURN_REQUESTS : "is subject of"
    ORDER_ITEMS ||--o| RETURN_REQUESTS : "is returned via"
    CUSTOMERS ||--o{ SUPPORT_TICKETS : raises
    ORDERS ||--o{ SUPPORT_TICKETS : "relates to (optional)"

    CUSTOMERS {
        string customer_id PK
        string name
        string email
        string phone
    }
    ORDERS {
        string order_id PK
        string customer_id FK
        string status
        string payment_status
        string refund_status
        decimal refund_amount
        datetime order_date
        datetime delivered_at
        decimal total_amount
    }
    ORDER_ITEMS {
        string item_id PK
        string order_id FK
        string product_id
        int quantity
        decimal price
    }
    RETURN_REQUESTS {
        string return_id PK
        string customer_id FK
        string order_id FK
        string item_id FK
        string reason
        string status
        datetime requested_at
    }
    SUPPORT_TICKETS {
        string ticket_id PK
        string customer_id FK
        string order_id FK
        string issue_type
        string description
        string conversation_summary
        string status
        string priority
        datetime created_at
    }
```

---

## C. Table Definitions

### `customers`

| Column | Type | Null | Key | Default | Unique | Check |
|---|---|---|---|---|---|---|
| `customer_id` | TEXT | NOT NULL | PK | — | — | — |
| `name` | TEXT | NOT NULL | — | — | — | — |
| `email` | TEXT | NOT NULL | — | — | **UNIQUE** | — |
| `phone` | TEXT | NULL | — | — | — | — |

**Indexes:** PK index on `customer_id` (automatic). Unique index on `email` — supports a future real-login lookup path without schema change, and prevents accidental duplicate seed customers.

### `orders`

**Storage conventions applied to this table (Technical Lead revision — see Section I for full rationale):** monetary columns (`refund_amount`, `total_amount`) are stored as **INTEGER paise** (1 rupee = 100 paise), not DECIMAL, to avoid floating-point rounding issues; timestamp columns (`order_date`, `delivered_at`) are stored as **TEXT in UTC ISO-8601 format** (`YYYY-MM-DDTHH:MM:SSZ`), SQLite's recommended approach since it has no native datetime type, and sorts correctly as plain text.

| Column | Type | Null | Key | Default | Unique | Check |
|---|---|---|---|---|---|---|
| `order_id` | TEXT | NOT NULL | PK | — | **UNIQUE(order_id, customer_id)** | — |
| `customer_id` | TEXT | NOT NULL | FK → customers.customer_id | — | (part of composite unique above) | — |
| `status` | TEXT | NOT NULL | — | `'Processing'` | — | must be one of the 7 FAQ.md values (Section D) |
| `payment_status` | TEXT | NOT NULL | — | `'Pending'` | — | must be one of `Pending`, `Paid`, `Failed` (Section D — revised) |
| `refund_status` | TEXT | NOT NULL | — | `'Not Applicable'` | — | must be one of `Not Applicable`, `Pending`, `Processed`, `Failed` (Section D — revised) |
| `refund_amount` | INTEGER (paise) | NULL | — | `NULL` | — | if not null, must be ≥ 0; see refund consistency rule below |
| `order_date` | TEXT (ISO-8601 UTC) | NOT NULL | — | — | — | — |
| `delivered_at` | TEXT (ISO-8601 UTC) | NULL | — | `NULL` | — | if `status NOT IN ('Delivered','Returned')`, should be NULL (app-level check, see note) |
| `total_amount` | INTEGER (paise) | NOT NULL | — | — | — | must be > 0 |

**Indexes:** PK on `order_id`. Non-unique index on `customer_id` — this is the single most-used filter in the entire system (every order lookup, every ownership check), so it must be indexed for both correctness-pattern clarity and query performance. The `UNIQUE(order_id, customer_id)` composite constraint doubles as the reference target for `return_requests`' and `support_tickets`' composite foreign keys (see Section B revision).

**Refund consistency rule (Technical Lead request, Section D):** `refund_amount` must be NULL whenever `refund_status = 'Not Applicable'`, and must be a non-null value ≥ 0 whenever `refund_status` is `Pending`, `Processed`, or `Failed`. This cross-column rule is documented here as a design requirement; like the `delivered_at` rule below, it is expressible as a SQLite `CHECK` constraint and will be finalized in the Phase 2 migration, not fully spelled out in SQL in this design-only document.

**Note on `delivered_at`:** SQLite's `CHECK` constraints can reference only the row's own columns, so a cross-column rule like "`delivered_at` must be NULL unless status is Delivered or Returned" is expressible as a `CHECK`, and will be included in the migration (Phase 2), but is noted here as a design decision rather than fully spelled out in SQL in this document, per the "design only" boundary.

### `order_items`

| Column | Type | Null | Key | Default | Unique | Check |
|---|---|---|---|---|---|---|
| `item_id` | TEXT | NOT NULL | PK | — | **UNIQUE(item_id, order_id)** | — |
| `order_id` | TEXT | NOT NULL | FK → orders.order_id | — | (part of composite unique above) | — |
| `product_id` | TEXT | NOT NULL | — | — | — | must match an ID present in `Products.json` (app-level check — see below) |
| `quantity` | INTEGER | NOT NULL | — | `1` | — | must be > 0 |
| `price` | INTEGER (paise) | NOT NULL | — | — | — | must be > 0 |

**Indexes:** PK on `item_id`. Index on `order_id` — needed because order detail and return-eligibility checks always fetch all items for one order. The `UNIQUE(item_id, order_id)` composite is the reference target for `return_requests`' second composite foreign key (Section B revision), proving a given item genuinely belongs to a given order.

**Design note — why `product_id` is not a database foreign key:** the product catalog is intentionally kept in `Products.json`, not duplicated into a `products` table, because the MVP has no requirement that demonstrates a need for database-side product management (no admin writes, no inventory mutation). Validating `product_id` against `Products.json` happens at the application/repository layer when an order is seeded or an item is created, not via a SQL foreign key, since SQLite cannot enforce a foreign key into a JSON file. This is a deliberate scope boundary, matching the instruction not to add unnecessary complexity. If a future requirement needed transactional stock updates or admin product edits, moving the catalog into a real `products` table would be the natural next step — flagged, not built.

### `return_requests`

| Column | Type | Null | Key | Default | Unique | Check |
|---|---|---|---|---|---|---|
| `return_id` | TEXT | NOT NULL | PK | — | — | — |
| `customer_id` | TEXT | NOT NULL | **Composite FK** → orders(order_id, customer_id); also plain FK → customers.customer_id | — | — | — |
| `order_id` | TEXT | NOT NULL | (part of both composite FKs below) | — | — | — |
| `item_id` | TEXT | NOT NULL | **Composite FK** → order_items(item_id, order_id) | — | — | — |
| `reason` | TEXT | NOT NULL | — | — | — | — |
| `status` | TEXT | NOT NULL | — | `'Requested'` | — | must be one of the proposed values (Section D) |
| `requested_at` | TEXT (ISO-8601 UTC) | NOT NULL | — | current UTC timestamp | — | — |

**Foreign keys, stated explicitly (revision):**
- `FOREIGN KEY (order_id, customer_id) REFERENCES orders(order_id, customer_id)` — proves this order genuinely belongs to this customer.
- `FOREIGN KEY (item_id, order_id) REFERENCES order_items(item_id, order_id)` — proves this item genuinely belongs to this order.

**Indexes:** PK on `return_id`. Index on `customer_id` (ownership filter). Index on `order_id` (duplicate-check and order-detail display). A **unique partial index** is recommended: at most one *active* (`status IN ('Requested','Approved')`) return request per `item_id` — this is the mechanism that prevents duplicate submissions (see Section H). This is distinct from, and in addition to, the composite foreign keys above — the composite FKs prove relational consistency, the partial unique index prevents duplicates.

### `support_tickets`

| Column | Type | Null | Key | Default | Unique | Check |
|---|---|---|---|---|---|---|
| `ticket_id` | TEXT | NOT NULL | PK | — | — | — |
| `customer_id` | TEXT | NOT NULL | FK → customers.customer_id; also part of composite FK below | — | — | — |
| `order_id` | TEXT | NULL | **Composite FK** → orders(order_id, customer_id), enforced only when not NULL | `NULL` | — | — |
| `issue_type` | TEXT | NOT NULL | — | — | — | must be a known category (free-text in MVP; not enumerated, since issue types are open-ended) |
| `description` | TEXT | NOT NULL | — | — | — | — |
| `conversation_summary` | TEXT | NULL | — | `NULL` | — | — |
| `status` | TEXT | NOT NULL | — | `'Open'` | — | must be one of the proposed values (Section D) |
| `priority` | TEXT | NOT NULL | — | `'Normal'` | — | must be one of the proposed values (Section D) |
| `created_at` | TEXT (ISO-8601 UTC) | NOT NULL | — | current UTC timestamp | — | — |

**Foreign key, stated explicitly (revision):** `FOREIGN KEY (order_id, customer_id) REFERENCES orders(order_id, customer_id)`. SQLite does not evaluate a foreign key when any of its referencing columns is NULL, so a general ticket (`order_id = NULL`) is unaffected by this constraint — only a ticket that *does* supply an `order_id` is checked for order/customer consistency. This is exactly the behavior Section G's "clear, don't reject" rule needs (see below).

**Indexes:** PK on `ticket_id`. Index on `customer_id` (ownership filter). Index on `order_id` (for "show tickets related to this order" lookups, where not null).

---

## D. Status and Enum Definitions

### Dataset-defined (must be preserved exactly — from `FAQ.md` Q1.4)

`orders.status`:
`Processing`, `Packed`, `Shipped`, `Out for Delivery`, `Delivered`, `Cancelled`, `Returned`

### Proposed implementation enums (not in the dataset — engineering proposal, pending approval)

**Revision (Technical Lead request):** refund states were previously mixed into `payment_status` (`Refund Pending`, `Refunded`), which conflated two distinct concepts — whether the original payment succeeded, and what's happening with a refund on top of it. These are now kept as two independent enums, corrected as follows:

`orders.payment_status` — describes the original payment only, never refund state:
- `Pending`
- `Paid`
- `Failed`

`orders.refund_status` — describes refund activity, independent of the original payment:
- `Not Applicable` (default — no refund involved)
- `Pending`
- `Processed`
- `Failed`

**Consistency rule between `refund_status` and `refund_amount`** (stated here and in Section C's `orders` table): `refund_amount` must be NULL exactly when `refund_status = 'Not Applicable'`, and must be a non-null integer-paise value ≥ 0 for any other `refund_status` value (`Pending`, `Processed`, `Failed`). A `Failed` refund still records the amount that was attempted, which is useful information for a support agent reviewing the ticket — it is not cleared back to NULL.

`return_requests.status`:
- `Requested`
- `Approved`
- `Rejected`
- `Completed`

`support_tickets.status`:
- `Open`
- `In Progress`
- `Resolved`
- `Closed`

`support_tickets.priority`:
- `Low`
- `Normal`
- `High`
- `Urgent`

These four enum sets are **engineering proposals**, clearly distinguished from the dataset-sourced `orders.status` list above. They need Technical Lead sign-off since no source document defines them.

---

## E. Customer Data Isolation

**Core rule:** every repository function that touches `orders`, `order_items`, `return_requests`, or `support_tickets` takes an `authenticated_customer_id` parameter supplied by the application layer (from Streamlit session state in this MVP), and includes it as a `WHERE` filter in the query itself — never as a post-fetch check on an already-retrieved row.

**Concrete scenario (C101 requests ORD1003, which belongs to C102):**

```sql
SELECT order_id, status, payment_status, refund_status, refund_amount, delivered_at
FROM orders
WHERE customer_id = :authenticated_customer_id   -- 'C101', from trusted session context
  AND order_id = :requested_order_id;              -- 'ORD1003'
```

This returns **zero rows**. The repository layer treats zero rows as "not found" — identical to the response for an order ID that doesn't exist at all. The application layer must not branch on "exists but not yours" versus "doesn't exist"; both paths produce the same customer-facing message (e.g., "I couldn't find that order on your account"). This is what prevents the system from confirming that ORD1003 exists or revealing who owns it.

**Applied the same way to every table:**

| Table | Ownership filter |
|---|---|
| `orders` | `WHERE customer_id = :auth_id AND order_id = :requested_id` |
| `order_items` | `WHERE order_id IN (SELECT order_id FROM orders WHERE customer_id = :auth_id) AND order_id = :requested_order_id` — i.e., ownership is enforced by first proving the order belongs to the customer, never by trusting a bare `item_id` lookup |
| `return_requests` | `WHERE customer_id = :auth_id AND return_id = :requested_id` (direct FK, per Section B) |
| `support_tickets` | `WHERE customer_id = :auth_id AND ticket_id = :requested_id` |

**Trust boundary, stated plainly:** `authenticated_customer_id` originates only from the trusted application/session layer (the Streamlit "log in as" selector in this MVP). It is never accepted as a parameter from the LLM's tool-call arguments, never parsed from the user's free-text message, and never taken from conversation history. If the LLM is prompt-injected into trying to request another customer's ID, the backend ignores whatever it says and uses the session's own value regardless — so the ownership filter is enforced even under adversarial LLM behavior, which is the property Section 9 of the discovery report commits to.

---

## F. Return Request Integrity

A return request must satisfy three joined conditions before it is created. Following the Section B revision, conditions 1 and 2 are now enforced at **two layers**, not one:

1. **`order_id` belongs to `customer_id`** — checked at the application layer via the same ownership query pattern as Section E (`SELECT 1 FROM orders WHERE customer_id = :auth_id AND order_id = :order_id`), rejected before any further check runs if this returns no row; **and** enforced again at the database layer by the `FOREIGN KEY (order_id, customer_id) REFERENCES orders(order_id, customer_id)` composite constraint on `return_requests`, which would reject the insert outright even if the application check were somehow bypassed.
2. **`item_id` belongs to `order_id`** — checked at the application layer via `SELECT 1 FROM order_items WHERE order_id = :order_id AND item_id = :item_id`, preventing a return request referencing an item from a *different* order even one the same customer owns; **and** enforced again by the `FOREIGN KEY (item_id, order_id) REFERENCES order_items(item_id, order_id)` composite constraint.
   - As Section B explains, these composite FKs prove the IDs are *internally consistent with each other* — they cannot by themselves prove the request came from the *right customer*, which is why the application-level check against the session's `authenticated_customer_id` remains mandatory and cannot be replaced by the constraints alone.
3. **Eligibility**, from the approved policy windows. No additional return rule is added. The 48-hour damaged and wrong-product window is the approved business policy from `FAQ.md` Q3.4, including where it is shorter than the 7-day window and therefore stricter than a general return.
   - The repository does not read or interpret the free-text `reason`. The caller supplies `reason_category`, a controlled value, and the repository validates it.
   - Allowed `reason_category` values and windows, measured from `orders.delivered_at` to the normalized UTC reference time:
     - `damaged`: 48 hours.
     - `wrong_product`: 48 hours.
     - `standard`: 7 × 24 hours (`Policies.md` §2, `FAQ.md` Q3.1).
   - A boundary is inclusive. Elapsed time of exactly 48 hours, or exactly 168 hours for `standard`, is eligible. Elapsed time is negative when `delivered_at` is later than the reference time; that request is ineligible. A NULL `delivered_at` is ineligible. All three ineligible cases use the `outside_window` result. There is no separate outcome for them.
   - Category-based non-returnable enforcement is not part of this contract. `Policies.md` §2 names categories that are non-returnable, but `Products.json` has no returnability metadata. This checkpoint does not invent a `non_returnable` result or change the schema or catalog (Section L).

**Approved repository contract (Checkpoint 7A).** Not yet implemented. Schema columns and constraints in Section C are unchanged. `reason_category` is not stored; the free-text `reason` is stored in the existing `reason` column. `requested_at` is the normalized reference time, written by this function as `YYYY-MM-DDTHH:MM:SSZ`, so the stored timestamp and the eligibility comparison use the same instant. The column default (`strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`) remains in the schema for other writers.

```text
create_return_request(authenticated_customer_id, order_id, item_id, reason, *, reason_category, reference_time=None)
```

`reason_category` is required and keyword-only. `reference_time` is optional and keyword-only.

Check order, inside one `BEGIN IMMEDIATE` transaction after input validation:

1. Validate inputs. This step runs before the transaction opens.
2. Verify order ownership. A nonexistent order and an order owned by another customer both produce `not_found`. The function stops before the item check.
3. Verify that the item belongs to the specified order. Failure produces `item_mismatch`.
4. Check for an existing active return (`status` of `Requested` or `Approved`) for the same `item_id`. This is the same predicate as `uq_active_return_per_item`. An active row produces `duplicate`, including when the new request would also be outside the eligibility window.
5. Check return eligibility, as defined above.
6. Insert one row with `status = 'Requested'`, the generated `return_id`, the original `reason`, and `requested_at` set from the normalized reference time.

The return ID is `RET-` plus the first 12 hexadecimal characters of a UUID4, uppercased (`uuid.uuid4().hex[:12].upper()`). `outcome` is `created` only after `COMMIT` succeeds.

**Reference time.** `reference_time` follows the timezone rules used by `seed_database` (timezone-aware input, normalized to UTC), but its default differs:

- `None`: the function reads the current UTC time exactly once, with `datetime.now(timezone.utc)`. This is the production behavior. (`seed_database(None)` is unaffected and continues to use its fixed deterministic baseline `2026-11-07T00:00:00Z`.)
- A `datetime` must be timezone-aware and is converted to UTC. A naive `datetime` raises `ValueError`.
- A string must be an ISO-8601 timestamp with an explicit UTC offset (`Z` or a numeric offset) and is normalized to UTC. An unparseable string or a string without an offset raises `ValueError`.
- Any other type raises `TypeError`.
- The normalized value uses whole-second precision, meaning truncation: microseconds are dropped (`replace(microsecond=0)`), never rounded. For example, `10:00:00.999999` becomes `10:00:00`, not `10:00:01`. Whole-second precision is acceptable for this contract.
- The same normalized instant is used for the eligibility comparison and for `requested_at`. The clock is read at most once per operation, and never again after that. Tests inject `reference_time` to stay deterministic.

**Input validation (type versus value).** A wrong type raises `TypeError`. A right type with an invalid value, including a blank or whitespace-only string, raises `ValueError`.

- `authenticated_customer_id`, `order_id`, `item_id`, `reason`: must be `str` (otherwise `TypeError`) with a non-empty `strip()` (otherwise `ValueError`). The stored `reason` is the original string.
- `reason_category`: must be `str` (otherwise `TypeError`) and exactly `damaged`, `wrong_product`, or `standard` (otherwise `ValueError`).

Exact result dictionaries:

```text
{"outcome": "created", "return_id": "<generated-id>"}
{"outcome": "not_found", "return_id": None}
{"outcome": "item_mismatch", "return_id": None}
{"outcome": "outside_window", "return_id": None}
{"outcome": "duplicate", "return_id": None}
{"outcome": "failed", "return_id": None}
```

`failed` is the generic database-failure result. It carries no SQLite message, SQL text, or exception type. A business rejection rolls the transaction back and returns its dictionary. No partial row remains. Transaction and exception rules are in Section H.

---

## G. Support Ticket Integrity

- `support_tickets.customer_id` is always required and always sourced from trusted session context, identically to orders. The repository parameter is `authenticated_customer_id`. It is never taken from the user message or from model text.
- `support_tickets.order_id` is optional (a ticket can be general, e.g., "how do I contact support").
- **When `order_id` is a non-empty string**, the same ownership check from Section E runs first at the application layer: `SELECT 1 FROM orders WHERE customer_id = :auth_id AND order_id = :order_id`. If this returns no row, the ticket is still created, but **without** the order reference (`order_id` stored as `NULL`). A nonexistent order and an unowned order produce the same stored row and the same result dictionary. The function does not attach another customer's order ID.

**Approved repository contract (Checkpoint 7A).** Not yet implemented. Section C is unchanged.

```text
create_support_ticket(authenticated_customer_id, issue_type, description, order_id=None, conversation_summary=None)
```

- `order_id=None` is a general ticket. No ownership query runs.
- Validation rule: a wrong type raises `TypeError`; an invalid value, including a blank or whitespace-only string, raises `ValueError`. Blank strings are never silently converted to `None`.
- `order_id`: `None` is a general ticket. A non-empty string is checked for ownership. A blank or whitespace-only string raises `ValueError`. A non-string, non-`None` value raises `TypeError`.
- `authenticated_customer_id`, `issue_type`, and `description` must be `str` (otherwise `TypeError`) with a non-empty `strip()` (otherwise `ValueError`).
- `conversation_summary`: `None` is stored as SQL NULL. A non-empty string is accepted and stored. An empty or whitespace-only string raises `ValueError`. A non-string, non-`None` value raises `TypeError`.
- An order owned by the authenticated customer is stored and linked. Any other lookup result is stored as NULL and is not linked.
- The INSERT uses parameterized SQL inside `BEGIN IMMEDIATE`. It does not override the schema defaults: `status = 'Open'`, `priority = 'Normal'`, and `created_at` from the column default. The column default is `strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` (`app/db/schema.py`), which yields UTC text in the approved `YYYY-MM-DDTHH:MM:SSZ` format and reads SQLite's own clock at insert time. Ticket creation does not take a `reference_time` parameter.
- The ticket ID is `TKT-` plus the first 12 hexadecimal characters of a UUID4, uppercased (`uuid.uuid4().hex[:12].upper()`).
- `outcome` is `created` only after `COMMIT` succeeds. `order_linked` is true only when the stored `order_id` is the owned order ID.

Exact success result:

```text
{"outcome": "created", "ticket_id": "<generated-id>", "order_id": "<owned-order-id-or-None>", "order_linked": true-or-false}
```

Generic database failure, with no SQLite message, SQL text, or exception type:

```text
{"outcome": "failed", "ticket_id": None, "order_id": None, "order_linked": False}
```

There is no rejection outcome for a missing or unowned order. The customer-facing layer must not say that the order belongs to someone else. Transaction and exception rules are in Section H.
- **Why the application check runs before the insert, not after:** the new composite FK on `support_tickets` (Section C revision: `FOREIGN KEY (order_id, customer_id) REFERENCES orders(order_id, customer_id)`) would in fact also reject an insert that tried to pair C101's `customer_id` with C102's `order_id` directly — but relying on that alone would surface a raw database constraint failure to the customer, which is poor UX and risks leaking implementation detail in an error message. The application layer checks first and silently clears `order_id` to NULL, so ticket creation always succeeds from the customer's point of view; the composite FK exists as a safety net in case application code has a bug, not as the primary mechanism (same division of responsibility as Section B).
- **How this is communicated safely:** the customer is never told "that order doesn't belong to you" (which would confirm the order ID exists under someone else's account). Instead, the assistant's response treats this identically to the case where the order ID doesn't exist at all — something like "I've logged your ticket; I wasn't able to link it to a specific order, so a support agent will follow up for the order details." This keeps ticket creation from becoming a side channel for confirming another customer's order ID exists, consistent with Section E's "same not-found result" principle.

---

## H. Transactions and Duplicate Requests

**Return request creation** and **support ticket creation** are each a single-row insert preceded by the checks in Sections F and G. Each write opens its connection through `get_connection()`, runs `BEGIN IMMEDIATE`, and uses parameterized SQL. Input validation finishes before `BEGIN IMMEDIATE`. A validation error (`TypeError` for a wrong type, `ValueError` for an invalid value) therefore opens no transaction.

- If a business check fails, the transaction rolls back and the function returns that check's result dictionary. No partial row remains.
- The insert runs only after those checks pass. `created` is returned only after `COMMIT` succeeds.

**Preventing duplicate return submissions:** rather than relying only on application-layer "check then insert" logic (which has a race-condition gap, even if unlikely in a single-user Streamlit demo), the design recommends a **unique partial index**:

```
CREATE UNIQUE INDEX uq_active_return_per_item
ON return_requests(item_id)
WHERE status IN ('Requested', 'Approved');
```

This index is the database guarantee against a duplicate active return. The repository still runs the duplicate pre-check inside the same `BEGIN IMMEDIATE` transaction. If the insert then raises a `sqlite3.IntegrityError` caused by `uq_active_return_per_item`, the function rolls back and translates it to the same `duplicate` dictionary. It may inspect the SQLite error internally to recognize that index. The caller receives no raw SQLite text. Any other `sqlite3.Error`, including an `IntegrityError` from a different constraint, uses the generic `failed` result below, not `duplicate`.

**Failure handling:**

- On `sqlite3.Error`, roll back and return the documented `failed` dictionary for that function. Do not put the exception message, SQL, or exception type in the result. Do not claim the write succeeded.
- On a programming error or any exception that is not a `sqlite3.Error`, roll back and re-raise that exception. Do not convert it into `failed`.
- If rollback itself fails, do not replace the original exception with the rollback exception. On the `sqlite3.Error` path, still return `failed` with no database text. On every other path, re-raise the original exception.
- `ConnectionError_` raised by `get_connection()` during connection acquisition returns the same generic `failed` dictionary. It is not propagated to the caller. No transaction was opened, so there is nothing to roll back. The result carries no connection or database error text.
- Raw database error details (messages, SQL text, exception types) are never exposed through a write-operation result.

---

## I. Database Technology Recommendation

**Recommendation: SQLite.**

| Factor | Assessment |
|---|---|
| Fit for this project | Single-user, local, demo-scale capstone with no concurrent-write load. SQLite's file-based model requires no separate server process, which keeps Docker setup and local run instructions simple (matches NFR3 from discovery: runnable via `docker compose up`). |
| Limitations | Weak concurrent-write support (not a concern here — one demo user at a time); limited native type enforcement (mitigated by enforcing enums and checks at the repository layer in addition to `CHECK` constraints, and by the storage conventions below); no built-in network access control (not needed — the DB file is only ever touched by the backend process). |
| Running locally | A single `.db` file, created from a schema script and seeded at startup; trivially included in a Docker volume for persistence across container restarts. |
| Migration path to PostgreSQL | The schema as designed (explicit types, named constraints, indexes, composite foreign keys, no SQLite-only features) maps cleanly to PostgreSQL if a future phase needed multi-user concurrency or a hosted deployment. This is noted as a future option, not a current requirement. |

Already consistent with the HR Leave Assistant practical referenced earlier in this project's history, which used SQLite for the same reasons.

**Storage conventions (Technical Lead revision):**

1. **Monetary values as integer paise, not DECIMAL or FLOAT.** SQLite has no fixed-point decimal type, and storing money as floating point risks rounding errors (e.g., ₹99.99 potentially becoming 99.98999999...). Storing the smallest unit as an integer (1 rupee = 100 paise) avoids this entirely. `total_amount`, `price`, and `refund_amount` are all stored this way; the application/display layer divides by 100 and formats with the ₹ symbol only at presentation time, never in storage.
2. **Timestamps as UTC ISO-8601 text, one consistent format throughout.** SQLite has no native datetime type; storing timestamps as `TEXT` in the format `YYYY-MM-DDTHH:MM:SSZ` (always UTC, always this exact format) means they sort correctly as plain strings and parse unambiguously in Python (`datetime.fromisoformat`, with care for the trailing `Z`). Every `DATETIME`-labeled column in earlier drafts of this document (`order_date`, `delivered_at`, `requested_at`, `created_at`) is corrected to this `TEXT (ISO-8601 UTC)` type throughout Section C.
3. **Explicit foreign-key enforcement on every connection.** SQLite does not enforce foreign keys by default — each new connection must run `PRAGMA foreign_keys = ON;` before any write, or every FK and composite FK in this design (Sections B and C) is silently unenforced. This must be part of the connection-setup code in Phase 2, not an optional flag; it is the single most important implementation detail carried over from this design document, since the entire relational-integrity revision in Section B depends on it being set.

**Reproducible test dates:** return-eligibility logic (Section F) compares `delivered_at` with `reference_time`. When `reference_time` is omitted, `create_return_request` reads the current UTC time once with `datetime.now(timezone.utc)`, truncates it to whole seconds, and uses that single instant for both eligibility and `requested_at`. Callers and tests inject a timezone-aware `datetime` or an ISO-8601 string with an explicit UTC offset (normalized to UTC) so results are reproducible; tests must not rely on the wall clock. `seed_database(None)` is a separate function and continues to use its fixed deterministic baseline `2026-11-07T00:00:00Z`; seed `delivered_at` values are offsets from that baseline, so tests that combine seed data with `create_return_request` must pass `reference_time` explicitly (for example the seed baseline). Support-ticket `created_at` is separate: `create_support_ticket` uses the schema column default (`strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`, UTC, approved format) and does not accept `reference_time`.

---

## J. Seed Data Plan

Proposed synthetic dataset (volumes per the task's required coverage, and per discovery report Section 11 item 4 — 5–8 customers, 2–3 orders each):

- **6 customers** (C101–C106), each with a unique email.
- **2–3 orders per customer** (~15 orders total), deliberately covering:
  - Every one of the 7 `orders.status` values at least once.
  - At least one order with `payment_status = 'Failed'`.
  - At least one order with `payment_status = 'Pending'` (revised enum — the original payment itself not yet settled).
  - At least one order with `refund_status = 'Pending'`, one `'Processed'`, and one `'Failed'` (each with a non-null `refund_amount`, per the refund consistency rule in Sections C/D).
  - At least one `Delivered` order with `delivered_at` **within** 7 days of "today" (eligible for return).
  - At least one `Delivered` order with `delivered_at` **more than** 7 days ago (ineligible — standard window expired).
  - At least one order with `delivered_at` within 48 hours (for the damaged/wrong-item exception path).
  - At least one order containing **multiple `order_items` rows** (multi-item order).
- **Products:** reuse the 5 existing `Products.json` entries for `order_items.product_id`. The catalog has no returnability metadata. The approved return contract does not test or implement a non-returnable outcome (Sections F and L). Adding that behavior needs a separate approved design decision. `Products.json` is not modified.
- **The required authorization scenario, standardized per Technical Lead review and built into the seed data by construction:**
  - `C101` owns `ORD1001` (status `Delivered`, recent — within the 7-day return window) and `ORD1002`.
  - `C102` owns `ORD1003`.
  - `C101` requesting `ORD1003` must receive the identical "not found" result as requesting a nonexistent order ID (e.g., `ORD9999`) — both are included as paired test cases (Section K) so the equivalence itself is verified, not just that access was denied.

Seed data generation itself (a script) is **out of scope for this document**, per the Phase 1 task boundary — this section defines the plan only.

---

## K. Database Test Plan

| # | Test | Expected result |
|---|---|---|
| 1 | Valid customer order lookup | C101 requests ORD1001 → returns the order row |
| 2 | Cross-customer order access denial | C101 requests ORD1003 (C102's) → zero rows, same as nonexistent ID |
| 2b | **Not-found equivalence (new)** | C101 requesting ORD1003 (exists, owned by C102) and C101 requesting ORD9999 (doesn't exist) produce byte-identical application-layer responses → proves Section E's "same not-found result" guarantee, not just that both are denied |
| 3 | Cross-customer order-item access denial | C101 requests an item belonging to ORD1003 → zero rows |
| 4 | Cross-customer return access denial | C101 requests a return_request row created by C102 → zero rows |
| 5 | Invalid return/order/item relationship | Return request submitted with an `item_id` that belongs to a *different* order than the supplied `order_id` → `{"outcome": "item_mismatch", "return_id": None}` and no row (Section F, check 3) |
| 5b | **Composite FK rejection at DB layer (new)** | Attempt a direct insert into `return_requests` with a `(order_id, customer_id)` pair that doesn't exist together in `orders` (bypassing application checks) → SQLite raises a foreign key constraint violation, proving the composite FK is the enforced backstop described in Section B |
| 6 | Cross-customer ticket access denial | C101 requests a ticket belonging to C102 → zero rows |
| 7 | Valid return eligibility (standard window) | Delivered order, 3 days since delivery, `reason_category="standard"` → `{"outcome": "created", "return_id": "<generated-id>"}`. Exactly 168 hours is also eligible. |
| 7b | **Valid return eligibility (48-hour window)** | Delivered order, 36 hours since delivery, `reason_category="damaged"` → created. `wrong_product` uses the same 48-hour window. Exactly 48 hours is eligible. |
| 8 | Invalid return eligibility (expired window) | Delivered order, 10 days since delivery, `reason_category="standard"` → `{"outcome": "outside_window", "return_id": None}`, no row |
| 8b | **Invalid return eligibility (expired 48-hour window)** | Delivered order, 60 hours since delivery, `reason_category="damaged"` or `"wrong_product"` → `outside_window`, even though the order is still inside 168 hours. NULL `delivered_at` and a future `delivered_at` are also `outside_window`. |
| 9 | Duplicate return submission | Second return request for the same `item_id` while the first is still `Requested` or `Approved` → `{"outcome": "duplicate", "return_id": None}` from the pre-check, and the same dictionary if `uq_active_return_per_item` rejects the insert (Section H). This check runs before eligibility. |
| 10 | Ticket creation with unowned order_id | C101 creates a ticket referencing ORD1003 → `{"outcome": "created", "ticket_id": "<generated-id>", "order_id": None, "order_linked": False}`. A nonexistent order ID produces that same result shape. The stored `order_id` is NULL (Section G). |
| 11 | **Refund consistency rule (new)** | Attempt to set `refund_status = 'Processed'` with `refund_amount = NULL`, and separately `refund_status = 'Not Applicable'` with a non-null `refund_amount` → both rejected per the consistency rule in Sections C/D |
| 12 | **Foreign key enforcement is actually on** | Four-part test, corrected per Technical Lead review (see note below): (a) open a connection through the application's normal connection path and confirm it executes `PRAGMA foreign_keys = ON;` as part of setup; (b) immediately query `PRAGMA foreign_keys;` on that same connection and assert the result is `1`, not just that the statement ran without error; (c) on that same connection, attempt an insert that violates a composite foreign key (e.g., a `return_requests` row whose `order_id`/`customer_id` pair doesn't exist together in `orders`) and assert SQLite raises a foreign key constraint violation; (d) **optional, documentation-only** — separately open a second connection that deliberately does *not* execute the pragma, query `PRAGMA foreign_keys;` on it and observe it reports `0`, then attempt the same violating insert and observe SQLite accepts it silently, to demonstrate what the failure mode looks like if the pragma were ever omitted in application code. |

**Correction note on Test 12 (this revision):** the previous draft of this test was logically backward — it described opening a connection *without* the pragma and treated a resulting FK rejection as proof that enforcement was working. That is an invalid test: a connection that never enables FK enforcement should **not** reject a violating insert, so if that draft's test had actually passed, it would have indicated a problem, not confirmed the design. The corrected version above instead verifies the real production path — that the application's normal connection setup executes the pragma, that `PRAGMA foreign_keys` reads back as `1` on that connection, and that a violation is then actually rejected on it — with the no-pragma case now clearly separated out as an optional, documentation-only illustration of the failure mode, never used as evidence that enforcement is active.

Tests 2b, 5b, 7b, 8b, 11, and 12 are new or corrected in this revision, added to directly exercise the strengthened relational integrity (composite FKs), corrected enums, and storage conventions from this review round. These are specified as test *cases* here; the actual `test_authorization.py` implementation is Phase 2 work, explicitly required by `PROJECT_STATUS.md` to pass before any orchestration or UI code is written.

---

## L. Risks and Open Questions

1. **Category-based non-returnable enforcement is an MVP limitation.** `Products.json` has no returnability metadata, and its categories do not match the non-returnable examples in `Policies.md` §2. Checkpoint 7A does not add a `non_returnable` outcome, a schema change, or a catalog change. Enforcing that policy requires a separate approved design decision. `Products.json` remains unmodified.
2. **Four enum sets remain engineering proposals, not dataset facts** (`payment_status`, `refund_status`, `return_requests.status`, `support_tickets.status`/`priority`) — **`payment_status` and `refund_status` values have been corrected per this review round** (Section D) and are now explicitly labeled as proposed engineering enums distinct from the dataset-sourced `orders.status` list; `return_requests.status` and `support_tickets.status`/`priority` are carried over unchanged from the prior draft and still need explicit sign-off.
3. **`delivered_at` cross-column consistency** (must be null unless status is Delivered/Returned) and the **new `refund_amount`/`refund_status` consistency rule** (Section C/D) are both identified as `CHECK` constraint candidates but not fully specified in SQL here, per the design-only boundary — to be finalized in Phase 2.
4. **`product_id` is validated at the application layer, not via a database foreign key**, since the catalog lives in `Products.json`. Unchanged by this review — still a deliberate MVP boundary (Section C), flagged for Technical Lead awareness rather than treated as settled.
5. **Simulated authentication remains a labeled limitation.** The composite foreign keys added in this revision strengthen *relational consistency* between orders, items, returns, and tickets — they do not and cannot substitute for real authentication. The database still cannot itself verify that the Streamlit session selector reflects a real login; that trust boundary sits one layer up, in the application, and should be stated plainly in the final technical report so the security claim isn't overstated.
6. **Teacher-provided schema/code**, if it exists outside this conversation, has not been reviewed. Unchanged by this review — if provided later, this design would need to be reconciled against it rather than assumed compatible.
7. **New (this revision): composite foreign keys require `PRAGMA foreign_keys = ON` on every connection to have any effect** (Section I). This is a single line of setup code, but if it is ever omitted in Phase 2, every composite FK and every plain FK in this design becomes silently unenforced with no error — this is flagged as the single highest-leverage implementation risk introduced by this revision, specifically because its failure mode is silent rather than loud.
8. **Reference time is injectable, and defaults to the real clock** (Sections F and I). `create_return_request(reference_time=None)` reads `datetime.now(timezone.utc)` exactly once and uses that instant for both eligibility and `requested_at`; it is not the seed baseline. Tests must inject `reference_time` to stay reproducible. `seed_database(None)` keeps its fixed `2026-11-07T00:00:00Z` baseline. Ticket `created_at` uses the schema default (`strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`, SQLite's clock, approved UTC format) because Section G gives that function no `reference_time` parameter.

---

## Completion Report — Revision Round 2

**Scope of this round:** a single targeted correction to Test 12 in Section K, requested by Technical Lead review. All other approved design decisions are unchanged — five-table model, SQLite, composite foreign keys, integer-paise monetary storage, UTC ISO-8601 timestamps, the injectable reference-time requirement, customer ownership checks, the existing 7-day/48-hour return windows, and the existing C101/C102/ORD1003 synthetic scenario all carry forward exactly as in Revision Round 1.

**What was wrong:** the prior Test 12 opened a connection *without* the `PRAGMA foreign_keys = ON;` statement and treated a resulting foreign-key rejection as proof that enforcement was active. That is backward — a connection that never enables FK enforcement is not supposed to reject a violating insert, so that test, if it had been implemented as written, would not have validated anything about whether enforcement works in the application's real connection path.

**What changed:** Test 12 now has four parts, run against the application's actual connection setup: (a) confirm the normal connection path executes the pragma, (b) confirm `PRAGMA foreign_keys` reads back as `1` on that connection — not just that the statement executed without error, (c) confirm a composite-FK-violating insert is actually rejected on that same, correctly-configured connection, and (d) an optional, clearly-labeled documentation case showing what happens on a separate connection that deliberately omits the pragma, included only to illustrate the failure mode, never cited as evidence that enforcement is working. A correction note was added directly beneath the test table explaining why the earlier version was invalid, and the summary sentence following the table was updated to say "new or corrected" rather than only "new."

**Sections touched:** Section K (Test 12 row and the explanatory paragraph beneath the table) and this Completion Report. Sections A–J, the ER diagram, all table definitions, the enum definitions, Section I's storage conventions, and Section L's risk list are unchanged from Revision Round 1 — in particular, risk item 7 (the FK-pragma risk itself) already described the risk correctly and did not need correction, only the test that was meant to verify it did.

**Confirmation:** no application code, SQL migration files, seed-data scripts, tests, Streamlit UI, RAG, LangGraph, or voice support was created or modified in this task. No other design decision was altered. Only `docs/01_database_design.md` was touched.

**Stopping here for Technical Lead approval.**

---

<details>
<summary>Prior completion report (Revision Round 1) — retained for history</summary>

## Completion Report — Revision Round 1

### 1. Every change made

| Area | Change |
|---|---|
| Seed-data IDs | All `C202` references replaced with `C102` throughout the document (scenario description, seed plan, test plan) — the authorization scenario is now internally consistent: C101 owns ORD1001/ORD1002, C102 owns ORD1003. |
| Relational integrity | Added `UNIQUE(order_id, customer_id)` on `orders` and `UNIQUE(item_id, order_id)` on `order_items` as composite reference targets. Added two composite foreign keys to `return_requests` and one (conditional on non-NULL) to `support_tickets`. Added a new explanatory passage in Section B on why application-level checks remain necessary alongside the constraints. Sections F and G updated to describe both enforcement layers explicitly. |
| Enums | `payment_status` corrected to `Pending`/`Paid`/`Failed` (refund values removed). `refund_status` corrected to `Not Applicable`/`Pending`/`Processed`/`Failed`. Added an explicit consistency rule linking `refund_status` to `refund_amount`. Both enums re-labeled as proposed engineering enums, distinct from the dataset-sourced `orders.status`. |
| Storage conventions | Added a new conventions block in Section I: monetary columns now specified as INTEGER paise (not DECIMAL); all timestamp columns changed from `DATETIME` to `TEXT (ISO-8601 UTC)` throughout Section C; added an explicit requirement that every SQLite connection run `PRAGMA foreign_keys = ON`; added a requirement for an injectable reference-time parameter in eligibility logic so return-window tests stay reproducible regardless of when they're run. |
| Return eligibility | No new rules invented — both the 7-day standard window and 48-hour damaged/wrong-item window are preserved exactly as in the prior draft (Section F), now cross-referenced against the two-layer enforcement model. |
| Non-returnable product gap | Re-stated explicitly: `Products.json` has not been and will not be modified without approval; the gap remains an open decision (Section L, item 1), not resolved by this revision. |
| Ticket behavior | Preserved: tickets can be created without an order reference. Section G expanded to explain why the application-layer check still runs first even though a composite FK now also exists, and to spell out the exact "not found"-equivalent language used so the system never confirms another customer's order exists. |
| Test plan | Six new test cases added (2b, 5b, 7b, 8b, 11, 12) specifically exercising the not-found equivalence, composite FK rejection, both return-eligibility windows independently, the refund consistency rule, and FK-enforcement-is-actually-on. |
| Risks/open questions | Items 1, 4, 5, 6 carried over as still-open (unchanged by this revision); item 2 updated to reflect the corrected enums; two new items (7, 8) added for the FK-pragma and reference-time risks this revision introduces. |

### 2. How composite foreign keys enforce ownership consistency

A composite foreign key constrains a *pair* of columns to reference a matching pair in another table, rather than constraining each column independently. `return_requests(order_id, customer_id)` referencing `orders(order_id, customer_id)` means the database will only accept a return request row if that exact `order_id`–`customer_id` pairing genuinely exists in `orders` — not merely if the `order_id` exists somewhere and the `customer_id` exists somewhere else. This closes a gap that two independent single-column foreign keys would leave open: independent FKs would allow `order_id='ORD1003'` (which exists, under C102) to be paired with `customer_id='C101'` (which also exists, just on a different order) in a single `return_requests` row, because each FK is satisfied on its own. The composite FK rejects that combination outright, at the database layer, regardless of what application code does.

The same logic applies to `return_requests(item_id, order_id)` against `order_items(item_id, order_id)` — it proves the item genuinely belongs to the stated order, not just that the item exists somewhere.

**The limit of this mechanism, restated plainly:** a composite FK can only ever check "is this combination of IDs one that exists somewhere in the referenced table" — it has no concept of *who is asking*. If an attacker (or a bug) constructed an insert using C102's genuinely-paired `(ORD1003, C102)` but the actual authenticated session belongs to C101, the composite FK would not catch that, because `(ORD1003, C102)` is a real, valid pair — the problem is that the *session* doesn't match it, which only the application layer, comparing against `authenticated_customer_id` from trusted session context, can detect. This is why Section B, F, and G all keep the application-level ownership check as the primary mechanism, with the composite FK as a backstop against bugs, not a replacement for authorization logic.

### 3. Final enum definitions

| Enum | Values | Source |
|---|---|---|
| `orders.status` | `Processing`, `Packed`, `Shipped`, `Out for Delivery`, `Delivered`, `Cancelled`, `Returned` | **Dataset fact** (`FAQ.md` Q1.4) — unchanged |
| `orders.payment_status` | `Pending`, `Paid`, `Failed` | **Proposed engineering enum** — corrected this round |
| `orders.refund_status` | `Not Applicable`, `Pending`, `Processed`, `Failed` | **Proposed engineering enum** — corrected this round |
| `return_requests.status` | `Requested`, `Approved`, `Rejected`, `Completed` | **Proposed engineering enum** — unchanged, still needs sign-off |
| `support_tickets.status` | `Open`, `In Progress`, `Resolved`, `Closed` | **Proposed engineering enum** — unchanged, still needs sign-off |
| `support_tickets.priority` | `Low`, `Normal`, `High`, `Urgent` | **Proposed engineering enum** — unchanged, still needs sign-off |

### 4. Remaining limitations (unresolved by this revision)

- Non-returnable product category has no qualifying seed product; `Products.json` has deliberately not been touched (Section L, item 1).
- `return_requests.status` and `support_tickets.status`/`priority` enums are still unvalidated engineering proposals.
- The `delivered_at` and `refund_amount`/`refund_status` cross-column `CHECK` constraints are specified as requirements here but not written in SQL yet (design-only boundary).
- `product_id` validation against `Products.json` remains application-layer only, not a database FK.
- Simulated authentication is still simulated — composite FKs strengthen internal consistency, not the authentication layer itself.
- No teacher-provided schema/code has been reviewed, since none has been supplied.
- Two new implementation risks introduced by this revision (FK pragma must be set on every connection; eligibility logic must accept an injectable reference time) are documented but not yet implemented — both are correctness-critical for Phase 2.

### 5. Confirmation

No application code, SQL migration files, seed-data scripts, Streamlit UI, RAG, LangGraph, or voice support was created in this task. `Products.json` was read but not modified. This document is design-only, as scoped. The only file touched in this task is `docs/01_database_design.md`.

*(End of Round 1 report.)*

</details>
