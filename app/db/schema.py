"""Create the five approved tables.

Checkpoint 2 only. Callers must pass a connection from
``app.db.connection.get_connection`` so foreign keys are already on.
This module does not open connections, seed rows, or run queries.
"""

from __future__ import annotations

import sqlite3

from app.db.connection import ConnectionError_


# ---------------------------------------------------------------------------
# Table DDL — in foreign-key dependency order.
# Each string is executed individually with connection.execute() so that a
# mid-schema failure can be rolled back cleanly (see create_schema below).
# CREATE TABLE IF NOT EXISTS is used so that the function is idempotent:
# calling it on a database that already has the schema is a safe no-op.
# ---------------------------------------------------------------------------

_CREATE_TABLES: list[str] = [
    # 1. customers — root of all ownership; no FK dependencies.
    """
    CREATE TABLE IF NOT EXISTS customers (
        customer_id TEXT NOT NULL PRIMARY KEY,
        name        TEXT NOT NULL,
        email       TEXT NOT NULL UNIQUE,
        phone       TEXT
    )
    """,

    # 2. orders — depends on customers.
    # Storage conventions (docs/01_database_design.md Section I):
    #   - monetary amounts (refund_amount, total_amount) are INTEGER paise
    #   - timestamps (order_date, delivered_at) are TEXT ISO-8601 UTC
    # UNIQUE(order_id, customer_id) is the composite-FK target for
    # return_requests and support_tickets (Section B revision).
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id       TEXT    NOT NULL PRIMARY KEY,
        customer_id    TEXT    NOT NULL,
        status         TEXT    NOT NULL DEFAULT 'Processing',
        payment_status TEXT    NOT NULL DEFAULT 'Pending',
        refund_status  TEXT    NOT NULL DEFAULT 'Not Applicable',
        refund_amount  INTEGER,
        order_date     TEXT    NOT NULL,
        delivered_at   TEXT,
        total_amount   INTEGER NOT NULL,
        UNIQUE (order_id, customer_id),
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
        CHECK (
            status IN (
                'Processing',
                'Packed',
                'Shipped',
                'Out for Delivery',
                'Delivered',
                'Cancelled',
                'Returned'
            )
        ),
        CHECK (payment_status IN ('Pending', 'Paid', 'Failed')),
        CHECK (
            refund_status IN ('Not Applicable', 'Pending', 'Processed', 'Failed')
        ),
        CHECK (total_amount > 0),
        CHECK (
            (
                refund_status = 'Not Applicable'
                AND refund_amount IS NULL
            )
            OR (
                refund_status != 'Not Applicable'
                AND refund_amount IS NOT NULL
                AND refund_amount >= 0
            )
        ),
        CHECK (
            delivered_at IS NULL
            OR status IN ('Delivered', 'Returned')
        )
    )
    """,

    # 3. order_items — depends on orders.
    # UNIQUE(item_id, order_id) is the composite-FK target for return_requests.
    """
    CREATE TABLE IF NOT EXISTS order_items (
        item_id    TEXT    NOT NULL PRIMARY KEY,
        order_id   TEXT    NOT NULL,
        product_id TEXT    NOT NULL,
        quantity   INTEGER NOT NULL DEFAULT 1,
        price      INTEGER NOT NULL,
        UNIQUE (item_id, order_id),
        FOREIGN KEY (order_id) REFERENCES orders (order_id),
        CHECK (quantity > 0),
        CHECK (price > 0)
    )
    """,

    # 4. return_requests — depends on customers, orders, and order_items.
    # Two composite FKs (Section B revision):
    #   (order_id, customer_id) → orders  — proves order belongs to customer
    #   (item_id,  order_id)   → order_items — proves item belongs to order
    """
    CREATE TABLE IF NOT EXISTS return_requests (
        return_id    TEXT NOT NULL PRIMARY KEY,
        customer_id  TEXT NOT NULL,
        order_id     TEXT NOT NULL,
        item_id      TEXT NOT NULL,
        reason       TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'Requested',
        requested_at TEXT NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
        FOREIGN KEY (order_id, customer_id)
            REFERENCES orders (order_id, customer_id),
        FOREIGN KEY (item_id, order_id)
            REFERENCES order_items (item_id, order_id),
        CHECK (status IN ('Requested', 'Approved', 'Rejected', 'Completed'))
    )
    """,

    # 5. support_tickets — depends on customers and optionally orders.
    # Composite FK on (order_id, customer_id) is only enforced when
    # order_id IS NOT NULL (SQLite ignores FK checks when any referencing
    # column is NULL — the behavior Section G relies on).
    """
    CREATE TABLE IF NOT EXISTS support_tickets (
        ticket_id            TEXT NOT NULL PRIMARY KEY,
        customer_id          TEXT NOT NULL,
        order_id             TEXT,
        issue_type           TEXT NOT NULL,
        description          TEXT NOT NULL,
        conversation_summary TEXT,
        status               TEXT NOT NULL DEFAULT 'Open',
        priority             TEXT NOT NULL DEFAULT 'Normal',
        created_at           TEXT NOT NULL
                                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
        FOREIGN KEY (order_id, customer_id)
            REFERENCES orders (order_id, customer_id),
        CHECK (status IN ('Open', 'In Progress', 'Resolved', 'Closed')),
        CHECK (priority IN ('Low', 'Normal', 'High', 'Urgent'))
    )
    """,
]


# ---------------------------------------------------------------------------
# Index DDL — separated from table DDL so they can be executed in a second
# pass inside the same transaction, after all tables exist.
# CREATE INDEX IF NOT EXISTS / CREATE UNIQUE INDEX IF NOT EXISTS ensures
# the function is idempotent (safe to call on an already-initialised DB).
# ---------------------------------------------------------------------------

_CREATE_INDEXES: list[str] = [
    # orders
    "CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders (customer_id)",

    # order_items
    "CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items (order_id)",

    # return_requests
    "CREATE INDEX IF NOT EXISTS idx_return_requests_customer_id ON return_requests (customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_return_requests_order_id ON return_requests (order_id)",
    # Partial unique index: at most one *active* return per item_id.
    # Name is exactly as approved in docs/01_database_design.md Section H.
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_return_per_item "
        "ON return_requests (item_id) "
        "WHERE status IN ('Requested', 'Approved')"
    ),

    # support_tickets
    "CREATE INDEX IF NOT EXISTS idx_support_tickets_customer_id ON support_tickets (customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_support_tickets_order_id ON support_tickets (order_id)",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_schema(connection: sqlite3.Connection) -> None:
    """Create all five tables and their indexes on an already-open connection.

    The caller is responsible for supplying a connection obtained through
    ``app.db.connection.get_connection()``, which guarantees that
    ``PRAGMA foreign_keys = ON`` has already been executed and verified.
    This function performs its own guard check at the top to catch any
    caller that bypassed that requirement.

    The entire schema creation is wrapped in an explicit transaction:
    all five table DDL statements and all index DDL statements are
    executed together and committed in one shot. If any statement fails,
    a rollback is attempted; if the rollback itself fails, the rollback
    exception is suppressed so the original failure is what propagates
    to the caller.

    Args:
        connection: An open ``sqlite3.Connection``. Must have been
            obtained via ``get_connection()`` so that foreign-key
            enforcement is already active.

    Raises:
        ConnectionError_: if ``PRAGMA foreign_keys`` does not read
            back as ``1`` on the supplied connection, meaning this
            connection was not obtained through ``get_connection()``.
        sqlite3.Error: if any DDL statement or the final commit fails.
            The transaction is rolled back before the error propagates;
            the rollback exception, if any, is suppressed to preserve
            the original error.
    """
    # ------------------------------------------------------------------
    # Guard: verify foreign-key enforcement is active on this connection
    # before touching a single table. This must run before the
    # transaction block so that the guard failure is a clear
    # ConnectionError_, not buried inside a rollback path.
    # ------------------------------------------------------------------
    enforced = connection.execute("PRAGMA foreign_keys;").fetchone()[0]
    if enforced != 1:
        raise ConnectionError_(
            "create_schema() requires a connection with foreign-key "
            "enforcement active (PRAGMA foreign_keys = 1). The supplied "
            "connection reports PRAGMA foreign_keys = "
            f"{enforced!r}. Obtain the connection via "
            "app.db.connection.get_connection() to satisfy this requirement."
        )

    # ------------------------------------------------------------------
    # Atomic schema creation: begin an explicit transaction, execute
    # every table and index statement individually, then commit.
    # On any failure, roll back and re-raise the original exception.
    # ------------------------------------------------------------------
    connection.execute("BEGIN")
    try:
        for statement in _CREATE_TABLES:
            connection.execute(statement)
        for statement in _CREATE_INDEXES:
            connection.execute(statement)
        connection.execute("COMMIT")
    except Exception as original_exc:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            # Suppress the rollback failure so the original DDL/commit
            # error is what the caller sees, not a secondary cleanup error.
            pass
        raise
