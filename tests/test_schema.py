"""The D2 schema: structure, constraints, indexes, versioning (docs/01 C, H, I).

SQLite ``CHECK`` constraints are a backstop. Python validation is
authoritative, and a few tests below assert that division honestly.
"""

from __future__ import annotations

import sqlite3

import pytest

import app.db.schema as schema_module
from app.db.connection import get_initialization_connection
from app.db.errors import (
    ConnectionError_,
    DatabaseOperationalError,
    SchemaIncompatibleError,
)
from app.db.schema import create_schema

GOOD_TS = "2026-11-07T00:00:00Z"
LATER_TS = "2026-11-08T00:00:00Z"


@pytest.fixture
def db():
    connection = get_initialization_connection(":memory:")
    create_schema(connection)
    yield connection
    connection.close()


def customer(db, cid="C1", email=None):
    db.execute(
        "INSERT INTO customers (customer_id, name, email) VALUES (?, 'N', ?)",
        (cid, email or f"{cid}@example.com"),
    )


_DEFAULT = object()


def order(db, oid="O1", cid="C1", status="Delivered", order_date=GOOD_TS,
          delivered_at=_DEFAULT, **extra):
    if delivered_at is _DEFAULT:
        delivered_at = LATER_TS if status in ("Delivered", "Returned") else None
    columns = {
        "order_id": oid, "customer_id": cid, "status": status,
        "order_date": order_date, "delivered_at": delivered_at,
        "total_amount": 100,
    }
    columns.update(extra)
    keys = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    db.execute(f"INSERT INTO orders ({keys}) VALUES ({marks})", tuple(columns.values()))


def item(db, iid="I1", oid="O1"):
    db.execute(
        "INSERT INTO order_items (item_id, order_id, product_id, quantity, price) "
        "VALUES (?, ?, 'P1001', 1, 100)", (iid, oid),
    )


def base(db):
    customer(db)
    order(db)
    item(db)


def ret(db, rid="R1", reason="reason", category="standard", status="Requested",
        requested_at=GOOD_TS, iid="I1", oid="O1", cid="C1"):
    db.execute(
        "INSERT INTO return_requests (return_id, customer_id, order_id, item_id, "
        "reason, reason_category, status, requested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, cid, oid, iid, reason, category, status, requested_at),
    )


def ticket(db, tid="T1", issue="order", desc="d", summary="s", status="Open",
           priority="Normal", created_at=GOOD_TS, oid=None, cid="C1"):
    db.execute(
        "INSERT INTO support_tickets (ticket_id, customer_id, order_id, issue_type, "
        "description, conversation_summary, status, priority, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, cid, oid, issue, desc, summary, status, priority, created_at),
    )


# ---------------------------------------------------------------------------
# Creation, versioning and guards
# ---------------------------------------------------------------------------

class TestCreation:
    def test_creates_five_tables_and_indexes(self, db):
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert names == {"customers", "orders", "order_items",
                         "return_requests", "support_tickets"}
        indexes = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")}
        assert "uq_active_return_per_item" in indexes

    def test_stamps_user_version_one(self, db):
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1

    def test_return_requests_has_reason_category_column(self, db):
        columns = {r["name"]: r for r in db.execute("PRAGMA table_info(return_requests)")}
        assert columns["reason_category"]["notnull"] == 1
        assert columns["reason"]["notnull"] == 1

    def test_ticket_summary_is_required(self, db):
        columns = {r["name"]: r for r in db.execute("PRAGMA table_info(support_tickets)")}
        assert columns["conversation_summary"]["notnull"] == 1

    def test_foreign_key_guard(self):
        raw = sqlite3.connect(":memory:")
        try:
            with pytest.raises(ConnectionError_):
                create_schema(raw)
        finally:
            raw.close()

    def test_refuses_to_run_inside_a_transaction(self):
        connection = get_initialization_connection(":memory:")
        try:
            connection.execute("BEGIN")
            with pytest.raises(ConnectionError_):
                create_schema(connection)
        finally:
            connection.close()

    def test_second_run_refused(self, db):
        with pytest.raises(ConnectionError_):
            create_schema(db)

    def test_failure_rolls_back_everything_including_version(self, monkeypatch):
        connection = get_initialization_connection(":memory:")
        monkeypatch.setattr(schema_module, "_CREATE_INDEXES", ["CREATE INDEX bad ON missing (x)"])
        try:
            with pytest.raises(DatabaseOperationalError):
                create_schema(connection)
            assert connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        finally:
            connection.close()

    def test_legacy_schema_rejected_unchanged(self):
        connection = get_initialization_connection(":memory:")
        connection.execute("CREATE TABLE legacy (x)")
        connection.commit()
        try:
            with pytest.raises(SchemaIncompatibleError) as info:
                create_schema(connection)
            assert info.value.reason == "outdated"
            assert connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 1
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        finally:
            connection.close()

    def test_future_version_rejected(self):
        connection = get_initialization_connection(":memory:")
        connection.execute("PRAGMA user_version = 4")
        try:
            with pytest.raises(SchemaIncompatibleError) as info:
                create_schema(connection)
            assert info.value.reason == "future"
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# Orders, items, customers
# ---------------------------------------------------------------------------

class TestCustomersAndOrders:
    def test_email_unique(self, db):
        customer(db, "C1", "a@x"); customer(db, "C2", "b@x")
        with pytest.raises(sqlite3.IntegrityError):
            customer(db, "C3", "a@x")

    @pytest.mark.parametrize("status", ["Processing", "Packed", "Shipped",
                                        "Out for Delivery", "Cancelled"])
    def test_non_delivered_statuses_need_null_delivered_at(self, db, status):
        customer(db)
        order(db, status=status, delivered_at=None)
        with pytest.raises(sqlite3.IntegrityError):
            order(db, "O2", status=status, delivered_at=LATER_TS)

    @pytest.mark.parametrize("status", ["Delivered", "Returned"])
    def test_delivered_and_returned_need_delivered_at(self, db, status):
        customer(db)
        order(db, status=status, delivered_at=LATER_TS)
        with pytest.raises(sqlite3.IntegrityError):
            order(db, "O2", status=status, delivered_at=None)

    def test_delivered_at_not_before_order_date(self, db):
        customer(db)
        order(db, "O1", order_date=LATER_TS, delivered_at=LATER_TS)  # equal is fine
        with pytest.raises(sqlite3.IntegrityError):
            order(db, "O2", order_date=LATER_TS, delivered_at=GOOD_TS)

    def test_invalid_status_payment_refund_total(self, db):
        customer(db)
        for column, value in [("status", "Lost"), ("payment_status", "Paid!"),
                              ("refund_status", "Maybe"), ("total_amount", 0)]:
            with pytest.raises(sqlite3.IntegrityError):
                order(db, f"X{column}", **{column: value})

    def test_refund_consistency(self, db):
        customer(db)
        with pytest.raises(sqlite3.IntegrityError):
            order(db, "O1", refund_status="Processed", refund_amount=None)
        with pytest.raises(sqlite3.IntegrityError):
            order(db, "O2", refund_status="Not Applicable", refund_amount=5)
        order(db, "O3", refund_status="Processed", refund_amount=0)

    BAD_TIMESTAMPS = [
        "2026-11-07T00:00:00",            # no Z
        "2026-11-07T00:00:00+00:00",      # offset
        "2026-11-07T00:00:00.5Z",         # fractional
        "2026-11-07 00:00:00Z",           # space
        "2026-11-07T00:00:00z",           # lowercase z
        "2026-11-07T00:00Z",              # short
        "x" * 20,                         # right length, wrong shape
        "",                               # empty
        "2026-11-07T00:00:00Z\x00",       # embedded NUL, 21 chars
        20261107000000,                   # integer
    ]

    @pytest.mark.parametrize("bad", BAD_TIMESTAMPS)
    def test_order_date_shape_enforced(self, db, bad):
        customer(db)
        with pytest.raises(sqlite3.IntegrityError):
            order(db, order_date=bad, status="Processing", delivered_at=None)

    @pytest.mark.parametrize("bad", BAD_TIMESTAMPS[:-1])
    def test_delivered_at_shape_enforced(self, db, bad):
        customer(db)
        with pytest.raises(sqlite3.IntegrityError):
            order(db, status="Delivered", delivered_at=bad)

    def test_glob_is_shape_only_not_calendar(self, db):
        """Backstop honesty: an impossible date passes the SQL shape check.

        Python (``clock.parse_timestamp``) is what rejects it.
        """
        customer(db)
        order(db, status="Processing", order_date="2026-13-45T99:99:99Z", delivered_at=None)

    def test_item_constraints(self, db):
        customer(db); order(db)
        item(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO order_items VALUES ('I2', 'O1', 'P', 0, 5)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO order_items VALUES ('I3', 'O1', 'P', 1, 0)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO order_items VALUES ('I4', 'NOPE', 'P', 1, 5)")

    def test_foreign_keys_enforced_on_factory_connection(self, db):
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            order(db, cid="MISSING")


# ---------------------------------------------------------------------------
# Return requests
# ---------------------------------------------------------------------------

class TestReturnRequests:
    def test_valid_row_for_each_category(self, db):
        base(db)
        for n, category in enumerate(["damaged", "wrong_product", "standard"]):
            item(db, f"J{n}")
            ret(db, f"R{n}", category=category, iid=f"J{n}")

    @pytest.mark.parametrize("bad", ["Damaged", "other", "", None])
    def test_invalid_category_rejected(self, db, bad):
        base(db)
        with pytest.raises(sqlite3.IntegrityError):
            ret(db, category=bad)

    def test_reason_exactly_1000_accepted_1001_rejected(self, db):
        base(db)
        ret(db, reason="a" * 1000)
        item(db, "I2")
        with pytest.raises(sqlite3.IntegrityError):
            ret(db, "R2", reason="a" * 1001, iid="I2")

    @pytest.mark.parametrize("bad", ["", " ", "\t\n\r\x0b\x0c ", "a\x00b", "\x00", b"bytes", None])
    def test_reason_backstop_rejects(self, db, bad):
        base(db)
        with pytest.raises(sqlite3.IntegrityError):
            ret(db, reason=bad)

    def test_reason_stored_exactly_with_surrounding_whitespace(self, db):
        base(db)
        ret(db, reason="  keep me  \n")
        assert db.execute("SELECT reason FROM return_requests").fetchone()[0] == "  keep me  \n"

    def test_nbsp_only_passes_sql_backstop_python_is_authoritative(self, db):
        """SQLite's trim set is six ASCII characters only (documented)."""
        base(db)
        ret(db, reason=" ")

    def test_requested_at_shape_enforced(self, db):
        base(db)
        with pytest.raises(sqlite3.IntegrityError):
            ret(db, requested_at="2026-11-07T00:00:00+00:00")

    def test_default_requested_at_is_canonical(self, db):
        base(db)
        db.execute(
            "INSERT INTO return_requests (return_id, customer_id, order_id, item_id, "
            "reason, reason_category) VALUES ('R9', 'C1', 'O1', 'I1', 'r', 'standard')")
        stamp = db.execute("SELECT requested_at FROM return_requests").fetchone()[0]
        assert len(stamp) == 20 and stamp.endswith("Z")

    def test_status_enum(self, db):
        base(db)
        with pytest.raises(sqlite3.IntegrityError):
            ret(db, status="Maybe")

    def test_composite_fk_customer_must_own_order(self, db):
        base(db); customer(db, "C2")
        with pytest.raises(sqlite3.IntegrityError):
            ret(db, cid="C2")

    def test_composite_fk_item_must_belong_to_order(self, db):
        base(db)
        order(db, "O2"); item(db, "I2", "O2")
        with pytest.raises(sqlite3.IntegrityError):
            ret(db, iid="I2", oid="O1")

    def test_partial_unique_index_one_active_per_item(self, db):
        base(db)
        ret(db, "R1")
        with pytest.raises(sqlite3.IntegrityError) as info:
            ret(db, "R2")
        assert "return_requests.item_id" in str(info.value)

    @pytest.mark.parametrize("active", ["Requested", "Approved"])
    def test_active_statuses_block(self, db, active):
        base(db)
        ret(db, "R1", status=active)
        with pytest.raises(sqlite3.IntegrityError):
            ret(db, "R2", status="Requested")

    @pytest.mark.parametrize("released", ["Rejected", "Completed"])
    def test_released_statuses_allow_new_request(self, db, released):
        base(db)
        ret(db, "R1", status=released)
        ret(db, "R2", status="Requested")


# ---------------------------------------------------------------------------
# Support tickets
# ---------------------------------------------------------------------------

class TestSupportTickets:
    def test_general_ticket_without_order(self, db):
        customer(db)
        ticket(db)

    def test_order_link_must_be_owned(self, db):
        base(db); customer(db, "C2")
        ticket(db, "T1", oid="O1", cid="C1")
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, "T2", oid="O1", cid="C2")

    @pytest.mark.parametrize("issue", ["order", "return", "payment", "product", "general"])
    def test_issue_type_allowlist_accepts(self, db, issue):
        customer(db)
        ticket(db, issue=issue)

    @pytest.mark.parametrize("bad", ["Order", "refund_issue", "other", ""])
    def test_issue_type_allowlist_rejects(self, db, bad):
        customer(db)
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, issue=bad)

    @pytest.mark.parametrize("column", ["desc", "summary"])
    def test_text_limits_and_backstops(self, db, column):
        customer(db)
        ticket(db, "T1", **{column: "a" * 2000})
        for n, bad in enumerate(["a" * 2001, "", " \t\n", "a\x00b", b"blob", None]):
            with pytest.raises(sqlite3.IntegrityError):
                ticket(db, f"X{n}", **{column: bad})

    def test_status_priority_enums(self, db):
        customer(db)
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, status="Done")
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, priority="Critical")

    def test_created_at_shape(self, db):
        customer(db)
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, created_at="yesterday")
