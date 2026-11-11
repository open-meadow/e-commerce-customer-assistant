"""Support-ticket creation (docs/01 Section G, D2 ticket contract)."""

from __future__ import annotations

import inspect
import re
import sqlite3

import pytest

import app.db.repository as repository
from app.db import clock
from app.db.connection import get_connection
from app.db.errors import (
    DatabaseConstraintError,
    DatabaseNotInitializedError,
    DatabaseOperationalError,
)
from app.db.repository import create_support_ticket, get_customer_tickets
from conftest import T0
from test_returns import FaultyConnection  # shared fault-injection proxy

TKT_ID = re.compile(r"TKT_[0-9a-f]{32}")


def tickets(path, customer=None):
    connection = get_connection(path)
    try:
        sql = "SELECT * FROM support_tickets"
        params = ()
        if customer:
            sql += " WHERE customer_id = ?"
            params = (customer,)
        return [dict(r) for r in connection.execute(sql + " ORDER BY ticket_id", params)]
    finally:
        connection.close()


def make(customer="C101", issue="order", description="desc", summary="sum", order_id=None):
    return create_support_ticket(customer, issue, description, summary, order_id)


class TestCreate:
    def test_result_shape_is_compact(self, temp_db):
        result = make()
        assert set(result) == {"outcome", "ticket_id"}
        assert result["outcome"] == "created" and TKT_ID.fullmatch(result["ticket_id"])

    def test_stored_values(self, temp_db):
        result = make("C101", "payment", "charged twice", "Customer Issue\ncharged twice",
                      "ORD1001")
        assert tickets(temp_db) == [{
            "ticket_id": result["ticket_id"], "customer_id": "C101", "order_id": "ORD1001",
            "issue_type": "payment", "description": "charged twice",
            "conversation_summary": "Customer Issue\ncharged twice",
            "status": "Open", "priority": "Normal", "created_at": "2026-11-07T00:00:00Z",
        }]

    def test_general_ticket_has_null_order(self, temp_db):
        make(order_id=None)
        assert tickets(temp_db)[0]["order_id"] is None

    @pytest.mark.parametrize("bad_order", ["ORD1003", "ORD9999"])
    def test_unowned_and_nonexistent_orders_give_a_general_ticket_and_same_result(
            self, temp_db, bad_order):
        result = make(order_id=bad_order)
        assert set(result) == {"outcome", "ticket_id"} and result["outcome"] == "created"
        assert tickets(temp_db)[0]["order_id"] is None

    def test_results_for_owned_and_unowned_orders_have_identical_shape(self, temp_db):
        owned = make(order_id="ORD1001")
        unowned = make(order_id="ORD1003")
        assert set(owned) == set(unowned)

    def test_status_and_priority_are_not_caller_controlled(self):
        parameters = set(inspect.signature(create_support_ticket).parameters)
        assert "status" not in parameters and "priority" not in parameters
        with pytest.raises(TypeError):
            create_support_ticket("C101", "order", "d", "s", None, status="Resolved")

    def test_text_preserved_exactly(self, temp_db):
        description = "  line one\nline two  \t"
        summary = "\nCustomer Issue\n  spaced  "
        make(description=description, summary=summary)
        row = tickets(temp_db)[0]
        assert row["description"] == description and row["conversation_summary"] == summary

    def test_limits_are_2000_code_points(self, temp_db):
        make(description="é" * 2000, summary="é" * 2000)
        with pytest.raises(ValueError):
            make(description="a" * 2001)
        with pytest.raises(ValueError):
            make(summary="a" * 2001)

    def test_created_at_uses_the_captured_time_once(self, temp_db, monkeypatch):
        calls = []
        monkeypatch.setattr(clock, "utc_now", lambda: calls.append(1) or T0)
        make()
        assert len(calls) == 1
        assert tickets(temp_db)[0]["created_at"] == "2026-11-07T00:00:00Z"

    def test_ids_are_unique(self, temp_db):
        assert len({make()["ticket_id"] for _ in range(5)}) == 5

    @pytest.mark.parametrize("issue", ["order", "return", "payment", "product", "general"])
    def test_every_allowlisted_issue_type(self, temp_db, issue):
        assert make(issue=issue)["outcome"] == "created"


class TestValidation:
    @pytest.fixture(autouse=True)
    def _no_database(self, monkeypatch):
        def boom():
            raise AssertionError("the database was opened before validation")
        monkeypatch.setattr(repository, "get_connection", boom)

    @pytest.mark.parametrize("bad", [None, 3, b"order"])
    def test_issue_type_wrong_type(self, bad):
        with pytest.raises(TypeError):
            make(issue=bad)

    @pytest.mark.parametrize("bad", ["Order", "refund_issue", "other", "", "order "])
    def test_issue_type_not_in_allowlist(self, bad):
        with pytest.raises(ValueError):
            make(issue=bad)

    @pytest.mark.parametrize("field", ["description", "summary"])
    @pytest.mark.parametrize("bad", [None, 5, b"x", ["a"]])
    def test_text_wrong_type(self, field, bad):
        with pytest.raises(TypeError):
            make(**{field: bad})

    @pytest.mark.parametrize("field", ["description", "summary"])
    @pytest.mark.parametrize("bad", ["", "  \t\n", " ", "a\x00b", "a\ud800b"])
    def test_text_invalid_value(self, field, bad):
        with pytest.raises(ValueError):
            make(**{field: bad})

    def test_summary_is_required(self):
        with pytest.raises(TypeError):
            create_support_ticket("C101", "order", "description")

    @pytest.mark.parametrize("bad", [5, b"O"])
    def test_order_id_wrong_type(self, bad):
        with pytest.raises(TypeError):
            make(order_id=bad)

    @pytest.mark.parametrize("bad", ["", "  ", "O\x00"])
    def test_order_id_invalid_value(self, bad):
        with pytest.raises(ValueError):
            make(order_id=bad)

    @pytest.mark.parametrize("bad", [None, 7, ""])
    def test_customer_id(self, bad):
        with pytest.raises((TypeError, ValueError)):
            make(customer=bad)


class TestFailures:
    @pytest.fixture
    def faulty(self, monkeypatch, temp_db):
        log = []

        def install(**faults):
            monkeypatch.setattr(repository, "get_connection",
                                lambda: FaultyConnection(get_connection(temp_db), log, **faults))
            return log

        return install

    def test_insert_failure_is_operational_error(self, temp_db, faulty):
        log = faulty(fail_on=("INSERT", sqlite3.OperationalError("secret detail")))
        with pytest.raises(DatabaseOperationalError) as info:
            make()
        assert "secret detail" not in str(info.value)
        assert "ROLLBACK" in log and tickets(temp_db) == []

    def test_commit_failure_records_nothing(self, temp_db, faulty):
        faulty(fail_commit=sqlite3.OperationalError("disk full"))
        with pytest.raises(DatabaseOperationalError):
            make()
        assert tickets(temp_db) == []

    def test_id_collision_is_a_constraint_error(self, temp_db, monkeypatch):
        first = make()["ticket_id"]
        monkeypatch.setattr(repository, "_new_id", lambda prefix: first)
        with pytest.raises(DatabaseConstraintError):
            make()
        assert len(tickets(temp_db)) == 1

    def test_unexpected_exception_rolls_back_and_reraises(self, temp_db, faulty):
        boom = RuntimeError("bug")
        faulty(fail_on=("INSERT", boom))
        with pytest.raises(RuntimeError) as info:
            make()
        assert info.value is boom

    def test_rollback_failure_does_not_mask(self, temp_db, faulty):
        faulty(fail_on=("INSERT", sqlite3.OperationalError("first")),
               fail_rollback=sqlite3.OperationalError("second"))
        with pytest.raises(DatabaseOperationalError):
            make()

    def test_missing_database_raises_and_creates_nothing(self, tmp_path, monkeypatch):
        target = tmp_path / "x" / "missing.db"
        monkeypatch.setenv("APP_DB_PATH", str(target))
        with pytest.raises(DatabaseNotInitializedError):
            make()
        assert not target.parent.exists()


class TestReads:
    def test_get_customer_tickets_is_owner_scoped_and_ordered(self, temp_db, set_now):
        from datetime import timedelta
        make("C101", description="first")
        set_now(T0 + timedelta(minutes=1))
        make("C101", description="second")
        make("C102", description="other customer")
        own = get_customer_tickets("C101")
        assert [t["description"] for t in own] == ["second", "first"]
        assert {t["customer_id"] for t in own} == {"C101"}
        assert get_customer_tickets("C106") == []
