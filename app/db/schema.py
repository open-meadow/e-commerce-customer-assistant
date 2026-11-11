"""Create the approved D2 schema on a fresh database (docs/01 Sections C, I).

``create_schema`` is the only schema operation. It is part of controlled
initialization: it takes ``BEGIN IMMEDIATE``, re-checks that the database
is genuinely empty (``user_version`` 0 and no tables), runs the DDL and
stamps ``PRAGMA user_version = 1`` in the **same** transaction. A legacy
or future database is rejected unchanged. There is no migration (M2).

Callers pass a connection from
``app.db.connection.get_initialization_connection``. This module does
not open connections, seed rows or run queries beyond the checks above.
"""

from __future__ import annotations

import sqlite3

from app.db.connection import SCHEMA_VERSION, read_schema_state
from app.db.errors import (
    ConnectionError_,
    DatabaseOperationalError,
    SchemaIncompatibleError,
)

# ---------------------------------------------------------------------------
# CHECK-expression builders. SQLite checks are a defensive backstop only:
# Python validation (app/db/validation.py, app/db/clock.py) is
# authoritative and the two are not claimed to be equivalent for Unicode
# whitespace. The trim set is exactly the six ASCII whitespace characters.
# ---------------------------------------------------------------------------

_TIMESTAMP_GLOB = (
    "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
    "T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'"
)
_ASCII_WHITESPACE = "' ' || char(9, 10, 11, 12, 13)"


def _timestamp_check(column: str, *, nullable: bool = False) -> str:
    core = (
        f"(typeof({column}) = 'text' AND instr({column}, char(0)) = 0 "
        f"AND length({column}) = 20 AND {column} GLOB {_TIMESTAMP_GLOB})"
    )
    return f"({column} IS NULL OR {core})" if nullable else core


def _text_check(column: str, limit: int) -> str:
    return (
        f"(typeof({column}) = 'text' AND instr({column}, char(0)) = 0 "
        f"AND length({column}) <= {limit} "
        f"AND length(trim({column}, {_ASCII_WHITESPACE})) > 0)"
    )


_CREATE_TABLES: list[str] = [
    # 1. customers — root of all ownership.
    """
    CREATE TABLE customers (
        customer_id TEXT NOT NULL PRIMARY KEY,
        name        TEXT NOT NULL,
        email       TEXT NOT NULL UNIQUE,
        phone       TEXT
    )
    """,

    # 2. orders — money is INTEGER paise; timestamps are canonical UTC text.
    f"""
    CREATE TABLE orders (
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
                'Processing', 'Packed', 'Shipped', 'Out for Delivery',
                'Delivered', 'Cancelled', 'Returned'
            )
        ),
        CHECK (payment_status IN ('Pending', 'Paid', 'Failed')),
        CHECK (
            refund_status IN ('Not Applicable', 'Pending', 'Processed', 'Failed')
        ),
        CHECK (total_amount > 0),
        CHECK (
            (refund_status = 'Not Applicable' AND refund_amount IS NULL)
            OR (
                refund_status != 'Not Applicable'
                AND refund_amount IS NOT NULL
                AND refund_amount >= 0
            )
        ),
        CHECK ({_timestamp_check('order_date')}),
        CHECK ({_timestamp_check('delivered_at', nullable=True)}),
        CHECK (
            (status IN ('Delivered', 'Returned') AND delivered_at IS NOT NULL)
            OR (status NOT IN ('Delivered', 'Returned') AND delivered_at IS NULL)
        ),
        CHECK (delivered_at IS NULL OR delivered_at >= order_date)
    )
    """,

    # 3. order_items.
    """
    CREATE TABLE order_items (
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

    # 4. return_requests — reason is the customer's text only; the
    # category has its own column. No category prefix is ever stored.
    f"""
    CREATE TABLE return_requests (
        return_id       TEXT NOT NULL PRIMARY KEY,
        customer_id     TEXT NOT NULL,
        order_id        TEXT NOT NULL,
        item_id         TEXT NOT NULL,
        reason          TEXT NOT NULL,
        reason_category TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'Requested',
        requested_at    TEXT NOT NULL
                             DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
        FOREIGN KEY (order_id, customer_id)
            REFERENCES orders (order_id, customer_id),
        FOREIGN KEY (item_id, order_id)
            REFERENCES order_items (item_id, order_id),
        CHECK (reason_category IN ('damaged', 'wrong_product', 'standard')),
        CHECK ({_text_check('reason', 1000)}),
        CHECK (status IN ('Requested', 'Approved', 'Rejected', 'Completed')),
        CHECK ({_timestamp_check('requested_at')})
    )
    """,

    # 5. support_tickets — the orderless ticket has order_id NULL.
    f"""
    CREATE TABLE support_tickets (
        ticket_id            TEXT NOT NULL PRIMARY KEY,
        customer_id          TEXT NOT NULL,
        order_id             TEXT,
        issue_type           TEXT NOT NULL,
        description          TEXT NOT NULL,
        conversation_summary TEXT NOT NULL,
        status               TEXT NOT NULL DEFAULT 'Open',
        priority             TEXT NOT NULL DEFAULT 'Normal',
        created_at           TEXT NOT NULL
                                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
        FOREIGN KEY (order_id, customer_id)
            REFERENCES orders (order_id, customer_id),
        CHECK (issue_type IN ('order', 'return', 'payment', 'product', 'general')),
        CHECK ({_text_check('description', 2000)}),
        CHECK ({_text_check('conversation_summary', 2000)}),
        CHECK (status IN ('Open', 'In Progress', 'Resolved', 'Closed')),
        CHECK (priority IN ('Low', 'Normal', 'High', 'Urgent')),
        CHECK ({_timestamp_check('created_at')})
    )
    """,
]

_CREATE_INDEXES: list[str] = [
    "CREATE INDEX idx_orders_customer_id ON orders (customer_id)",
    "CREATE INDEX idx_order_items_order_id ON order_items (order_id)",
    "CREATE INDEX idx_return_requests_customer_id ON return_requests (customer_id)",
    "CREATE INDEX idx_return_requests_order_id ON return_requests (order_id)",
    # At most one *active* return per item (docs/01 Section H).
    (
        "CREATE UNIQUE INDEX uq_active_return_per_item "
        "ON return_requests (item_id) "
        "WHERE status IN ('Requested', 'Approved')"
    ),
    "CREATE INDEX idx_support_tickets_customer_id ON support_tickets (customer_id)",
    "CREATE INDEX idx_support_tickets_order_id ON support_tickets (order_id)",
]


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the five tables, indexes and stamp ``user_version = 1``.

    Raises:
        ConnectionError_: foreign keys are not enforced on *connection*,
            the connection is already inside a transaction, or the
            database is already initialized (current schema).
        SchemaIncompatibleError: the database holds a legacy pre-D2
            schema (``outdated``) or a newer schema (``future``). It is
            left unchanged.
        DatabaseOperationalError: a DDL statement failed. The
            transaction is rolled back first.
    """
    enforced = connection.execute("PRAGMA foreign_keys;").fetchone()[0]
    if enforced != 1:
        raise ConnectionError_(
            "create_schema requires a connection with foreign keys enforced; "
            "use get_initialization_connection()"
        )
    if connection.in_transaction:
        raise ConnectionError_("create_schema must not run inside a transaction")

    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise DatabaseOperationalError("could not begin the initialization transaction") from exc
    try:
        state = read_schema_state(connection)
        if state == "legacy":
            raise SchemaIncompatibleError(
                "legacy pre-D2 database; it is preserved unchanged", "outdated"
            )
        if state == "future":
            raise SchemaIncompatibleError("unsupported newer schema version", "future")
        if state == "current":
            raise ConnectionError_("the database is already initialized")
        for statement in _CREATE_TABLES:
            connection.execute(statement)
        for statement in _CREATE_INDEXES:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            connection.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise DatabaseOperationalError("schema creation failed") from exc
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise
