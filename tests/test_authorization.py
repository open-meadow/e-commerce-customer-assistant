"""Customer isolation and read contracts (docs/01 Section E, D2 reads).

Every customer-scoped read filters on ``authenticated_customer_id`` in
SQL. These tests use several customers with real rows so the filter is
shown to discriminate, not merely to return nothing.
"""

from __future__ import annotations

import pytest

import app.db.repository as repository
from app.db.errors import DatabaseDataIntegrityError
from app.db.repository import (
    create_return_request,
    create_support_ticket,
    get_customer_returns,
    get_customer_tickets,
    get_order,
    get_order_items,
    list_customer_orders,
    list_customers_for_login,
)

ORDER_KEYS = {"order_id", "customer_id", "status", "payment_status", "refund_status",
              "refund_amount", "order_date", "delivered_at", "total_amount"}
INJECTIONS = ["' OR '1'='1", "ORD1001' OR '1'='1", "ORD1001; DROP TABLE orders;--",
              "%", "*", "C101' --", "ORD1001​", "ord1001"]


class TestOrders:
    def test_own_order_is_returned(self, temp_db):
        order = get_order("C101", "ORD1001")
        assert set(order) == ORDER_KEYS
        assert order["customer_id"] == "C101" and order["status"] == "Delivered"

    def test_cross_customer_and_missing_orders_are_indistinguishable(self, temp_db):
        assert get_order("C101", "ORD1003") is None
        assert get_order("C101", "ORD9999") is None

    def test_list_orders_contains_only_own_orders(self, temp_db):
        for customer, expected in {"C101": 3, "C102": 3, "C103": 2, "C104": 3,
                                   "C105": 2, "C106": 2}.items():
            orders = list_customer_orders(customer)
            assert len(orders) == expected
            assert {o["customer_id"] for o in orders} == {customer}
        assert [o["order_id"] for o in list_customer_orders("C101")] == [
            "ORD1001", "ORD1002", "ORD1014"]

    def test_unknown_customer_gets_empty_results(self, temp_db):
        assert list_customer_orders("C999") == []
        assert get_customer_returns("C999") == [] and get_customer_tickets("C999") == []

    def test_order_items_scoped_through_the_order(self, temp_db):
        assert [i["item_id"] for i in get_order_items("C103", "ORD1005")] == ["IT105", "IT106"]
        assert get_order_items("C101", "ORD1005") == []  # another customer's order
        assert get_order_items("C101", "ORD9999") == []  # missing order

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_injection_like_ids_leak_nothing(self, temp_db, payload):
        assert get_order("C101", payload) is None
        assert get_order_items("C101", payload) == []
        assert list_customer_orders(payload) == []
        assert get_customer_returns(payload) == [] and get_customer_tickets(payload) == []
        assert get_order(payload, "ORD1001") is None


class TestReturnsAndTicketsIsolation:
    @pytest.fixture
    def populated(self, temp_db):
        create_return_request("C101", "ORD1001", "IT101", "mine", reason_category="standard")
        create_return_request("C103", "ORD1005", "IT105", "theirs", reason_category="damaged")
        create_support_ticket("C101", "order", "c101 ticket", "summary 101", "ORD1001")
        create_support_ticket("C102", "general", "c102 ticket", "summary 102")

    def test_each_customer_sees_only_their_returns(self, populated):
        mine = get_customer_returns("C101")
        theirs = get_customer_returns("C103")
        assert [r["reason"] for r in mine] == ["mine"]
        assert [r["reason"] for r in theirs] == ["theirs"]
        assert {r["customer_id"] for r in mine} == {"C101"}
        assert get_customer_returns("C102") == []

    def test_each_customer_sees_only_their_tickets(self, populated):
        assert [t["description"] for t in get_customer_tickets("C101")] == ["c101 ticket"]
        assert [t["description"] for t in get_customer_tickets("C102")] == ["c102 ticket"]
        assert get_customer_tickets("C103") == []

    def test_a_customer_cannot_attach_a_ticket_to_someone_elses_order(self, populated):
        result = create_support_ticket("C102", "order", "about ORD1001", "s", "ORD1001")
        assert set(result) == {"outcome", "ticket_id"}
        ticket = [t for t in get_customer_tickets("C102") if t["description"] == "about ORD1001"][0]
        assert ticket["order_id"] is None

    def test_a_customer_cannot_return_someone_elses_item(self, temp_db):
        assert create_return_request("C102", "ORD1001", "IT101", "r",
                                     reason_category="standard") == {
            "outcome": "order_not_found", "return_id": None}
        assert get_customer_returns("C101") == []


class TestLoginDirectory:
    def test_exact_keys_and_deterministic_order(self, temp_db):
        customers = list_customers_for_login()
        assert [set(c) for c in customers] == [{"customer_id", "name"}] * 6
        names = [c["name"] for c in customers]
        assert names == sorted(names)
        assert {c["customer_id"] for c in customers} == {f"C10{n}" for n in range(1, 7)}

    def test_exposes_no_contact_details(self, temp_db):
        text = str(list_customers_for_login())
        assert "@" not in text and "email" not in text and "phone" not in text

    def test_takes_no_arguments(self):
        with pytest.raises(TypeError):
            list_customers_for_login("C101")


class TestReadValidation:
    @pytest.fixture(autouse=True)
    def _no_database(self, monkeypatch):
        def boom():
            raise AssertionError("the database was opened before validation")
        monkeypatch.setattr(repository, "get_connection", boom)

    @pytest.mark.parametrize("function", [list_customer_orders, get_customer_returns,
                                          get_customer_tickets])
    def test_customer_id_validation(self, function):
        for bad in [None, 3, b"C101"]:
            with pytest.raises(TypeError):
                function(bad)
        for bad in ["", "  ", "C1\x00"]:
            with pytest.raises(ValueError):
                function(bad)

    @pytest.mark.parametrize("function", [get_order, get_order_items])
    def test_two_argument_validation(self, function):
        with pytest.raises(TypeError):
            function(None, "ORD1001")
        with pytest.raises(TypeError):
            function("C101", None)
        with pytest.raises(ValueError):
            function("C101", "")


class TestPersistedTimestampIntegrity:
    def test_malformed_order_date_raises_on_reads(self, temp_db, corrupt):
        corrupt(temp_db, "UPDATE orders SET order_date = 'bad' WHERE order_id = 'ORD1001'")
        with pytest.raises(DatabaseDataIntegrityError):
            get_order("C101", "ORD1001")
        with pytest.raises(DatabaseDataIntegrityError):
            list_customer_orders("C101")
        assert get_order("C102", "ORD1003")  # unaffected rows still read

    def test_future_requested_at_raises(self, temp_db, corrupt):
        create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")
        corrupt(temp_db, "UPDATE return_requests SET requested_at = '2999-01-01T00:00:00Z'")
        with pytest.raises(DatabaseDataIntegrityError):
            get_customer_returns("C101")

    def test_malformed_created_at_raises(self, temp_db, corrupt):
        create_support_ticket("C101", "general", "d", "s")
        corrupt(temp_db, "UPDATE support_tickets SET created_at = 'yesterday'")
        with pytest.raises(DatabaseDataIntegrityError):
            get_customer_tickets("C101")

    def test_timestamp_equal_to_now_is_allowed(self, temp_db):
        create_support_ticket("C101", "general", "d", "s")  # created_at == frozen now
        assert len(get_customer_tickets("C101")) == 1
