"""Automated tests for app/db/schema.py (Checkpoint 3).

Scope (see docs/02_database_implementation_plan.md Section 3, step 3,
and Section 7, Checkpoint 3): these tests verify the schema created by
``create_schema()`` behaves exactly as approved in
``docs/01_database_design.md`` Section C — table structure, composite
foreign keys, CHECK constraints, the partial unique index, idempotency,
the foreign-key-enforcement guard, and transaction rollback.

What is deliberately NOT tested here, per the Checkpoint 3 scope:
  - Application-level return-eligibility rules (the 7-day / 48-hour
    windows from docs/01_database_design.md Section F) — those are not
    database constraints at all; nothing in app/db/schema.py enforces
    them, and they belong to the repository-layer tests in a later
    checkpoint.
  - Seed data or repository functions — not implemented yet.

Every test obtains its connection through
``app.db.connection.get_connection()``, except the explicit raw-
connection test in the "foreign-key enforcement guard" section, where
using a connection NOT obtained that way is the entire point of the
test.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

import app.db.schema as schema_module
from app.db.connection import ConnectionError_, get_connection
from app.db.schema import create_schema


# ---------------------------------------------------------------------------
# Shared constants (the approved enum vocabularies from
# docs/01_database_design.md Section D, as actually encoded in
# app/db/schema.py's CHECK constraints). These are values tests need to
# insert and assert against -- not a duplication of the DDL itself.
# ---------------------------------------------------------------------------

ORDER_STATUSES = (
    "Processing", "Packed", "Shipped", "Out for Delivery",
    "Delivered", "Cancelled", "Returned",
)
PAYMENT_STATUSES = ("Pending", "Paid", "Failed")
REFUND_STATUSES = ("Not Applicable", "Pending", "Processed", "Failed")
RETURN_STATUSES = ("Requested", "Approved", "Rejected", "Completed")
TICKET_STATUSES = ("Open", "In Progress", "Resolved", "Closed")
TICKET_PRIORITIES = ("Low", "Normal", "High", "Urgent")

UTC_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """A fresh, isolated in-memory connection with the schema created.

    Each test gets its own private ``:memory:`` database (not a shared
    file), so tests cannot leak state into one another. The connection
    is closed in teardown even if the test raises.
    """
    connection = get_connection(":memory:")
    try:
        create_schema(connection)
        yield connection
    finally:
        connection.close()


@pytest.fixture
def seeded(conn):
    """``conn`` plus two customers, one order each, and one item.

    This is the minimum prerequisite data needed for the composite-FK
    and cross-customer ownership tests: two distinct customers (C101,
    C102), each owning one order, with one order item on C101's order.
    Mirrors the project's standing C101/C102/ORD1003 reference scenario
    used elsewhere in this project.
    """
    conn.execute(
        "INSERT INTO customers (customer_id, name, email) "
        "VALUES ('C101', 'Alice', 'alice@example.com')"
    )
    conn.execute(
        "INSERT INTO customers (customer_id, name, email) "
        "VALUES ('C102', 'Bob', 'bob@example.com')"
    )
    conn.execute(
        "INSERT INTO orders "
        "(order_id, customer_id, status, order_date, delivered_at, total_amount) "
        "VALUES ('ORD1001', 'C101', 'Delivered', "
        "'2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', 10000)"
    )
    conn.execute(
        "INSERT INTO orders (order_id, customer_id, order_date, total_amount) "
        "VALUES ('ORD1003', 'C102', '2026-01-01T00:00:00Z', 5000)"
    )
    conn.execute(
        "INSERT INTO order_items (item_id, order_id, product_id, price) "
        "VALUES ('IT1', 'ORD1001', 'P1', 5000)"
    )
    conn.commit()
    return conn


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table';"
    ).fetchall()
    return {row[0] for row in rows}


def _index_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'index' AND name NOT LIKE 'sqlite_%';"
    ).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# A. Schema creation
# ---------------------------------------------------------------------------

class TestSchemaCreation:
    def test_all_five_tables_exist(self, conn):
        expected = {
            "customers", "orders", "order_items",
            "return_requests", "support_tickets",
        }
        actual = _table_names(conn)
        assert expected <= actual, (
            f"Expected tables {expected} to all exist, but sqlite_master "
            f"only reports {actual}"
        )

    def test_expected_indexes_exist(self, conn):
        expected = {
            "idx_orders_customer_id",
            "idx_order_items_order_id",
            "idx_return_requests_customer_id",
            "idx_return_requests_order_id",
            "idx_support_tickets_customer_id",
            "idx_support_tickets_order_id",
            "uq_active_return_per_item",
        }
        actual = _index_names(conn)
        assert expected <= actual, (
            f"Expected indexes {expected} to all exist, but sqlite_master "
            f"only reports {actual}"
        )

    def test_create_schema_is_idempotent(self, conn):
        # conn already had create_schema() called once by the fixture.
        # Calling it again against the same, already-initialized
        # database must not raise.
        create_schema(conn)
        assert _table_names(conn) >= {
            "customers", "orders", "order_items",
            "return_requests", "support_tickets",
        }, "Tables must still be present after a second create_schema() call"


# ---------------------------------------------------------------------------
# B. Customer constraints
# ---------------------------------------------------------------------------

class TestCustomerConstraints:
    def test_customer_id_is_primary_key_duplicate_rejected(self, conn):
        conn.execute(
            "INSERT INTO customers (customer_id, name, email) "
            "VALUES ('C1', 'Alice', 'alice@example.com')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO customers (customer_id, name, email) "
                "VALUES ('C1', 'Someone Else', 'other@example.com')"
            )

    def test_customer_name_not_null(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO customers (customer_id, name, email) "
                "VALUES ('C1', NULL, 'alice@example.com')"
            )

    def test_customer_email_not_null(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO customers (customer_id, name, email) "
                "VALUES ('C1', 'Alice', NULL)"
            )

    def test_customer_email_unique_duplicate_rejected(self, conn):
        conn.execute(
            "INSERT INTO customers (customer_id, name, email) "
            "VALUES ('C1', 'Alice', 'dup@example.com')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO customers (customer_id, name, email) "
                "VALUES ('C2', 'Bob', 'dup@example.com')"
            )

    def test_customer_different_emails_accepted(self, conn):
        conn.execute(
            "INSERT INTO customers (customer_id, name, email) "
            "VALUES ('C1', 'Alice', 'alice@example.com')"
        )
        conn.execute(
            "INSERT INTO customers (customer_id, name, email) "
            "VALUES ('C2', 'Bob', 'bob@example.com')"
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        assert count == 2, "Two customers with different emails should both insert"


# ---------------------------------------------------------------------------
# C. Orders constraints
# ---------------------------------------------------------------------------

class TestOrderConstraints:
    @staticmethod
    def _insert_customer(conn, customer_id="C1"):
        conn.execute(
            "INSERT INTO customers (customer_id, name, email) "
            f"VALUES ('{customer_id}', 'Alice', '{customer_id}@example.com')"
        )
        conn.commit()

    @pytest.mark.parametrize("status", ORDER_STATUSES)
    def test_valid_order_statuses_accepted(self, conn, status):
        self._insert_customer(conn)
        conn.execute(
            "INSERT INTO orders (order_id, customer_id, status, order_date, total_amount) "
            "VALUES ('O1', 'C1', ?, '2026-01-01T00:00:00Z', 100)",
            (status,),
        )
        conn.commit()
        row = conn.execute("SELECT status FROM orders WHERE order_id='O1'").fetchone()
        assert row[0] == status

    def test_invalid_order_status_rejected(self, conn):
        self._insert_customer(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders (order_id, customer_id, status, order_date, total_amount) "
                "VALUES ('O1', 'C1', 'Bogus', '2026-01-01T00:00:00Z', 100)"
            )

    @pytest.mark.parametrize("payment_status", PAYMENT_STATUSES)
    def test_valid_payment_statuses_accepted(self, conn, payment_status):
        self._insert_customer(conn)
        conn.execute(
            "INSERT INTO orders (order_id, customer_id, payment_status, order_date, total_amount) "
            "VALUES ('O1', 'C1', ?, '2026-01-01T00:00:00Z', 100)",
            (payment_status,),
        )
        conn.commit()

    def test_invalid_payment_status_rejected(self, conn):
        self._insert_customer(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders (order_id, customer_id, payment_status, order_date, total_amount) "
                "VALUES ('O1', 'C1', 'Bogus', '2026-01-01T00:00:00Z', 100)"
            )

    @pytest.mark.parametrize("refund_status", REFUND_STATUSES)
    def test_valid_refund_statuses_accepted(self, conn, refund_status):
        self._insert_customer(conn)
        # refund_amount must be consistent with refund_status per the
        # CHECK constraint: NULL only for 'Not Applicable'.
        amount = None if refund_status == "Not Applicable" else 500
        conn.execute(
            "INSERT INTO orders "
            "(order_id, customer_id, refund_status, refund_amount, order_date, total_amount) "
            "VALUES ('O1', 'C1', ?, ?, '2026-01-01T00:00:00Z', 100)",
            (refund_status, amount),
        )
        conn.commit()

    def test_invalid_refund_status_rejected(self, conn):
        self._insert_customer(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders (order_id, customer_id, refund_status, order_date, total_amount) "
                "VALUES ('O1', 'C1', 'Bogus', '2026-01-01T00:00:00Z', 100)"
            )

    def test_refund_not_applicable_requires_null_amount(self, conn):
        self._insert_customer(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders "
                "(order_id, customer_id, refund_status, refund_amount, order_date, total_amount) "
                "VALUES ('O1', 'C1', 'Not Applicable', 500, '2026-01-01T00:00:00Z', 100)"
            )

    def test_refund_other_status_requires_non_null_amount(self, conn):
        self._insert_customer(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders "
                "(order_id, customer_id, refund_status, refund_amount, order_date, total_amount) "
                "VALUES ('O1', 'C1', 'Processed', NULL, '2026-01-01T00:00:00Z', 100)"
            )

    def test_refund_other_status_requires_non_negative_amount(self, conn):
        self._insert_customer(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders "
                "(order_id, customer_id, refund_status, refund_amount, order_date, total_amount) "
                "VALUES ('O1', 'C1', 'Processed', -1, '2026-01-01T00:00:00Z', 100)"
            )

    def test_refund_other_status_valid_amount_accepted(self, conn):
        self._insert_customer(conn)
        conn.execute(
            "INSERT INTO orders "
            "(order_id, customer_id, refund_status, refund_amount, order_date, total_amount) "
            "VALUES ('O1', 'C1', 'Processed', 500, '2026-01-01T00:00:00Z', 100)"
        )
        conn.commit()

    def test_negative_total_amount_rejected(self, conn):
        self._insert_customer(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders (order_id, customer_id, order_date, total_amount) "
                "VALUES ('O1', 'C1', '2026-01-01T00:00:00Z', -100)"
            )

    def test_delivered_at_rejected_when_status_not_delivered_or_returned(self, conn):
        self._insert_customer(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders "
                "(order_id, customer_id, status, delivered_at, order_date, total_amount) "
                "VALUES ('O1', 'C1', 'Processing', '2026-01-02T00:00:00Z', "
                "'2026-01-01T00:00:00Z', 100)"
            )

    @pytest.mark.parametrize("status", ["Delivered", "Returned"])
    def test_delivered_at_allowed_when_status_delivered_or_returned(self, conn, status):
        self._insert_customer(conn)
        conn.execute(
            "INSERT INTO orders "
            "(order_id, customer_id, status, delivered_at, order_date, total_amount) "
            "VALUES ('O1', 'C1', ?, '2026-01-02T00:00:00Z', "
            "'2026-01-01T00:00:00Z', 100)",
            (status,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# D. Order item constraints
# ---------------------------------------------------------------------------

class TestOrderItemConstraints:
    @staticmethod
    def _insert_customer_and_order(conn):
        conn.execute(
            "INSERT INTO customers (customer_id, name, email) "
            "VALUES ('C1', 'Alice', 'alice@example.com')"
        )
        conn.execute(
            "INSERT INTO orders (order_id, customer_id, order_date, total_amount) "
            "VALUES ('O1', 'C1', '2026-01-01T00:00:00Z', 100)"
        )
        conn.commit()

    def test_order_item_requires_existing_order(self, conn):
        self._insert_customer_and_order(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO order_items (item_id, order_id, product_id, price) "
                "VALUES ('IT1', 'NONEXISTENT_ORDER', 'P1', 100)"
            )

    def test_order_item_quantity_must_be_positive(self, conn):
        self._insert_customer_and_order(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO order_items (item_id, order_id, product_id, quantity, price) "
                "VALUES ('IT1', 'O1', 'P1', 0, 100)"
            )

    def test_order_item_price_must_be_positive(self, conn):
        self._insert_customer_and_order(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO order_items (item_id, order_id, product_id, price) "
                "VALUES ('IT1', 'O1', 'P1', 0)"
            )

    def test_valid_order_item_insert_succeeds(self, conn):
        self._insert_customer_and_order(conn)
        conn.execute(
            "INSERT INTO order_items (item_id, order_id, product_id, quantity, price) "
            "VALUES ('IT1', 'O1', 'P1', 2, 500)"
        )
        conn.commit()
        row = conn.execute("SELECT quantity, price FROM order_items WHERE item_id='IT1'").fetchone()
        assert row[0] == 2 and row[1] == 500


# ---------------------------------------------------------------------------
# E. Composite foreign keys (actual enforcement, not just declaration)
# ---------------------------------------------------------------------------

class TestCompositeForeignKeys:
    def test_return_request_cannot_link_customer_to_other_customers_order(self, seeded):
        # IT2 is inserted as a genuine item of ORD1003, so the
        # (item_id, order_id) composite FK is satisfied -- 'IT2' really
        # does belong to 'ORD1003'. The only thing still wrong is that
        # ORD1003 belongs to C102, not C101, so the
        # (order_id, customer_id) composite FK is the one, and only,
        # constraint this insert can fail. Using IT1 here instead (which
        # belongs to ORD1001) would have violated both composite FKs at
        # once, making it impossible to tell which constraint actually
        # caused the rejection.
        seeded.execute(
            "INSERT INTO order_items (item_id, order_id, product_id, price) "
            "VALUES ('IT2', 'ORD1003', 'P1', 1000)"
        )
        seeded.commit()
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO return_requests "
                "(return_id, customer_id, order_id, item_id, reason) "
                "VALUES ('R1', 'C101', 'ORD1003', 'IT2', 'damaged')"
            )

    def test_return_request_cannot_link_item_to_different_order(self, seeded):
        # Give C101 a second order with no items, then try to return IT1
        # (which belongs to ORD1001) against that second order instead.
        seeded.execute(
            "INSERT INTO orders (order_id, customer_id, order_date, total_amount) "
            "VALUES ('ORD1002', 'C101', '2026-01-01T00:00:00Z', 2000)"
        )
        seeded.commit()
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO return_requests "
                "(return_id, customer_id, order_id, item_id, reason) "
                "VALUES ('R1', 'C101', 'ORD1002', 'IT1', 'damaged')"
            )

    def test_return_request_invalid_customer_reference_rejected(self, seeded):
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO return_requests "
                "(return_id, customer_id, order_id, item_id, reason) "
                "VALUES ('R1', 'NONEXISTENT_CUSTOMER', 'ORD1001', 'IT1', 'damaged')"
            )

    def test_valid_return_request_matching_pairs_succeeds(self, seeded):
        seeded.execute(
            "INSERT INTO return_requests "
            "(return_id, customer_id, order_id, item_id, reason) "
            "VALUES ('R1', 'C101', 'ORD1001', 'IT1', 'damaged')"
        )
        seeded.commit()

    def test_support_ticket_cannot_reference_other_customers_order(self, seeded):
        # ORD1001 belongs to C101, not C102.
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO support_tickets "
                "(ticket_id, customer_id, order_id, issue_type, description) "
                "VALUES ('T1', 'C102', 'ORD1001', 'general', 'not my order')"
            )

    def test_support_ticket_without_order_id_succeeds_for_valid_customer(self, seeded):
        seeded.execute(
            "INSERT INTO support_tickets "
            "(ticket_id, customer_id, order_id, issue_type, description) "
            "VALUES ('T1', 'C101', NULL, 'general', 'how do I contact support')"
        )
        seeded.commit()
        row = seeded.execute(
            "SELECT order_id FROM support_tickets WHERE ticket_id='T1'"
        ).fetchone()
        assert row[0] is None

    def test_support_ticket_invalid_customer_reference_rejected(self, seeded):
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO support_tickets "
                "(ticket_id, customer_id, issue_type, description) "
                "VALUES ('T1', 'NONEXISTENT_CUSTOMER', 'general', 'help')"
            )

    def test_support_ticket_valid_matching_order_reference_succeeds(self, seeded):
        seeded.execute(
            "INSERT INTO support_tickets "
            "(ticket_id, customer_id, order_id, issue_type, description) "
            "VALUES ('T1', 'C101', 'ORD1001', 'order_issue', 'where is my order')"
        )
        seeded.commit()


# ---------------------------------------------------------------------------
# F. Return request constraints
# ---------------------------------------------------------------------------

class TestReturnRequestConstraints:
    @pytest.mark.parametrize("status", RETURN_STATUSES)
    def test_valid_return_statuses_accepted(self, seeded, status):
        seeded.execute(
            "INSERT INTO return_requests "
            "(return_id, customer_id, order_id, item_id, reason, status) "
            "VALUES ('R1', 'C101', 'ORD1001', 'IT1', 'damaged', ?)",
            (status,),
        )
        seeded.commit()

    def test_invalid_return_status_rejected(self, seeded):
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO return_requests "
                "(return_id, customer_id, order_id, item_id, reason, status) "
                "VALUES ('R1', 'C101', 'ORD1001', 'IT1', 'damaged', 'Bogus')"
            )

    def test_requested_at_not_null_explicit_null_rejected(self, seeded):
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO return_requests "
                "(return_id, customer_id, order_id, item_id, reason, requested_at) "
                "VALUES ('R1', 'C101', 'ORD1001', 'IT1', 'damaged', NULL)"
            )

    def test_requested_at_defaults_to_utc_timestamp_when_omitted(self, seeded):
        seeded.execute(
            "INSERT INTO return_requests (return_id, customer_id, order_id, item_id, reason) "
            "VALUES ('R1', 'C101', 'ORD1001', 'IT1', 'damaged')"
        )
        seeded.commit()
        row = seeded.execute(
            "SELECT requested_at FROM return_requests WHERE return_id='R1'"
        ).fetchone()
        assert row[0] is not None
        assert UTC_TIMESTAMP_PATTERN.match(row[0]), (
            f"requested_at {row[0]!r} does not match the approved "
            f"YYYY-MM-DDTHH:MM:SSZ format"
        )

    @pytest.mark.parametrize("active_status", ["Requested", "Approved"])
    def test_partial_unique_index_blocks_second_active_return_for_same_item(
        self, seeded, active_status
    ):
        seeded.execute(
            "INSERT INTO return_requests "
            "(return_id, customer_id, order_id, item_id, reason, status) "
            "VALUES ('R1', 'C101', 'ORD1001', 'IT1', 'damaged', ?)",
            (active_status,),
        )
        seeded.commit()
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO return_requests "
                "(return_id, customer_id, order_id, item_id, reason, status) "
                "VALUES ('R2', 'C101', 'ORD1001', 'IT1', 'damaged again', ?)",
                (active_status,),
            )

    @pytest.mark.parametrize("released_status", ["Rejected", "Completed"])
    def test_rejected_or_completed_return_releases_item_for_new_request(
        self, seeded, released_status
    ):
        seeded.execute(
            "INSERT INTO return_requests "
            "(return_id, customer_id, order_id, item_id, reason, status) "
            "VALUES ('R1', 'C101', 'ORD1001', 'IT1', 'damaged', ?)",
            (released_status,),
        )
        seeded.commit()
        # A new ACTIVE request for the same item must now be insertable,
        # since R1 is no longer Requested/Approved.
        seeded.execute(
            "INSERT INTO return_requests "
            "(return_id, customer_id, order_id, item_id, reason, status) "
            "VALUES ('R2', 'C101', 'ORD1001', 'IT1', 'damaged again', 'Requested')"
        )
        seeded.commit()


# ---------------------------------------------------------------------------
# G. Support ticket constraints
# ---------------------------------------------------------------------------

class TestSupportTicketConstraints:
    @pytest.mark.parametrize("status", TICKET_STATUSES)
    def test_valid_ticket_statuses_accepted(self, seeded, status):
        seeded.execute(
            "INSERT INTO support_tickets (ticket_id, customer_id, issue_type, description, status) "
            "VALUES ('T1', 'C101', 'general', 'help', ?)",
            (status,),
        )
        seeded.commit()

    def test_invalid_ticket_status_rejected(self, seeded):
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO support_tickets (ticket_id, customer_id, issue_type, description, status) "
                "VALUES ('T1', 'C101', 'general', 'help', 'Bogus')"
            )

    @pytest.mark.parametrize("priority", TICKET_PRIORITIES)
    def test_valid_ticket_priorities_accepted(self, seeded, priority):
        seeded.execute(
            "INSERT INTO support_tickets (ticket_id, customer_id, issue_type, description, priority) "
            "VALUES ('T1', 'C101', 'general', 'help', ?)",
            (priority,),
        )
        seeded.commit()

    def test_invalid_ticket_priority_rejected(self, seeded):
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO support_tickets (ticket_id, customer_id, issue_type, description, priority) "
                "VALUES ('T1', 'C101', 'general', 'help', 'Bogus')"
            )

    def test_created_at_not_null_explicit_null_rejected(self, seeded):
        with pytest.raises(sqlite3.IntegrityError):
            seeded.execute(
                "INSERT INTO support_tickets "
                "(ticket_id, customer_id, issue_type, description, created_at) "
                "VALUES ('T1', 'C101', 'general', 'help', NULL)"
            )

    def test_created_at_defaults_to_utc_timestamp_when_omitted(self, seeded):
        seeded.execute(
            "INSERT INTO support_tickets (ticket_id, customer_id, issue_type, description) "
            "VALUES ('T1', 'C101', 'general', 'help')"
        )
        seeded.commit()
        row = seeded.execute(
            "SELECT created_at FROM support_tickets WHERE ticket_id='T1'"
        ).fetchone()
        assert row[0] is not None
        assert UTC_TIMESTAMP_PATTERN.match(row[0]), (
            f"created_at {row[0]!r} does not match the approved "
            f"YYYY-MM-DDTHH:MM:SSZ format"
        )


# ---------------------------------------------------------------------------
# H. Foreign-key enforcement guard
# ---------------------------------------------------------------------------

class TestForeignKeyEnforcementGuard:
    def test_create_schema_raises_when_foreign_keys_not_enforced(self):
        # Deliberately NOT using get_connection() here -- that is the
        # entire point of this test. A raw connection defaults to
        # foreign_keys OFF, and create_schema() must refuse to proceed.
        raw_connection = sqlite3.connect(":memory:")
        raw_connection.row_factory = sqlite3.Row
        try:
            enforced = raw_connection.execute("PRAGMA foreign_keys;").fetchone()[0]
            assert enforced == 0, (
                "Sanity check failed: a raw sqlite3.connect() should default "
                "to foreign_keys = 0"
            )
            with pytest.raises(ConnectionError_):
                create_schema(raw_connection)
        finally:
            raw_connection.close()

    def test_guard_does_not_create_tables_before_failing(self):
        raw_connection = sqlite3.connect(":memory:")
        raw_connection.row_factory = sqlite3.Row
        try:
            with pytest.raises(ConnectionError_):
                create_schema(raw_connection)
            assert _table_names(raw_connection) == set(), (
                "No tables should exist after the foreign-key guard refuses "
                "to proceed, but found: "
                f"{_table_names(raw_connection)}"
            )
        finally:
            raw_connection.close()


# ---------------------------------------------------------------------------
# I. Transaction rollback
# ---------------------------------------------------------------------------

class TestTransactionRollback:
    def test_ddl_failure_rolls_back_with_no_partial_schema(self, monkeypatch):
        # Replace the order_items CREATE TABLE statement (the third
        # table in dependency order, after customers and orders) with
        # deliberately broken SQL, so that by the time it fails,
        # customers and orders have already been created within the
        # same transaction. If rollback works, none of the three
        # should persist afterward.
        original_tables = schema_module._CREATE_TABLES
        broken_tables = (
            original_tables[:2]
            + ["CREATE TABLE BROKEN SYNTAX (((;"]
            + original_tables[3:]
        )
        monkeypatch.setattr(schema_module, "_CREATE_TABLES", broken_tables)

        connection = get_connection(":memory:")
        try:
            with pytest.raises(sqlite3.Error):
                create_schema(connection)
            assert _table_names(connection) == set(), (
                "Expected no tables to persist after a mid-schema DDL "
                "failure and rollback, but found: "
                f"{_table_names(connection)}"
            )
        finally:
            connection.close()
        # monkeypatch automatically restores schema_module._CREATE_TABLES
        # to `original_tables` at the end of this test, even if an
        # assertion above failed.
