"""Populate the database with the approved synthetic seed dataset.

Checkpoint 4 only. Callers must pass a connection from
``app.db.connection.get_connection`` so foreign keys are already on.

Scope
-----
Exactly three tables are written: ``customers``, ``orders``, and
``order_items``. The ``return_requests`` and ``support_tickets`` tables
are intentionally not seeded here; their records are created through
business-layer workflows or isolated test fixtures in later checkpoints.

Data conventions
----------------
- **Money:** all monetary values (``price``, ``total_amount``,
  ``refund_amount``) are stored as INTEGER paise (1 rupee = 100 paise).
  Every order's ``total_amount`` equals the exact arithmetic sum of its
  line items (``quantity * price``).
- **Timestamps:** all timestamps are TEXT in ``YYYY-MM-DDTHH:MM:SSZ``
  format (UTC, no sub-second precision). Timestamps are calculated as
  fixed offsets from an injectable ``reference_time``; this module never
  calls ``datetime.now()``.
- **Products:** every ``product_id`` is validated against the read-only
  ``data/source/Products.json`` catalog. An unknown ID causes the seed
  operation to abort immediately with a ``ValueError`` before any rows
  are written.
- **Atomicity:** the entire seed operation runs inside one explicit
  transaction. If any insertion fails, the transaction is rolled back and
  the original exception propagates. The database is never left in a
  partially-seeded state.
- **Idempotency:** the function rejects execution if any of the three
  target tables already contains rows. It does not silently overwrite or
  skip existing data.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Union

from app.db.connection import ConnectionError_

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Location of the product catalog relative to this project's root.
# Resolved at import time so that the module can be used from any working
# directory. __file__ → .../app/db/seed_data.py → up two levels = project root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PRODUCTS_JSON_PATH = os.path.join(_PROJECT_ROOT, "data", "source", "Products.json")

# Default reference "now" value: the fixed approved baseline.
# This is intentionally a constant string, not a call to datetime.now().
_DEFAULT_REFERENCE_TIME_STR = "2026-11-07T00:00:00Z"

# Convenience shorthand used throughout the timestamp helpers.
_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_valid_product_ids() -> frozenset[str]:
    """Load the read-only product catalog and return the set of valid IDs.

    Raises:
        FileNotFoundError: if Products.json cannot be found at the
            expected path.
        ValueError: if the JSON is malformed or any entry lacks an ``id``
            field.
    """
    try:
        with open(_PRODUCTS_JSON_PATH, encoding="utf-8") as fh:
            products = json.load(fh)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Products.json not found at expected path: {_PRODUCTS_JSON_PATH}"
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Products.json at {_PRODUCTS_JSON_PATH} is not valid JSON: {exc}"
        ) from exc

    ids: set[str] = set()
    for entry in products:
        if "id" not in entry:
            raise ValueError(
                f"Products.json entry missing 'id' field: {entry!r}"
            )
        ids.add(entry["id"])
    return frozenset(ids)


def _parse_reference_time(
    reference_time: Union[datetime, str, None],
) -> datetime:
    """Normalize *reference_time* to a UTC-aware datetime.

    Rules (approved spec §7):
    - ``None`` → use the fixed default baseline (never ``datetime.now()``).
    - ``str`` → parse as ISO-8601; must include explicit UTC offset (e.g. Z).
    - ``datetime`` → must already be timezone-aware; naive datetimes are
      rejected.

    Returns:
        A timezone-aware ``datetime`` in UTC.

    Raises:
        TypeError: if *reference_time* is an unsupported type.
        ValueError: if *reference_time* is a timezone-naive datetime or a
            string that cannot be parsed or lacks a UTC offset.
    """
    if reference_time is None:
        # Fixed baseline — never reads the system clock.
        return datetime.fromisoformat(_DEFAULT_REFERENCE_TIME_STR.replace("Z", "+00:00"))

    if isinstance(reference_time, str):
        # Accept both "...Z" (replace with +00:00 for fromisoformat) and
        # any explicit offset string such as "2026-11-07T00:00:00+05:30".
        normalized = reference_time.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(
                f"reference_time string {reference_time!r} could not be "
                f"parsed as ISO-8601: {exc}"
            ) from exc
        if dt.tzinfo is None:
            raise ValueError(
                f"reference_time string {reference_time!r} has no UTC offset. "
                "Supply an explicit offset (e.g. append 'Z' or '+00:00')."
            )
        return dt.astimezone(_UTC)

    if isinstance(reference_time, datetime):
        if reference_time.tzinfo is None:
            raise ValueError(
                "reference_time datetime is timezone-naive. "
                "Supply a timezone-aware datetime (e.g. "
                "datetime(..., tzinfo=timezone.utc))."
            )
        return reference_time.astimezone(_UTC)

    raise TypeError(
        f"reference_time must be a datetime, an ISO-8601 str, or None; "
        f"got {type(reference_time).__name__!r}."
    )


def _fmt(dt: datetime) -> str:
    """Format a UTC-aware datetime as the approved ISO-8601 string.

    Returns a string in ``YYYY-MM-DDTHH:MM:SSZ`` format, truncated to
    whole seconds (no microseconds).
    """
    utc_dt = dt.astimezone(_UTC)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _offset(ref: datetime, **kwargs: int) -> str:
    """Return ref + timedelta(**kwargs) formatted as an ISO-8601 timestamp.

    Example: ``_offset(ref, days=-3)`` → ref minus 3 days.
    """
    return _fmt(ref + timedelta(**kwargs))


def _validate_product_ids(
    items: list[dict],
    valid_ids: frozenset[str],
) -> None:
    """Abort immediately if any item references an unknown product ID.

    Args:
        items: list of dicts, each with at least a ``product_id`` key.
        valid_ids: the set of IDs loaded from Products.json.

    Raises:
        ValueError: on the first unknown product ID encountered.
    """
    for item in items:
        pid = item["product_id"]
        if pid not in valid_ids:
            raise ValueError(
                f"Unknown product_id {pid!r} in seed data. "
                f"Valid IDs from Products.json: {sorted(valid_ids)}. "
                "Seed aborted; no rows have been written."
            )


def _validate_order_totals(
    orders: list[dict],
    items_by_order: dict[str, list[dict]],
) -> None:
    """Verify every order total equals the sum of its line items.

    Args:
        orders: list of order dicts with an ``order_id`` and
            ``total_amount`` key.
        items_by_order: mapping of order_id → list of item dicts, each
            with ``quantity`` and ``price`` keys (all in paise).

    Raises:
        ValueError: if any order's total does not match its item sum.
    """
    for order in orders:
        oid = order["order_id"]
        expected = order["total_amount"]
        line_items = items_by_order.get(oid, [])
        actual = sum(it["quantity"] * it["price"] for it in line_items)
        if actual != expected:
            raise ValueError(
                f"Order {oid!r}: total_amount {expected} paise does not "
                f"match sum of line items {actual} paise. "
                "Seed aborted; no rows have been written."
            )


def _check_tables_empty(connection: sqlite3.Connection) -> None:
    """Refuse to seed if any target table already contains rows.

    Args:
        connection: an open connection with the schema already applied.

    Raises:
        RuntimeError: if ``customers``, ``orders``, or ``order_items``
            contains at least one row.
    """
    for table in ("customers", "orders", "order_items"):
        count = connection.execute(
            f"SELECT COUNT(*) FROM {table}"  # noqa: S608 — table name is a literal constant
        ).fetchone()[0]
        if count > 0:
            raise RuntimeError(
                f"Table '{table}' already contains {count} row(s). "
                "seed_database() will not overwrite or delete existing data. "
                "Start from a fresh database or drop and recreate the schema "
                "before seeding."
            )


# ---------------------------------------------------------------------------
# Seed data definitions
# ---------------------------------------------------------------------------

def _build_seed_data(ref: datetime) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (customers, orders, order_items) with timestamps relative to *ref*.

    All monetary values are in integer paise.
    Phone is NULL for all customers per approved spec decision §3.

    Args:
        ref: UTC-aware datetime used as the reference "now" for all
            delivered_at and order_date offsets.

    Returns:
        A 3-tuple of (customers, orders, order_items) where each element
        is a list of dicts ready to be passed to parameterized SQL.
    """
    # -----------------------------------------------------------------
    # Customers — 6 customers, C101–C106, unique emails, phone=NULL.
    # -----------------------------------------------------------------
    customers = [
        {"customer_id": "C101", "name": "Alice Walker",   "email": "alice.walker@example.com",   "phone": None},
        {"customer_id": "C102", "name": "Bob Smith",      "email": "bob.smith@example.com",      "phone": None},
        {"customer_id": "C103", "name": "Charlie Patel",  "email": "charlie.patel@example.com",  "phone": None},
        {"customer_id": "C104", "name": "Diana Roy",      "email": "diana.roy@example.com",      "phone": None},
        {"customer_id": "C105", "name": "Evan Sharma",    "email": "evan.sharma@example.com",    "phone": None},
        {"customer_id": "C106", "name": "Fiona Das",      "email": "fiona.das@example.com",      "phone": None},
    ]

    # -----------------------------------------------------------------
    # Orders — 15 orders, 2–3 per customer.
    #
    # Offset notation: days=-N means order_date is N days before ref.
    # delivered_at is only set for Delivered / Returned orders.
    #
    # Return-eligibility coverage (relative to ref = T0):
    #   ORD1001  delivered T0-72h  (-3 days)  → within 7-day window; outside 48h window
    #   ORD1003  delivered T0-240h (-10 days) → outside 7-day window (ineligible)
    #   ORD1005  delivered T0-36h             → within both 7-day AND 48h damaged windows
    #   ORD1010  delivered T0-288h (-12 days) → outside 7-day window; already Returned
    #   ORD1012  delivered T0-360h (-15 days) → outside 7-day window
    #   ORD1013  delivered T0-60h             → within 7-day window; outside 48h window
    #
    # Enum coverage:
    #   orders.status        : all 7 canonical values covered
    #   orders.payment_status: Paid, Pending, Failed all covered
    #   orders.refund_status : Not Applicable, Pending, Processed, Failed all covered
    # -----------------------------------------------------------------
    orders = [
        # --- C101 (3 orders) ---
        {
            "order_id":       "ORD1001",
            "customer_id":    "C101",
            "status":         "Delivered",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-5),
            "delivered_at":   _offset(ref, hours=-72),   # T0 - 3 days; in-window
            "total_amount":   249900,                     # 1 × P1001 ₹2499
        },
        {
            "order_id":       "ORD1002",
            "customer_id":    "C101",
            "status":         "Shipped",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-2),
            "delivered_at":   None,
            "total_amount":   199900,                     # 1 × P1003 ₹1999
        },
        {
            "order_id":       "ORD1014",
            "customer_id":    "C101",
            "status":         "Processing",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-1),
            "delivered_at":   None,
            "total_amount":   199900,                     # 1 × P1003 ₹1999
        },

        # --- C102 (3 orders) ---
        {
            "order_id":       "ORD1003",
            "customer_id":    "C102",
            "status":         "Delivered",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-14),
            "delivered_at":   _offset(ref, hours=-240),  # T0 - 10 days; expired window
            "total_amount":   379900,                     # 1 × P1002 ₹3799
        },
        {
            "order_id":       "ORD1004",
            "customer_id":    "C102",
            "status":         "Processing",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-1),
            "delivered_at":   None,
            "total_amount":   499800,                     # 2 × P1010 ₹2499
        },
        {
            "order_id":       "ORD1015",
            "customer_id":    "C102",
            "status":         "Shipped",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-2),
            "delivered_at":   None,
            "total_amount":   249900,                     # 1 × P1001 ₹2499
        },

        # --- C103 (2 orders) ---
        {
            "order_id":       "ORD1005",
            "customer_id":    "C103",
            "status":         "Delivered",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-4),
            "delivered_at":   _offset(ref, hours=-36),   # T0 - 36h; inside 48h damaged window
            "total_amount":   1199800,                    # 1 × P1006 ₹9999 + 1 × P1003 ₹1999
        },
        {
            "order_id":       "ORD1006",
            "customer_id":    "C103",
            "status":         "Cancelled",
            "payment_status": "Paid",
            "refund_status":  "Processed",
            "refund_amount":  249900,                     # refund amount in paise ₹2499
            "order_date":     _offset(ref, days=-3),
            "delivered_at":   None,                       # Cancelled; no delivery
            "total_amount":   249900,                     # 1 × P1001 ₹2499
        },

        # --- C104 (3 orders) ---
        {
            "order_id":       "ORD1007",
            "customer_id":    "C104",
            "status":         "Out for Delivery",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-2),
            "delivered_at":   None,
            "total_amount":   249900,                     # 1 × P1010 ₹2499
        },
        {
            "order_id":       "ORD1008",
            "customer_id":    "C104",
            "status":         "Processing",
            "payment_status": "Failed",                   # payment_status = Failed
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-1),
            "delivered_at":   None,
            "total_amount":   999900,                     # 1 × P1006 ₹9999
        },
        {
            "order_id":       "ORD1009",
            "customer_id":    "C104",
            "status":         "Packed",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-2),
            "delivered_at":   None,
            "total_amount":   379900,                     # 1 × P1002 ₹3799
        },

        # --- C105 (2 orders) ---
        {
            "order_id":       "ORD1010",
            "customer_id":    "C105",
            "status":         "Returned",
            "payment_status": "Paid",
            "refund_status":  "Pending",                  # refund_status = Pending
            "refund_amount":  199900,                     # ₹1999 pending refund
            "order_date":     _offset(ref, days=-16),
            "delivered_at":   _offset(ref, hours=-288),  # T0 - 12 days; already returned
            "total_amount":   199900,                     # 1 × P1003 ₹1999
        },
        {
            "order_id":       "ORD1011",
            "customer_id":    "C105",
            "status":         "Processing",
            "payment_status": "Pending",                  # payment_status = Pending
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-1),
            "delivered_at":   None,
            "total_amount":   249900,                     # 1 × P1001 ₹2499
        },

        # --- C106 (2 orders) ---
        {
            "order_id":       "ORD1012",
            "customer_id":    "C106",
            "status":         "Delivered",
            "payment_status": "Paid",
            "refund_status":  "Failed",                   # refund_status = Failed
            "refund_amount":  379900,                     # ₹3799 attempted (not cleared)
            "order_date":     _offset(ref, days=-20),
            "delivered_at":   _offset(ref, hours=-360),  # T0 - 15 days; expired window
            "total_amount":   379900,                     # 1 × P1002 ₹3799
        },
        {
            "order_id":       "ORD1013",
            "customer_id":    "C106",
            "status":         "Delivered",
            "payment_status": "Paid",
            "refund_status":  "Not Applicable",
            "refund_amount":  None,
            "order_date":     _offset(ref, days=-5),
            "delivered_at":   _offset(ref, hours=-60),   # T0 - 60h; inside 7d, outside 48h
            "total_amount":   249900,                     # 1 × P1010 ₹2499
        },
    ]

    # -----------------------------------------------------------------
    # Order items — one or two items per order.
    # price values are in paise; total_amount must equal sum(qty * price).
    # -----------------------------------------------------------------
    order_items = [
        # ORD1001 — C101 — 1 × P1001 (₹2499 = 249900 paise)
        {"item_id": "IT101", "order_id": "ORD1001", "product_id": "P1001", "quantity": 1, "price": 249900},

        # ORD1002 — C101 — 1 × P1003 (₹1999 = 199900 paise)
        {"item_id": "IT102", "order_id": "ORD1002", "product_id": "P1003", "quantity": 1, "price": 199900},

        # ORD1014 — C101 — 1 × P1003 (₹1999 = 199900 paise)
        {"item_id": "IT115", "order_id": "ORD1014", "product_id": "P1003", "quantity": 1, "price": 199900},

        # ORD1003 — C102 — 1 × P1002 (₹3799 = 379900 paise)
        {"item_id": "IT103", "order_id": "ORD1003", "product_id": "P1002", "quantity": 1, "price": 379900},

        # ORD1004 — C102 — 2 × P1010 (₹2499 each = 499800 paise total)
        {"item_id": "IT104", "order_id": "ORD1004", "product_id": "P1010", "quantity": 2, "price": 249900},

        # ORD1015 — C102 — 1 × P1001 (₹2499 = 249900 paise)
        {"item_id": "IT116", "order_id": "ORD1015", "product_id": "P1001", "quantity": 1, "price": 249900},

        # ORD1005 — C103 — 2-item order: P1006 + P1003 (₹9999 + ₹1999 = 1199800 paise)
        {"item_id": "IT105", "order_id": "ORD1005", "product_id": "P1006", "quantity": 1, "price": 999900},
        {"item_id": "IT106", "order_id": "ORD1005", "product_id": "P1003", "quantity": 1, "price": 199900},

        # ORD1006 — C103 — 1 × P1001 (₹2499 = 249900 paise)
        {"item_id": "IT107", "order_id": "ORD1006", "product_id": "P1001", "quantity": 1, "price": 249900},

        # ORD1007 — C104 — 1 × P1010 (₹2499 = 249900 paise)
        {"item_id": "IT108", "order_id": "ORD1007", "product_id": "P1010", "quantity": 1, "price": 249900},

        # ORD1008 — C104 — 1 × P1006 (₹9999 = 999900 paise)
        {"item_id": "IT109", "order_id": "ORD1008", "product_id": "P1006", "quantity": 1, "price": 999900},

        # ORD1009 — C104 — 1 × P1002 (₹3799 = 379900 paise)
        {"item_id": "IT110", "order_id": "ORD1009", "product_id": "P1002", "quantity": 1, "price": 379900},

        # ORD1010 — C105 — 1 × P1003 (₹1999 = 199900 paise)
        {"item_id": "IT111", "order_id": "ORD1010", "product_id": "P1003", "quantity": 1, "price": 199900},

        # ORD1011 — C105 — 1 × P1001 (₹2499 = 249900 paise)
        {"item_id": "IT112", "order_id": "ORD1011", "product_id": "P1001", "quantity": 1, "price": 249900},

        # ORD1012 — C106 — 1 × P1002 (₹3799 = 379900 paise)
        {"item_id": "IT113", "order_id": "ORD1012", "product_id": "P1002", "quantity": 1, "price": 379900},

        # ORD1013 — C106 — 1 × P1010 (₹2499 = 249900 paise)
        {"item_id": "IT114", "order_id": "ORD1013", "product_id": "P1010", "quantity": 1, "price": 249900},
    ]

    return customers, orders, order_items


# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------

_INSERT_CUSTOMER = (
    "INSERT INTO customers (customer_id, name, email, phone) "
    "VALUES (:customer_id, :name, :email, :phone)"
)

_INSERT_ORDER = (
    "INSERT INTO orders "
    "(order_id, customer_id, status, payment_status, refund_status, "
    "refund_amount, order_date, delivered_at, total_amount) "
    "VALUES (:order_id, :customer_id, :status, :payment_status, :refund_status, "
    ":refund_amount, :order_date, :delivered_at, :total_amount)"
)

_INSERT_ORDER_ITEM = (
    "INSERT INTO order_items (item_id, order_id, product_id, quantity, price) "
    "VALUES (:item_id, :order_id, :product_id, :quantity, :price)"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def seed_database(
    connection: sqlite3.Connection,
    reference_time: Union[datetime, str, None] = None,
) -> None:
    """Populate customers, orders, and order_items with synthetic seed data.

    This function is atomic: either all rows are inserted successfully and
    committed, or the transaction is rolled back and no rows are written.
    It is intentionally not idempotent: calling it on a database that
    already has rows in any of the three target tables raises
    ``RuntimeError`` immediately, before any writes.

    Args:
        connection: An open ``sqlite3.Connection`` obtained via
            ``app.db.connection.get_connection()``. Must have foreign-key
            enforcement active (``PRAGMA foreign_keys = 1``). Must not
            already be in a transaction (``connection.in_transaction``
            must be ``False``).
        reference_time: Optional "now" anchor for relative timestamps.
            Accepts:
              - ``None`` → uses the fixed baseline ``2026-11-07T00:00:00Z``.
              - ``str``  → parsed as ISO-8601 with an explicit UTC offset.
              - ``datetime`` → must be timezone-aware.
            Timezone-naive values are always rejected.
            ``datetime.now()`` is never called internally.

    Raises:
        ConnectionError_: if the connection does not have foreign-key
            enforcement active.
        RuntimeError: if the connection is already in a transaction, or
            if any target table already contains rows.
        TypeError: if *reference_time* is an unsupported type.
        ValueError: if *reference_time* is timezone-naive or unparseable,
            if a product ID is not found in Products.json, or if any
            order total does not equal the sum of its line items.
        sqlite3.Error: if any INSERT fails. The transaction is rolled back
            before this propagates.
    """
    # ------------------------------------------------------------------
    # Guard 1: foreign-key enforcement must be active.
    # ------------------------------------------------------------------
    enforced = connection.execute("PRAGMA foreign_keys;").fetchone()[0]
    if enforced != 1:
        raise ConnectionError_(
            "seed_database() requires a connection with foreign-key "
            "enforcement active (PRAGMA foreign_keys = 1). The supplied "
            f"connection reports PRAGMA foreign_keys = {enforced!r}. "
            "Obtain the connection via app.db.connection.get_connection()."
        )

    # ------------------------------------------------------------------
    # Guard 2: must not already be inside a caller-owned transaction.
    # ------------------------------------------------------------------
    if connection.in_transaction:
        raise RuntimeError(
            "seed_database() was called on a connection that is already "
            "inside a transaction. The function manages its own atomic "
            "transaction and must not be called inside an existing one."
        )

    # ------------------------------------------------------------------
    # Pre-flight checks (all run before the transaction opens).
    # ------------------------------------------------------------------
    ref = _parse_reference_time(reference_time)

    # Load and validate product IDs from the read-only catalog.
    valid_product_ids = _load_valid_product_ids()

    # Build the full dataset, then validate it before touching the DB.
    customers, orders, order_items = _build_seed_data(ref)

    _validate_product_ids(order_items, valid_product_ids)

    items_by_order: dict[str, list[dict]] = {}
    for item in order_items:
        items_by_order.setdefault(item["order_id"], []).append(item)
    _validate_order_totals(orders, items_by_order)

    # ------------------------------------------------------------------
    # Atomic seed execution: BEGIN IMMEDIATE → empty-table check →
    # all INSERTs → COMMIT.
    # BEGIN IMMEDIATE acquires a write reservation immediately so
    # concurrent operations cannot race between the empty-table check
    # and subsequent inserts.
    # On any failure (empty-table violation, INSERT error, or COMMIT
    # failure), the transaction is rolled back and the original
    # exception is re-raised.
    # Insertion order respects FK dependencies:
    #   customers → orders → order_items.
    # ------------------------------------------------------------------
    connection.execute("BEGIN IMMEDIATE")
    try:
        _check_tables_empty(connection)

        for customer in customers:
            connection.execute(_INSERT_CUSTOMER, customer)

        for order in orders:
            connection.execute(_INSERT_ORDER, order)

        for item in order_items:
            connection.execute(_INSERT_ORDER_ITEM, item)

        connection.execute("COMMIT")

    except Exception:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            # Suppress: the original exception is more informative.
            pass
        raise


# ---------------------------------------------------------------------------
# Script entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Import here (not at module top) to keep the entrypoint self-contained
    # and avoid circular-import concerns if this module is ever imported
    # without running as a script.
    from app.db.connection import DEFAULT_DB_PATH, get_connection
    from app.db.schema import create_schema

    print(f"Database path: {DEFAULT_DB_PATH}")
    conn = get_connection()
    try:
        print("Creating schema...")
        create_schema(conn)
        print("Seeding database...")
        seed_database(conn)
        # Quick verification summary.
        n_customers = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        n_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        n_items = conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]
        print(
            f"Done. Inserted {n_customers} customer(s), "
            f"{n_orders} order(s), "
            f"{n_items} order item(s)."
        )
    finally:
        conn.close()
