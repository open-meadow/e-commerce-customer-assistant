"""Authorization tests for the read-only repository (Checkpoint 6).

These tests verify the customer-data-isolation guarantees of
``app/db/repository.py`` (docs/01_database_design.md Section E and
Section K tests 1, 2, 2b, 3, 4, 6; docs/02_database_implementation_plan.md
Section 6 and Section 7, Checkpoint 6). They are the gate AGENTS.md names
as required to pass before any orchestrator or UI work begins.

How the isolation works
-----------------------
Repository functions open their own connections by calling
``app.db.repository.get_connection()`` with no arguments, which would hit
the real ``data/app.db``. Every test therefore uses the ``temp_db``
fixture, which:

  1. builds a private SQLite file under pytest's ``tmp_path``,
  2. creates the schema with the real ``create_schema``,
  3. seeds it with the real ``seed_database`` (fixed baseline reference
     time, so nothing depends on today's date),
  4. monkeypatches ``app.db.repository.get_connection`` so every
     repository call opens that temporary file instead, and records each
     connection it hands out so tests can check they get closed.

A module-level guard additionally fails the run if the real
``data/app.db`` is created or changed while these tests execute.

Scope and deliberate non-goals
------------------------------
* Repository-level behaviour only. Test 2b is verified here as "identical
  return values at the repository layer"; whether the eventual
  user-facing chat message is identical for an unowned vs. a nonexistent
  order is deferred to the orchestration phase (docs/02 Section 6, row 2b).
* Expected ownership comes from a hard-coded map of the actual seed data
  (below), not from re-querying the database with repository-like SQL, so
  these tests cannot pass merely because they repeat the code under test.
* Result ordering is not asserted: the design documents do not specify it.
* Return requests and support tickets are not created by the seed, and no
  write functions exist yet, so tests insert those rows directly with
  plain parameterized INSERTs (test fixtures only).
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Callable

import pytest

import app.db.repository as repository
from app.db.connection import DEFAULT_DB_PATH, get_connection
from app.db.repository import (
    get_customer_returns,
    get_customer_tickets,
    get_order,
    get_order_items,
    list_customer_orders,
)
from app.db.schema import create_schema
from app.db.seed_data import seed_database


# ---------------------------------------------------------------------------
# Expected ownership, taken from the actual seed (app/db/seed_data.py).
# AGENTS.md mandates at least: C101 -> ORD1001 (Delivered) and ORD1002;
# C102 -> ORD1003. The remainder is the seed as it exists today.
# ---------------------------------------------------------------------------

EXPECTED_ORDERS_BY_CUSTOMER: dict[str, list[str]] = {
    "C101": ["ORD1001", "ORD1002", "ORD1014"],
    "C102": ["ORD1003", "ORD1004", "ORD1015"],
    "C103": ["ORD1005", "ORD1006"],
    "C104": ["ORD1007", "ORD1008", "ORD1009"],
    "C105": ["ORD1010", "ORD1011"],
    "C106": ["ORD1012", "ORD1013"],
}

EXPECTED_ITEMS_BY_ORDER: dict[str, list[str]] = {
    "ORD1001": ["IT101"],
    "ORD1002": ["IT102"],
    "ORD1003": ["IT103"],
    "ORD1004": ["IT104"],
    "ORD1005": ["IT105", "IT106"],  # the one multi-item order
    "ORD1006": ["IT107"],
    "ORD1007": ["IT108"],
    "ORD1008": ["IT109"],
    "ORD1009": ["IT110"],
    "ORD1010": ["IT111"],
    "ORD1011": ["IT112"],
    "ORD1012": ["IT113"],
    "ORD1013": ["IT114"],
    "ORD1014": ["IT115"],
    "ORD1015": ["IT116"],
}

ALL_CUSTOMERS: tuple[str, ...] = tuple(EXPECTED_ORDERS_BY_CUSTOMER)
ORDER_OWNER: dict[str, str] = {
    order_id: customer_id
    for customer_id, order_ids in EXPECTED_ORDERS_BY_CUSTOMER.items()
    for order_id in order_ids
}
ALL_ORDER_IDS: tuple[str, ...] = tuple(sorted(ORDER_OWNER))

OWNED_ORDER_PAIRS: list[tuple[str, str]] = [
    (customer_id, order_id)
    for customer_id, order_ids in EXPECTED_ORDERS_BY_CUSTOMER.items()
    for order_id in order_ids
]
UNOWNED_ORDER_PAIRS: list[tuple[str, str]] = [
    (customer_id, order_id)
    for customer_id in ALL_CUSTOMERS
    for order_id in ALL_ORDER_IDS
    if ORDER_OWNER[order_id] != customer_id
]

NONEXISTENT_ORDER_ID = "ORD9999"
NONEXISTENT_CUSTOMER_ID = "C999"

# Documented return shapes (see the docstrings in app/db/repository.py).
ORDER_KEYS = {
    "order_id", "customer_id", "status", "payment_status", "refund_status",
    "refund_amount", "order_date", "delivered_at", "total_amount",
}
ORDER_ITEM_KEYS = {"item_id", "order_id", "product_id", "quantity", "price"}
RETURN_KEYS = {
    "return_id", "customer_id", "order_id", "item_id", "reason", "status",
    "requested_at",
}
TICKET_KEYS = {
    "ticket_id", "customer_id", "order_id", "issue_type", "description",
    "conversation_summary", "status", "priority", "created_at",
}

# ---------------------------------------------------------------------------
# Rows the seed does not create. Every (order, customer) and (item, order)
# pairing below is valid under the schema's composite foreign keys.
# Timestamps are literals so nothing depends on the current date.
# ---------------------------------------------------------------------------

RETURN_ROWS: list[dict[str, str]] = [
    {"return_id": "RET-101-A", "customer_id": "C101", "order_id": "ORD1001",
     "item_id": "IT101", "reason": "damaged", "status": "Requested",
     "requested_at": "2026-11-06T09:00:00Z"},
    {"return_id": "RET-102-A", "customer_id": "C102", "order_id": "ORD1003",
     "item_id": "IT103", "reason": "wrong item", "status": "Approved",
     "requested_at": "2026-11-06T10:00:00Z"},
    {"return_id": "RET-103-A", "customer_id": "C103", "order_id": "ORD1005",
     "item_id": "IT105", "reason": "defective", "status": "Requested",
     "requested_at": "2026-11-06T11:00:00Z"},
    {"return_id": "RET-103-B", "customer_id": "C103", "order_id": "ORD1005",
     "item_id": "IT106", "reason": "changed mind", "status": "Rejected",
     "requested_at": "2026-11-05T11:00:00Z"},
]

TICKET_ROWS: list[dict[str, Any]] = [
    {"ticket_id": "TKT-101-ORDER", "customer_id": "C101", "order_id": "ORD1001",
     "issue_type": "delivery", "description": "where is my order",
     "created_at": "2026-11-06T09:00:00Z"},
    {"ticket_id": "TKT-101-GENERAL", "customer_id": "C101", "order_id": None,
     "issue_type": "general", "description": "how do I contact support",
     "created_at": "2026-11-06T09:30:00Z"},
    {"ticket_id": "TKT-102-GENERAL", "customer_id": "C102", "order_id": None,
     "issue_type": "general", "description": "general inquiry",
     "created_at": "2026-11-06T10:00:00Z"},
    {"ticket_id": "TKT-102-ORDER", "customer_id": "C102", "order_id": "ORD1003",
     "issue_type": "refund", "description": "refund status",
     "created_at": "2026-11-06T10:30:00Z"},
    {"ticket_id": "TKT-103-ORDER", "customer_id": "C103", "order_id": "ORD1005",
     "issue_type": "return", "description": "return question",
     "created_at": "2026-11-06T11:00:00Z"},
]


def _expected_ids_by_customer(
    rows: list[dict[str, Any]], id_field: str
) -> dict[str, set[str]]:
    """Group the ids of *rows* by owning customer (every customer present)."""
    return {
        customer_id: {
            row[id_field] for row in rows if row["customer_id"] == customer_id
        }
        for customer_id in ALL_CUSTOMERS
    }


EXPECTED_RETURN_IDS = _expected_ids_by_customer(RETURN_ROWS, "return_id")
EXPECTED_TICKET_IDS = _expected_ids_by_customer(TICKET_ROWS, "ticket_id")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class TempDatabase:
    """Handle to the isolated temporary database used by one test."""

    def __init__(self, path: str) -> None:
        self.path = path
        # Every connection the *repository* opened through the patched
        # factory, in call order.
        self.repository_connections: list[sqlite3.Connection] = []

    def open(self) -> sqlite3.Connection:
        """Open a configured connection for test setup/inspection.

        Goes through the real ``get_connection`` (never ``sqlite3.connect``
        directly) so foreign keys are enforced, as AGENTS.md requires.
        """
        return get_connection(self.path)

    def add_return_request(self, row: dict[str, str]) -> None:
        connection = self.open()
        try:
            connection.execute(
                "INSERT INTO return_requests "
                "(return_id, customer_id, order_id, item_id, reason, status, "
                "requested_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["return_id"], row["customer_id"], row["order_id"],
                 row["item_id"], row["reason"], row["status"],
                 row["requested_at"]),
            )
            connection.commit()
        finally:
            connection.close()

    def add_ticket(self, row: dict[str, Any]) -> None:
        connection = self.open()
        try:
            connection.execute(
                "INSERT INTO support_tickets "
                "(ticket_id, customer_id, order_id, issue_type, description, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (row["ticket_id"], row["customer_id"], row["order_id"],
                 row["issue_type"], row["description"], row["created_at"]),
            )
            connection.commit()
        finally:
            connection.close()

    def count_rows(self, table: str) -> int:
        """Row count of one of the five known tables (test inspection only)."""
        assert table in {
            "customers", "orders", "order_items", "return_requests",
            "support_tickets",
        }, f"unexpected table name {table!r}"
        connection = self.open()
        try:
            return connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # table validated above
            ).fetchone()[0]
        finally:
            connection.close()


@pytest.fixture(scope="module", autouse=True)
def _real_database_untouched():
    """Fail if these tests create or modify the real ``data/app.db``.

    Only ``os.stat`` is used; the real database is never opened or read.
    ``DEFAULT_DB_PATH`` is resolved relative to the working directory,
    exactly as the un-patched ``get_connection()`` would resolve it.
    """
    real_path = os.path.abspath(DEFAULT_DB_PATH)

    def fingerprint() -> tuple[int, int] | None:
        try:
            stat = os.stat(real_path)
        except FileNotFoundError:
            return None
        return (stat.st_size, stat.st_mtime_ns)

    before = fingerprint()
    yield
    after = fingerprint()
    assert after == before, (
        f"The real database at {real_path} changed while the authorization "
        f"tests ran (before={before}, after={after}). A repository call "
        f"escaped the temporary-database patch."
    )


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Isolated, seeded temporary database with the repository patched to use it."""
    db = TempDatabase(str(tmp_path / "authorization_test.db"))
    assert os.path.abspath(db.path) != os.path.abspath(DEFAULT_DB_PATH), (
        "Temporary database path must never be the real database path"
    )

    setup_connection = get_connection(db.path)
    try:
        create_schema(setup_connection)
        seed_database(setup_connection)
    finally:
        setup_connection.close()

    def patched_get_connection() -> sqlite3.Connection:
        connection = get_connection(db.path)
        db.repository_connections.append(connection)
        return connection

    monkeypatch.setattr(repository, "get_connection", patched_get_connection)
    try:
        yield db
    finally:
        # Safety net so temp files are never held open (matters on Windows).
        # Closing an already-closed connection is a harmless no-op.
        for connection in db.repository_connections:
            connection.close()


@pytest.fixture
def db_with_returns(temp_db):
    """``temp_db`` plus return requests belonging to several customers."""
    for row in RETURN_ROWS:
        temp_db.add_return_request(row)
    return temp_db


@pytest.fixture
def db_with_tickets(temp_db):
    """``temp_db`` plus support tickets belonging to several customers."""
    for row in TICKET_ROWS:
        temp_db.add_ticket(row)
    return temp_db


def _ids(rows: list[dict[str, Any]], field: str) -> list[str]:
    return [row[field] for row in rows]


# ---------------------------------------------------------------------------
# Harness canaries: prove the isolation setup itself is working
# ---------------------------------------------------------------------------

class TestIsolationHarness:
    def test_required_reference_scenario_is_seeded(self, temp_db):
        """AGENTS.md: C101->ORD1001 (Delivered) and ORD1002; C102->ORD1003."""
        ord1001 = get_order("C101", "ORD1001")
        assert ord1001 is not None and ord1001["status"] == "Delivered"
        assert get_order("C101", "ORD1002") is not None
        assert get_order("C102", "ORD1003") is not None

    def test_repository_reads_go_through_the_patched_factory(self, temp_db):
        list_customer_orders("C101")
        get_order("C101", "ORD1001")
        get_order_items("C101", "ORD1001")
        get_customer_returns("C101")
        get_customer_tickets("C101")
        assert len(temp_db.repository_connections) == 5, (
            "Every repository function must open its connection through "
            "app.db.repository.get_connection, so the patch redirects all "
            "five away from the real data/app.db"
        )

    def test_every_repository_call_closes_its_connection(self, temp_db):
        list_customer_orders("C101")
        get_order("C101", "ORD1001")
        get_order_items("C101", "ORD1001")
        get_customer_returns("C101")
        get_customer_tickets("C101")
        assert temp_db.repository_connections, "no connections were tracked"
        for connection in temp_db.repository_connections:
            with pytest.raises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_not_found_result_still_closes_its_connection(self, temp_db):
        # A "not found" result must still release its connection.
        assert get_order("C101", NONEXISTENT_ORDER_ID) is None
        (connection,) = temp_db.repository_connections
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


# ---------------------------------------------------------------------------
# Test 1 / own-data access: a customer can see their own data
# ---------------------------------------------------------------------------

class TestOwnOrderAccess:
    def test_valid_order_lookup(self, temp_db):
        order = get_order("C101", "ORD1001")
        assert order is not None, "C101 must be able to read their own ORD1001"
        assert order["order_id"] == "ORD1001"
        assert order["customer_id"] == "C101"
        assert set(order) == ORDER_KEYS

    @pytest.mark.parametrize("customer_id", ALL_CUSTOMERS)
    def test_customer_lists_exactly_their_own_orders(self, temp_db, customer_id):
        orders = list_customer_orders(customer_id)
        assert sorted(_ids(orders, "order_id")) == sorted(
            EXPECTED_ORDERS_BY_CUSTOMER[customer_id]
        )
        assert all(order["customer_id"] == customer_id for order in orders), (
            f"A row not owned by {customer_id} was returned"
        )
        assert all(set(order) == ORDER_KEYS for order in orders)

    @pytest.mark.parametrize(
        ("customer_id", "order_id"),
        OWNED_ORDER_PAIRS,
        ids=[f"{c}-{o}" for c, o in OWNED_ORDER_PAIRS],
    )
    def test_owner_can_get_each_of_their_orders(
        self, temp_db, customer_id, order_id
    ):
        order = get_order(customer_id, order_id)
        assert order is not None
        assert order["order_id"] == order_id
        assert order["customer_id"] == customer_id

    @pytest.mark.parametrize(
        ("customer_id", "order_id"),
        OWNED_ORDER_PAIRS,
        ids=[f"{c}-{o}" for c, o in OWNED_ORDER_PAIRS],
    )
    def test_owner_can_get_items_of_each_of_their_orders(
        self, temp_db, customer_id, order_id
    ):
        items = get_order_items(customer_id, order_id)
        assert sorted(_ids(items, "item_id")) == sorted(
            EXPECTED_ITEMS_BY_ORDER[order_id]
        )
        assert all(item["order_id"] == order_id for item in items)
        assert all(set(item) == ORDER_ITEM_KEYS for item in items)

    def test_customer_with_no_data_gets_empty_results_not_an_error(self, temp_db):
        assert list_customer_orders(NONEXISTENT_CUSTOMER_ID) == []
        assert get_customer_returns(NONEXISTENT_CUSTOMER_ID) == []
        assert get_customer_tickets(NONEXISTENT_CUSTOMER_ID) == []


# ---------------------------------------------------------------------------
# Tests 2, 2b, 3 and 5: cross-customer order / order-item denial
# ---------------------------------------------------------------------------

class TestCrossCustomerOrderDenial:
    def test_cross_customer_order_denied(self, temp_db):
        # Premise: ORD1003 really exists and C102 really can read it.
        assert get_order("C102", "ORD1003") is not None
        # C101 asking for C102's order gets nothing.
        assert get_order("C101", "ORD1003") is None, (
            "SECURITY: C101 was able to read C102's order ORD1003"
        )
        # And the reverse direction.
        assert get_order("C102", "ORD1001") is None, (
            "SECURITY: C102 was able to read C101's order ORD1001"
        )

    def test_not_found_equivalence(self, temp_db):
        """Unowned and nonexistent orders are indistinguishable at the
        repository layer. (Application-layer wording is out of scope here.)
        """
        # Premise: the "unowned" order genuinely exists, so equivalence is
        # not vacuous.
        assert get_order("C102", "ORD1003") is not None
        assert get_order_items("C102", "ORD1003") != []

        unowned_order = get_order("C101", "ORD1003")
        nonexistent_order = get_order("C101", NONEXISTENT_ORDER_ID)
        assert unowned_order is None
        assert nonexistent_order is None
        assert type(unowned_order) is type(nonexistent_order)

        unowned_items = get_order_items("C101", "ORD1003")
        nonexistent_items = get_order_items("C101", NONEXISTENT_ORDER_ID)
        assert unowned_items == []
        assert nonexistent_items == []
        assert type(unowned_items) is type(nonexistent_items) is list

    def test_cross_customer_items_denied(self, temp_db):
        # Premise: ORD1003 has items and its owner can read them.
        assert _ids(get_order_items("C102", "ORD1003"), "item_id") == ["IT103"]
        assert get_order_items("C101", "ORD1003") == [], (
            "SECURITY: C101 was able to read the items of C102's order"
        )
        assert get_order_items("C102", "ORD1001") == [], (
            "SECURITY: C102 was able to read the items of C101's order"
        )
        # Multi-item order owned by C103 must not leak to anyone else.
        assert get_order_items("C101", "ORD1005") == []
        assert get_order_items("C102", "ORD1005") == []

    def test_c101_and_c102_order_listings_never_overlap(self, temp_db):
        c101_ids = set(_ids(list_customer_orders("C101"), "order_id"))
        c102_ids = set(_ids(list_customer_orders("C102"), "order_id"))
        assert c101_ids, "premise: C101 has orders"
        assert c102_ids, "premise: C102 has orders"
        assert c101_ids.isdisjoint(c102_ids), (
            f"Order IDs appear in both listings: {c101_ids & c102_ids}"
        )
        assert c101_ids.isdisjoint(EXPECTED_ORDERS_BY_CUSTOMER["C102"])
        assert c102_ids.isdisjoint(EXPECTED_ORDERS_BY_CUSTOMER["C101"])

    def test_no_customer_listing_contains_another_customers_orders(self, temp_db):
        listings = {
            customer_id: set(_ids(list_customer_orders(customer_id), "order_id"))
            for customer_id in ALL_CUSTOMERS
        }
        for customer_id, order_ids in listings.items():
            foreign = {o for o in order_ids if ORDER_OWNER[o] != customer_id}
            assert not foreign, (
                f"SECURITY: {customer_id}'s listing contains orders owned by "
                f"others: {sorted(foreign)}"
            )
        # Together the listings cover every order exactly once.
        all_listed = [o for ids in listings.values() for o in ids]
        assert sorted(all_listed) == list(ALL_ORDER_IDS)

    def test_no_customer_can_read_any_unowned_order_or_its_items(self, temp_db):
        """Exhaustive sweep: every (customer, order they do NOT own) pair."""
        assert UNOWNED_ORDER_PAIRS, "premise: there are unowned pairs to check"
        leaks: list[str] = []
        for customer_id, order_id in UNOWNED_ORDER_PAIRS:
            if get_order(customer_id, order_id) is not None:
                leaks.append(f"get_order({customer_id!r}, {order_id!r})")
            if get_order_items(customer_id, order_id) != []:
                leaks.append(f"get_order_items({customer_id!r}, {order_id!r})")
        assert not leaks, f"SECURITY: cross-customer reads succeeded: {leaks}"


# ---------------------------------------------------------------------------
# Test 4: return-request isolation (multi-customer data)
# ---------------------------------------------------------------------------

class TestReturnRequestIsolation:
    def test_cross_customer_return_denied(self, db_with_returns):
        c101 = get_customer_returns("C101")
        c102 = get_customer_returns("C102")
        assert set(_ids(c101, "return_id")) == {"RET-101-A"}
        assert set(_ids(c102, "return_id")) == {"RET-102-A"}
        assert "RET-102-A" not in _ids(c101, "return_id"), (
            "SECURITY: C101 can see C102's return request"
        )
        assert "RET-101-A" not in _ids(c102, "return_id"), (
            "SECURITY: C102 can see C101's return request"
        )

    @pytest.mark.parametrize("customer_id", ALL_CUSTOMERS)
    def test_each_customer_sees_only_their_own_returns(
        self, db_with_returns, customer_id
    ):
        returns = get_customer_returns(customer_id)
        assert set(_ids(returns, "return_id")) == EXPECTED_RETURN_IDS[customer_id]
        assert len(returns) == len(EXPECTED_RETURN_IDS[customer_id]), (
            "duplicate rows returned"
        )
        assert all(row["customer_id"] == customer_id for row in returns)
        assert all(set(row) == RETURN_KEYS for row in returns)

    def test_returns_fixture_really_spans_multiple_customers(self, db_with_returns):
        # Guards the preserved rule from docs/02 Section 6: isolation tests
        # must run against data owned by several customers, otherwise an
        # unfiltered query could still "pass" by returning one customer's data.
        owners = {row["customer_id"] for row in RETURN_ROWS}
        assert len(owners) >= 3
        assert db_with_returns.count_rows("return_requests") == len(RETURN_ROWS)

    def test_customer_with_multiple_returns_gets_all_and_only_theirs(
        self, db_with_returns
    ):
        returns = get_customer_returns("C103")
        assert set(_ids(returns, "return_id")) == {"RET-103-A", "RET-103-B"}


# ---------------------------------------------------------------------------
# Tests 6, 7 and 8: support-ticket isolation (multi-customer data)
# ---------------------------------------------------------------------------

class TestSupportTicketIsolation:
    def test_cross_customer_ticket_denied(self, db_with_tickets):
        c101_ids = set(_ids(get_customer_tickets("C101"), "ticket_id"))
        c102_ids = set(_ids(get_customer_tickets("C102"), "ticket_id"))
        assert c101_ids == {"TKT-101-ORDER", "TKT-101-GENERAL"}
        assert c102_ids == {"TKT-102-GENERAL", "TKT-102-ORDER"}
        assert c101_ids.isdisjoint(c102_ids), (
            f"SECURITY: tickets visible to both C101 and C102: "
            f"{c101_ids & c102_ids}"
        )

    @pytest.mark.parametrize("customer_id", ALL_CUSTOMERS)
    def test_each_customer_sees_only_their_own_tickets(
        self, db_with_tickets, customer_id
    ):
        tickets = get_customer_tickets(customer_id)
        assert set(_ids(tickets, "ticket_id")) == EXPECTED_TICKET_IDS[customer_id]
        assert len(tickets) == len(EXPECTED_TICKET_IDS[customer_id]), (
            "duplicate rows returned"
        )
        assert all(row["customer_id"] == customer_id for row in tickets)
        assert all(set(row) == TICKET_KEYS for row in tickets)

    def test_tickets_fixture_really_spans_multiple_customers(self, db_with_tickets):
        owners = {row["customer_id"] for row in TICKET_ROWS}
        assert len(owners) >= 3
        assert db_with_tickets.count_rows("support_tickets") == len(TICKET_ROWS)

    def test_general_ticket_with_null_order_id_visible_to_owner_only(
        self, db_with_tickets
    ):
        general = [row for row in TICKET_ROWS if row["order_id"] is None]
        assert general, "premise: fixture contains general (NULL order_id) tickets"

        for ticket in general:
            owner = ticket["customer_id"]
            owner_view = {t["ticket_id"]: t for t in get_customer_tickets(owner)}
            assert ticket["ticket_id"] in owner_view, (
                f"{owner} cannot retrieve their own general ticket "
                f"{ticket['ticket_id']}"
            )
            assert owner_view[ticket["ticket_id"]]["order_id"] is None

            for other in ALL_CUSTOMERS:
                if other == owner:
                    continue
                other_ids = _ids(get_customer_tickets(other), "ticket_id")
                assert ticket["ticket_id"] not in other_ids, (
                    f"SECURITY: {other} can see {owner}'s general ticket "
                    f"{ticket['ticket_id']}"
                )

    def test_c102_general_ticket_not_visible_to_c101(self, db_with_tickets):
        c101_tickets = get_customer_tickets("C101")
        assert "TKT-102-GENERAL" not in _ids(c101_tickets, "ticket_id")
        c102_tickets = {t["ticket_id"]: t for t in get_customer_tickets("C102")}
        assert c102_tickets["TKT-102-GENERAL"]["order_id"] is None


# ---------------------------------------------------------------------------
# Injection attempts: identifiers must be bound as values, never as SQL
# ---------------------------------------------------------------------------

CUSTOMER_ID_PAYLOADS = [
    "C101' OR '1'='1",
    "' OR 1=1 --",
    "C101'; DROP TABLE orders; --",
    "%",
    "C10%",
    "*",
]
ORDER_ID_PAYLOADS = [
    "ORD1001' OR '1'='1",  # would match every row if concatenated into SQL
    "ORD1003' OR '1'='1",
    "' OR 1=1 --",
    "%",
    "ORD%",
]


class TestInjectionResistance:
    @pytest.mark.parametrize("payload", CUSTOMER_ID_PAYLOADS)
    def test_hostile_customer_id_matches_nothing(
        self, db_with_returns, db_with_tickets, payload
    ):
        # Both fixtures share one temp_db instance, so returns AND tickets
        # exist: an injected "match everything" would visibly leak rows.
        orders_before = db_with_returns.count_rows("orders")

        assert list_customer_orders(payload) == []
        assert get_order(payload, "ORD1001") is None
        assert get_order_items(payload, "ORD1001") == []
        assert get_customer_returns(payload) == []
        assert get_customer_tickets(payload) == []

        assert db_with_returns.count_rows("orders") == orders_before, (
            "orders table changed after a hostile customer ID"
        )

    @pytest.mark.parametrize("payload", ORDER_ID_PAYLOADS)
    def test_hostile_order_id_matches_nothing(self, temp_db, payload):
        assert get_order("C101", payload) is None
        assert get_order_items("C101", payload) == []


# ---------------------------------------------------------------------------
# Input validation: documented ValueError, raised before any database access
# ---------------------------------------------------------------------------

BLANK_IDS = ["", " ", "   ", "\t", "\n", " \t\n "]
BLANK_IDS_LABELS = ["empty", "space", "spaces", "tab", "newline", "mixed"]

CUSTOMER_ONLY_FUNCTIONS: list[Callable[[str], Any]] = [
    list_customer_orders,
    get_customer_returns,
    get_customer_tickets,
]
CUSTOMER_AND_ORDER_FUNCTIONS: list[Callable[[str, str], Any]] = [
    get_order,
    get_order_items,
]


def _fn_id(fn: Callable[..., Any]) -> str:
    return fn.__name__


class TestInputValidation:
    @pytest.mark.parametrize("blank", BLANK_IDS, ids=BLANK_IDS_LABELS)
    @pytest.mark.parametrize("fn", CUSTOMER_ONLY_FUNCTIONS, ids=_fn_id)
    def test_blank_customer_id_rejected_customer_only(self, temp_db, fn, blank):
        with pytest.raises(ValueError):
            fn(blank)
        assert temp_db.repository_connections == [], (
            "validation must reject before any database connection is opened"
        )

    @pytest.mark.parametrize("blank", BLANK_IDS, ids=BLANK_IDS_LABELS)
    @pytest.mark.parametrize("fn", CUSTOMER_AND_ORDER_FUNCTIONS, ids=_fn_id)
    def test_blank_customer_id_rejected_with_order(self, temp_db, fn, blank):
        with pytest.raises(ValueError):
            fn(blank, "ORD1001")
        assert temp_db.repository_connections == []

    @pytest.mark.parametrize("blank", BLANK_IDS, ids=BLANK_IDS_LABELS)
    @pytest.mark.parametrize("fn", CUSTOMER_AND_ORDER_FUNCTIONS, ids=_fn_id)
    def test_blank_order_id_rejected(self, temp_db, fn, blank):
        with pytest.raises(ValueError):
            fn("C101", blank)
        assert temp_db.repository_connections == []

    @pytest.mark.parametrize("fn", CUSTOMER_AND_ORDER_FUNCTIONS, ids=_fn_id)
    def test_blank_customer_and_order_ids_rejected(self, temp_db, fn):
        with pytest.raises(ValueError):
            fn("", "")
        assert temp_db.repository_connections == []

    @pytest.mark.parametrize("bad", [None, 101], ids=["None", "int"])
    @pytest.mark.parametrize(
        "fn",
        CUSTOMER_ONLY_FUNCTIONS + CUSTOMER_AND_ORDER_FUNCTIONS,
        ids=_fn_id,
    )
    def test_non_string_customer_id_rejected(self, temp_db, fn, bad):
        # repository._require_non_empty documents ValueError for non-strings.
        args = (bad,) if fn in CUSTOMER_ONLY_FUNCTIONS else (bad, "ORD1001")
        with pytest.raises(ValueError):
            fn(*args)
        assert temp_db.repository_connections == []

    @pytest.mark.parametrize("bad", [None, 1001], ids=["None", "int"])
    @pytest.mark.parametrize("fn", CUSTOMER_AND_ORDER_FUNCTIONS, ids=_fn_id)
    def test_non_string_order_id_rejected(self, temp_db, fn, bad):
        with pytest.raises(ValueError):
            fn("C101", bad)
        assert temp_db.repository_connections == []
