"""Shared helpers for the Phase 2 database tests (scratch databases only; never the protected files)."""
from __future__ import annotations

import sqlite3

from app.db import schema as schema_module
from app.db.connection import get_connection, get_initialization_connection
from app.db.seed_data import seed_database

GOOD_TS = "2026-11-01T00:00:00Z"

# The version-1 ``support_tickets`` table exactly as it was before Phase 2 (the migration source).
V1_SUPPORT_TICKETS = """
CREATE TABLE support_tickets (
    ticket_id            TEXT NOT NULL PRIMARY KEY,
    customer_id          TEXT NOT NULL,
    order_id             TEXT,
    issue_type           TEXT NOT NULL,
    description          TEXT NOT NULL,
    conversation_summary TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'Open',
    priority             TEXT NOT NULL DEFAULT 'Normal',
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    FOREIGN KEY (order_id, customer_id) REFERENCES orders (order_id, customer_id),
    CHECK (issue_type IN ('order', 'return', 'payment', 'product', 'general')),
    CHECK (typeof(description) = 'text' AND instr(description, char(0)) = 0 AND length(description) <= 2000
           AND length(trim(description, ' ' || char(9, 10, 11, 12, 13))) > 0),
    CHECK (typeof(conversation_summary) = 'text' AND instr(conversation_summary, char(0)) = 0
           AND length(conversation_summary) <= 2000
           AND length(trim(conversation_summary, ' ' || char(9, 10, 11, 12, 13))) > 0),
    CHECK (status IN ('Open', 'In Progress', 'Resolved', 'Closed')),
    CHECK (priority IN ('Low', 'Normal', 'High', 'Urgent'))
)
"""


def build_v1_database(path: str, tickets: list[tuple] | None = None, *, seed: bool = True) -> str:
    """A faithful version-1 database: the four unchanged tables, the v1 ticket table, the v1 indexes, ``user_version`` 1.

    ``tickets`` rows are ``(ticket_id, customer_id, order_id, issue_type, description, summary, status, created_at)``.
    """
    connection = get_initialization_connection(path)
    try:
        for statement in schema_module._CREATE_TABLES[:4]:
            connection.execute(statement)
        connection.execute(V1_SUPPORT_TICKETS)
        for statement in schema_module._CREATE_INDEXES[:5]:
            connection.execute(statement)
        connection.execute("CREATE INDEX idx_support_tickets_customer_id ON support_tickets (customer_id)")
        connection.execute("CREATE INDEX idx_support_tickets_order_id ON support_tickets (order_id)")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        if seed:
            seed_database(connection)
        for row in tickets or []:
            connection.execute(
                "INSERT INTO support_tickets (ticket_id, customer_id, order_id, issue_type, description, "
                "conversation_summary, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
        connection.commit()
    finally:
        connection.close()
    return path


def an_item(order_id: str = "ORD1001", customer: str = "C101") -> str:
    """One item id of the customer's order, read from the seeded scratch database."""
    connection = get_connection()
    try:
        row = connection.execute("SELECT item_id FROM order_items WHERE order_id = ? ORDER BY item_id", (order_id,)).fetchone()
        return row[0]
    finally:
        connection.close()


def force_status(path: str, ticket_id: str, status: str, *, resolved_event_at: str | None = None) -> None:
    """Test scaffolding: put a ticket in a status by direct SQL (no staff tooling exists), optionally with the trusted
    ``Resolved`` status_change event a real staff transition would have written."""
    connection = get_connection(path)
    try:
        row = connection.execute("SELECT customer_id, status FROM support_tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        connection.execute("UPDATE support_tickets SET status = ? WHERE ticket_id = ?", (status, ticket_id))
        if resolved_event_at is not None:
            seq = connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM ticket_events WHERE ticket_id = ?",
                                     (ticket_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO ticket_events (event_id, ticket_id, customer_id, seq, kind, actor_type, actor_ref, "
                "from_status, to_status, transition_reason, request_key, request_hash, created_at) "
                "VALUES (?, ?, ?, ?, 'status_change', 'agent', 'operator:test', ?, 'Resolved', 'staff_action', ?, 'h', ?)",
                (f"EVT_{seq:032x}", ticket_id, row["customer_id"], seq, row["status"], f"staff-{ticket_id}-{seq}",
                 resolved_event_at))
        connection.commit()
    finally:
        connection.close()


class CommitProxy:
    """Wrap a sqlite3 connection so ``commit`` can be made to fail after (or instead of) really committing."""

    def __init__(self, inner: sqlite3.Connection, *, commit_first: bool) -> None:
        self._inner = inner
        self._commit_first = commit_first

    def commit(self):
        if self._commit_first:
            self._inner.commit()
        raise sqlite3.OperationalError("disk I/O error while committing (simulated)")

    def __getattr__(self, name):
        return getattr(self._inner, name)
