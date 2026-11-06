"""Create the five approved tables.

Checkpoint 2 only. Callers must pass a connection from
``app.db.connection.get_connection`` so foreign keys are already on.
This module does not open connections, seed rows, or run queries.
"""

from __future__ import annotations

import sqlite3


# DDL matches docs/01_database_design.md Section C and the enums in Section D.
# Table order follows foreign-key dependencies.
SCHEMA_SQL = """
CREATE TABLE customers (
    customer_id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT
);

CREATE TABLE orders (
    order_id TEXT NOT NULL PRIMARY KEY,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Processing',
    payment_status TEXT NOT NULL DEFAULT 'Pending',
    refund_status TEXT NOT NULL DEFAULT 'Not Applicable',
    refund_amount INTEGER,
    order_date TEXT NOT NULL,
    delivered_at TEXT,
    total_amount INTEGER NOT NULL,
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
);

CREATE INDEX idx_orders_customer_id ON orders (customer_id);

CREATE TABLE order_items (
    item_id TEXT NOT NULL PRIMARY KEY,
    order_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    price INTEGER NOT NULL,
    UNIQUE (item_id, order_id),
    FOREIGN KEY (order_id) REFERENCES orders (order_id),
    CHECK (quantity > 0),
    CHECK (price > 0)
);

CREATE INDEX idx_order_items_order_id ON order_items (order_id);

CREATE TABLE return_requests (
    return_id TEXT NOT NULL PRIMARY KEY,
    customer_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Requested',
    requested_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    FOREIGN KEY (order_id, customer_id)
        REFERENCES orders (order_id, customer_id),
    FOREIGN KEY (item_id, order_id)
        REFERENCES order_items (item_id, order_id),
    CHECK (status IN ('Requested', 'Approved', 'Rejected', 'Completed'))
);

CREATE INDEX idx_return_requests_customer_id
    ON return_requests (customer_id);
CREATE INDEX idx_return_requests_order_id
    ON return_requests (order_id);
CREATE UNIQUE INDEX idx_return_requests_one_active_per_item
    ON return_requests (item_id)
    WHERE status IN ('Requested', 'Approved');

CREATE TABLE support_tickets (
    ticket_id TEXT NOT NULL PRIMARY KEY,
    customer_id TEXT NOT NULL,
    order_id TEXT,
    issue_type TEXT NOT NULL,
    description TEXT NOT NULL,
    conversation_summary TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    priority TEXT NOT NULL DEFAULT 'Normal',
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    FOREIGN KEY (order_id, customer_id)
        REFERENCES orders (order_id, customer_id),
    CHECK (status IN ('Open', 'In Progress', 'Resolved', 'Closed')),
    CHECK (priority IN ('Low', 'Normal', 'High', 'Urgent'))
);

CREATE INDEX idx_support_tickets_customer_id
    ON support_tickets (customer_id);
CREATE INDEX idx_support_tickets_order_id
    ON support_tickets (order_id);
"""


def create_schema(connection: sqlite3.Connection) -> None:
    """Create all five tables on an already-open connection.

    Raises:
        sqlite3.Error: if any statement fails. Nothing here is caught,
            so a failed create is visible to the caller.
    """
    connection.executescript(SCHEMA_SQL)
