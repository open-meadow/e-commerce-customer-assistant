"""Create the approved D2 schema on a fresh database (docs/01 Sections C, I).

``create_schema`` creates a fresh database. It is part of controlled
initialization: it takes ``BEGIN IMMEDIATE``, re-checks that the database
is genuinely empty (``user_version`` 0 and no tables), runs the DDL and
stamps ``PRAGMA user_version = SCHEMA_VERSION`` in the **same** transaction. A legacy,
outdated or future database is rejected unchanged. Upgrading an existing version-1
database is the separate, operator-run ``app.db.migrations`` (Phase 2); it reuses the ticket DDL defined here
(``ticket_rebuild_statements``) so a fresh and a migrated database are structurally identical.

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


_TICKET_STATUSES = (
    "('New', 'Open', 'In Progress', 'Pending', 'Waiting for customer', 'On hold', "
    "'Resolved', 'Reopened', 'Closed', 'Cancelled')"
)
# an active ticket is any non-terminal one (OD-03); the partial unique index is written as "not terminal"
_TERMINAL_STATUSES = "('Resolved', 'Closed', 'Cancelled')"
_KEY_TEXT_MAX = 256


def _key_check(column: str) -> str:
    return (
        f"(typeof({column}) = 'text' AND instr({column}, char(0)) = 0 "
        f"AND length({column}) BETWEEN 1 AND {_KEY_TEXT_MAX})"
    )


def support_tickets_ddl(name: str) -> str:
    """The final ``support_tickets`` table (design 2.1). ``name`` is ``support_tickets`` or the rebuild's temporary name."""
    return f"""
    CREATE TABLE {name} (
        ticket_id            TEXT NOT NULL PRIMARY KEY,
        customer_id          TEXT NOT NULL,
        order_id             TEXT,
        item_id              TEXT,
        issue_type           TEXT NOT NULL,
        issue_key            TEXT NOT NULL,
        description          TEXT NOT NULL,
        conversation_summary TEXT,
        status               TEXT NOT NULL DEFAULT 'Open',
        priority             TEXT NOT NULL DEFAULT 'Normal',
        parent_ticket_id     TEXT,
        request_key          TEXT NOT NULL,
        request_hash         TEXT NOT NULL,
        created_at           TEXT NOT NULL
                                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated_at           TEXT NOT NULL
                                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        redacted_at          TEXT,
        UNIQUE (ticket_id, customer_id),
        UNIQUE (customer_id, request_key),
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
        FOREIGN KEY (order_id, customer_id)
            REFERENCES orders (order_id, customer_id),
        FOREIGN KEY (item_id, order_id)
            REFERENCES order_items (item_id, order_id),
        FOREIGN KEY (parent_ticket_id, customer_id)
            REFERENCES {name} (ticket_id, customer_id),
        CHECK (item_id IS NULL OR order_id IS NOT NULL),
        CHECK (issue_type IN ('order', 'return', 'payment', 'product', 'general')),
        CHECK (order_id IS NULL OR instr(order_id, ':') = 0),
        CHECK (item_id IS NULL OR instr(item_id, ':') = 0),
        CHECK (
            (issue_type IN ('product', 'return') AND order_id IS NOT NULL AND item_id IS NOT NULL
                AND issue_key = 'i:' || order_id || ':' || item_id)
            OR (issue_type IN ('order', 'payment') AND order_id IS NOT NULL AND item_id IS NULL
                AND issue_key = 'o:' || order_id)
            OR (issue_key = 'n:' || ticket_id)
        ),
        CHECK ({_text_check('description', 2000)}),
        CHECK (conversation_summary IS NULL OR {_text_check('conversation_summary', 2000)}),
        CHECK (status IN {_TICKET_STATUSES}),
        CHECK (priority IN ('Low', 'Normal', 'High', 'Urgent')),
        CHECK ({_key_check('request_key')}),
        CHECK ({_key_check('request_hash')}),
        CHECK ({_timestamp_check('created_at')}),
        CHECK ({_timestamp_check('updated_at')}),
        CHECK ({_timestamp_check('redacted_at', nullable=True)}),
        CHECK (updated_at >= created_at),
        CHECK (redacted_at IS NULL OR redacted_at >= created_at)
    )
    """


_TICKET_SIDE_TABLES: list[str] = [
    # ticket_events: append-only (design 2.2).
    f"""
    CREATE TABLE ticket_events (
        event_id              TEXT NOT NULL PRIMARY KEY,
        ticket_id             TEXT NOT NULL,
        customer_id           TEXT NOT NULL,
        seq                   INTEGER NOT NULL CHECK (seq >= 1),
        kind                  TEXT NOT NULL
                                  CHECK (kind IN ('created', 'comment', 'status_change', 'follow_up_link', 'escalation')),
        actor_type            TEXT NOT NULL CHECK (actor_type IN ('customer', 'agent', 'system')),
        actor_ref             TEXT NOT NULL,
        body                  TEXT,
        intent                TEXT CHECK (intent IN ('information', 'issue_persists')),
        staff_review_required INTEGER NOT NULL DEFAULT 0 CHECK (staff_review_required IN (0, 1)),
        from_status           TEXT,
        to_status             TEXT,
        transition_reason     TEXT CHECK (transition_reason IN
                                  ('customer_supplied_requested_information',
                                   'customer_reported_issue_persists', 'staff_action')),
        trigger_event_id      TEXT,
        request_key           TEXT NOT NULL,
        request_hash          TEXT NOT NULL,
        created_at            TEXT NOT NULL,
        redacted_at           TEXT,
        UNIQUE (ticket_id, seq),
        UNIQUE (ticket_id, event_id),
        UNIQUE (customer_id, request_key),
        FOREIGN KEY (ticket_id, customer_id) REFERENCES support_tickets (ticket_id, customer_id),
        FOREIGN KEY (ticket_id, trigger_event_id) REFERENCES ticket_events (ticket_id, event_id),
        CHECK (kind <> 'comment' OR intent IS NOT NULL),
        CHECK (kind = 'comment' OR intent IS NULL),
        CHECK (kind <> 'comment' OR redacted_at IS NOT NULL
               OR (body IS NOT NULL AND length(body) <= 2000
                   AND length(trim(body, {_ASCII_WHITESPACE})) > 0)),
        CHECK (redacted_at IS NULL OR body IS NULL),
        CHECK (staff_review_required = 0 OR kind = 'comment'),
        CHECK (from_status IS NULL OR from_status IN {_TICKET_STATUSES}),
        CHECK (to_status IS NULL OR to_status IN {_TICKET_STATUSES}),
        CHECK (kind <> 'status_change' OR (from_status IS NOT NULL AND to_status IS NOT NULL
                                           AND from_status <> to_status AND transition_reason IS NOT NULL)),
        CHECK (kind = 'status_change' OR (from_status IS NULL AND to_status IS NULL
                                          AND transition_reason IS NULL AND trigger_event_id IS NULL)),
        CHECK (transition_reason IS NULL OR transition_reason = 'staff_action'
               OR (actor_type = 'system' AND trigger_event_id IS NOT NULL)),
        CHECK (transition_reason IS NULL OR transition_reason <> 'staff_action' OR actor_type = 'agent'),
        CHECK (kind <> 'status_change' OR actor_type <> 'customer'),
        CHECK ({_key_check('request_key')}),
        CHECK ({_key_check('request_hash')}),
        CHECK ({_timestamp_check('created_at')}),
        CHECK ({_timestamp_check('redacted_at', nullable=True)}),
        CHECK (redacted_at IS NULL OR redacted_at >= created_at)
    )
    """,
    # ticket_erasure_log: audit of every erasure, holds no text and no customer data (OD-09).
    f"""
    CREATE TABLE ticket_erasure_log (
        erasure_id        TEXT NOT NULL PRIMARY KEY,
        performed_at      TEXT NOT NULL,
        operator_ref      TEXT NOT NULL,
        authorization_ref TEXT NOT NULL,
        reason_code       TEXT NOT NULL CHECK (reason_code IN ('privacy_request', 'retention_expiry', 'legal_order')),
        scope_customer_id TEXT NOT NULL,
        tickets_redacted  INTEGER NOT NULL CHECK (tickets_redacted >= 0),
        events_redacted   INTEGER NOT NULL CHECK (events_redacted >= 0),
        request_key       TEXT NOT NULL UNIQUE,
        FOREIGN KEY (scope_customer_id) REFERENCES customers (customer_id),
        CHECK ({_timestamp_check('performed_at')})
    )
    """,
    # ticket_write_intents [R]: the durable half of idempotency. The request key and the immutable payload are
    # persisted BEFORE the insert is attempted and marked committed in the same transaction as the ticket row, so an
    # uncertain outcome can be recovered across session expiry and process restart.
    f"""
    CREATE TABLE ticket_write_intents (
        customer_id  TEXT NOT NULL,
        request_key  TEXT NOT NULL,
        payload_json TEXT,
        request_hash TEXT NOT NULL,
        state        TEXT NOT NULL DEFAULT 'prepared',
        last_outcome TEXT,
        ticket_id    TEXT,
        created_at   TEXT NOT NULL,
        resolved_at  TEXT,
        redacted_at  TEXT,
        PRIMARY KEY (customer_id, request_key),
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
        FOREIGN KEY (ticket_id, customer_id) REFERENCES support_tickets (ticket_id, customer_id),
        CHECK (state IN ('prepared', 'committed')),
        CHECK ((state = 'committed') = (ticket_id IS NOT NULL)),
        CHECK (last_outcome IS NULL OR last_outcome IN ('rate_limited', 'duplicate_active', 'item_not_found', 'parent_not_found', 'unavailable')),
        CHECK (payload_json IS NULL OR (typeof(payload_json) = 'text' AND instr(payload_json, char(0)) = 0)),
        CHECK (payload_json IS NOT NULL OR redacted_at IS NOT NULL),
        CHECK (redacted_at IS NULL OR payload_json IS NULL),
        CHECK ({_key_check('request_key')}),
        CHECK ({_key_check('request_hash')}),
        CHECK ({_timestamp_check('created_at')}),
        CHECK ({_timestamp_check('resolved_at', nullable=True)}),
        CHECK ({_timestamp_check('redacted_at', nullable=True)})
    )
    """,
]

_TICKET_INDEXES: list[str] = [
    "CREATE INDEX idx_support_tickets_customer_id ON support_tickets (customer_id)",
    "CREATE INDEX idx_support_tickets_order_id ON support_tickets (order_id)",
    "CREATE INDEX idx_support_tickets_customer_created ON support_tickets (customer_id, created_at)",
    (
        "CREATE UNIQUE INDEX uq_active_ticket_per_issue "
        "ON support_tickets (customer_id, issue_type, issue_key) "
        f"WHERE status NOT IN {_TERMINAL_STATUSES}"
    ),
    "CREATE INDEX idx_ticket_events_ticket ON ticket_events (ticket_id, seq)",
    "CREATE INDEX idx_ticket_events_status ON ticket_events (ticket_id, kind, to_status, created_at)",
    "CREATE INDEX idx_ticket_events_staff_review ON ticket_events (ticket_id) WHERE staff_review_required = 1",
    (
        "CREATE INDEX idx_ticket_write_intents_unresolved ON ticket_write_intents (customer_id, created_at) "
        "WHERE state = 'prepared' AND last_outcome IS NULL"
    ),
]

_TICKET_TRIGGERS: list[str] = [
    # The original text can never be overwritten except through the audited erasure shape (design 2.1a).
    """
    CREATE TRIGGER trg_support_tickets_text_immutable
    BEFORE UPDATE OF description, conversation_summary ON support_tickets
    WHEN NOT (OLD.redacted_at IS NULL AND NEW.redacted_at IS NOT NULL
              AND NEW.description = '[erased]' AND NEW.conversation_summary IS NULL)
    BEGIN SELECT RAISE(ABORT, 'ticket text is immutable except through the audited erasure process'); END
    """,
    """
    CREATE TRIGGER trg_ticket_events_no_update BEFORE UPDATE ON ticket_events
    WHEN NOT (OLD.redacted_at IS NULL AND NEW.redacted_at IS NOT NULL AND NEW.body IS NULL
              AND NEW.event_id = OLD.event_id AND NEW.ticket_id = OLD.ticket_id
              AND NEW.customer_id = OLD.customer_id AND NEW.seq = OLD.seq AND NEW.kind = OLD.kind
              AND NEW.actor_type = OLD.actor_type AND NEW.actor_ref = OLD.actor_ref
              AND NEW.intent IS OLD.intent AND NEW.staff_review_required = OLD.staff_review_required
              AND NEW.from_status IS OLD.from_status AND NEW.to_status IS OLD.to_status
              AND NEW.transition_reason IS OLD.transition_reason
              AND NEW.trigger_event_id IS OLD.trigger_event_id
              AND NEW.request_key = OLD.request_key AND NEW.request_hash = OLD.request_hash
              AND NEW.created_at = OLD.created_at)
    BEGIN SELECT RAISE(ABORT, 'ticket_events is append-only'); END
    """,
    """
    CREATE TRIGGER trg_ticket_events_no_delete BEFORE DELETE ON ticket_events
    BEGIN SELECT RAISE(ABORT, 'ticket_events rows are never deleted'); END
    """,
    """
    CREATE TRIGGER trg_ticket_erasure_log_no_update BEFORE UPDATE ON ticket_erasure_log
    BEGIN SELECT RAISE(ABORT, 'ticket_erasure_log is append-only'); END
    """,
    """
    CREATE TRIGGER trg_ticket_erasure_log_no_delete BEFORE DELETE ON ticket_erasure_log
    BEGIN SELECT RAISE(ABORT, 'ticket_erasure_log is append-only'); END
    """,
    # The request key and the immutable payload of an intent never change; only the erasure shape and the state fields do.
    """
    CREATE TRIGGER trg_ticket_write_intents_payload_immutable
    BEFORE UPDATE OF customer_id, request_key, payload_json, request_hash, created_at ON ticket_write_intents
    WHEN NOT (OLD.redacted_at IS NULL AND NEW.redacted_at IS NOT NULL AND NEW.payload_json IS NULL
              AND NEW.customer_id = OLD.customer_id AND NEW.request_key = OLD.request_key
              AND NEW.request_hash = OLD.request_hash AND NEW.created_at = OLD.created_at)
    BEGIN SELECT RAISE(ABORT, 'a ticket write intent payload is immutable except through the audited erasure process'); END
    """,
    """
    CREATE TRIGGER trg_ticket_write_intents_no_delete BEFORE DELETE ON ticket_write_intents
    BEGIN SELECT RAISE(ABORT, 'ticket write intents are never deleted'); END
    """,
]


def ticket_rebuild_statements() -> dict[str, list[str]]:
    """The statements a migration needs, from the same text ``create_schema`` uses (parity by construction)."""
    return {
        "support_tickets": [support_tickets_ddl("support_tickets")],
        "side_tables": list(_TICKET_SIDE_TABLES),
        "indexes": list(_TICKET_INDEXES),
        "triggers": list(_TICKET_TRIGGERS),
    }


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

]

_CREATE_TABLES += [support_tickets_ddl("support_tickets")] + _TICKET_SIDE_TABLES

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
]
_CREATE_INDEXES += _TICKET_INDEXES


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the eight tables, indexes, triggers and stamp ``user_version = SCHEMA_VERSION``.

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
        if state == "outdated":
            raise SchemaIncompatibleError(
                "older schema version; it is preserved unchanged (use the operator-run migration)", "outdated"
            )
        if state == "future":
            raise SchemaIncompatibleError("unsupported newer schema version", "future")
        if state == "current":
            raise ConnectionError_("the database is already initialized")
        for statement in _CREATE_TABLES:
            connection.execute(statement)
        for statement in _CREATE_INDEXES:
            connection.execute(statement)
        for statement in _TICKET_TRIGGERS:
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
