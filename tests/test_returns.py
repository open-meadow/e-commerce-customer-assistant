"""Return eligibility and creation (docs/01 Section F, D2 return contract).

Seed facts are hard-coded (baseline T0 = 2026-11-07T00:00:00Z):
ORD1001 (C101, item IT101) Delivered at T0-72h; ORD1005 (C103, items
IT105 and IT106) Delivered at T0-36h; ORD1013 (C106, IT114) Delivered at
T0-60h; ORD1003 (C102, IT103) Delivered at T0-240h; ORD1010 (C105, IT111)
Returned; ORD1006 (C103, IT107) Cancelled; ORD1002 (C101, IT102) Shipped.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import app.db.repository as repository
from app.db import clock
from app.db.connection import get_connection
from app.db.errors import (
    ConnectionError_,
    DatabaseConstraintError,
    DatabaseDataIntegrityError,
    DatabaseNotInitializedError,
    DatabaseOperationalError,
    SchemaIncompatibleError,
)
from app.db.repository import (
    check_return_eligibility,
    create_return_request,
    get_customer_returns,
)
from conftest import T0, fingerprint

UTC = timezone.utc
RET_ID = re.compile(r"RET_[0-9a-f]{32}")
ORD1001_DELIVERED = datetime(2026, 11, 4, tzinfo=UTC)
ORD1005_DELIVERED = datetime(2026, 11, 5, 12, tzinfo=UTC)


def rows(path, sql, params=()):
    connection = get_connection(path)
    try:
        return [dict(r) for r in connection.execute(sql, params)]
    finally:
        connection.close()


def return_rows(path):
    return rows(path, "SELECT * FROM return_requests ORDER BY return_id")


# ---------------------------------------------------------------------------
# Advisory check: precedence and outcomes
# ---------------------------------------------------------------------------

class TestEligibility:
    def test_eligible_standard(self, temp_db):
        result = check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")
        assert result == {
            "outcome": "eligible", "reason_category": "standard",
            "order_status": "Delivered", "window_hours": 168,
            "delivered_at": "2026-11-04T00:00:00Z",
            "window_closes_at": "2026-11-11T00:00:00Z",
            "evaluated_at": "2026-11-07T00:00:00Z",
            "window_reason": None, "existing_return_status": None,
        }

    @pytest.mark.parametrize("category", ["damaged", "wrong_product"])
    def test_forty_eight_hour_categories(self, temp_db, category):
        result = check_return_eligibility("C103", "ORD1005", "IT105", reason_category=category)
        assert result["outcome"] == "eligible" and result["window_hours"] == 48
        assert result["window_closes_at"] == "2026-11-07T12:00:00Z"

    @pytest.mark.parametrize("category", ["damaged", "wrong_product"])
    def test_damaged_window_closed_for_older_delivery(self, temp_db, category):
        result = check_return_eligibility("C101", "ORD1001", "IT101", reason_category=category)
        assert result["outcome"] == "outside_return_window"
        assert result["window_reason"] == "expired"

    # Inclusive boundaries: exactly at the closing instant is eligible.
    @pytest.mark.parametrize(
        "order_id,customer,item,category,delivered,hours",
        [
            ("ORD1001", "C101", "IT101", "standard", ORD1001_DELIVERED, 168),
            ("ORD1005", "C103", "IT105", "damaged", ORD1005_DELIVERED, 48),
            ("ORD1005", "C103", "IT105", "wrong_product", ORD1005_DELIVERED, 48),
        ],
    )
    def test_boundaries_inclusive(self, temp_db, set_now, order_id, customer, item,
                                  category, delivered, hours):
        closes = delivered + timedelta(hours=hours)
        set_now(closes)
        assert check_return_eligibility(customer, order_id, item,
                                        reason_category=category)["outcome"] == "eligible"
        set_now(closes - timedelta(seconds=1))
        assert check_return_eligibility(customer, order_id, item,
                                        reason_category=category)["outcome"] == "eligible"
        set_now(closes + timedelta(seconds=1))
        late = check_return_eligibility(customer, order_id, item, reason_category=category)
        assert late["outcome"] == "outside_return_window"
        assert late["window_closes_at"] == closes.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_missing_and_unowned_orders_are_indistinguishable(self, temp_db):
        unowned = check_return_eligibility("C102", "ORD1001", "IT101", reason_category="standard")
        missing = check_return_eligibility("C102", "ORD9999", "IT101", reason_category="standard")
        assert unowned == missing
        assert unowned["outcome"] == "order_not_found"
        assert unowned["order_status"] is None

    @pytest.mark.parametrize(
        "customer,order_id,item,status",
        [
            ("C101", "ORD1002", "IT102", "Shipped"),
            ("C101", "ORD1014", "IT115", "Processing"),
            ("C103", "ORD1006", "IT107", "Cancelled"),
            ("C104", "ORD1007", "IT108", "Out for Delivery"),
            ("C104", "ORD1009", "IT110", "Packed"),
            ("C105", "ORD1010", "IT111", "Returned"),
        ],
    )
    def test_every_non_delivered_status_is_ineligible(self, temp_db, customer, order_id,
                                                      item, status):
        result = check_return_eligibility(customer, order_id, item, reason_category="standard")
        assert result["outcome"] == "order_status_ineligible"
        assert result["order_status"] == status
        assert result["window_hours"] is None and result["delivered_at"] is None

    def test_status_is_checked_before_the_item(self, temp_db):
        result = check_return_eligibility("C101", "ORD1002", "NOPE", reason_category="standard")
        assert result["outcome"] == "order_status_ineligible"

    @pytest.mark.parametrize("item", ["NOPE", "IT103"])  # nonexistent / another order's item
    def test_item_not_found(self, temp_db, item):
        result = check_return_eligibility("C101", "ORD1001", item, reason_category="standard")
        assert result["outcome"] == "item_not_found"
        assert result["order_status"] == "Delivered"

    def test_active_return_wins_over_an_expired_window(self, temp_db, set_now):
        assert create_return_request("C101", "ORD1001", "IT101", "r",
                                     reason_category="standard")["outcome"] == "created"
        set_now(T0 + timedelta(days=30))
        result = check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")
        assert result["outcome"] == "active_return_exists"
        assert result["existing_return_status"] == "Requested"

    def test_status_wins_over_an_active_return(self, temp_db, raw):
        create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")
        raw.execute("UPDATE orders SET status = 'Returned' WHERE order_id = 'ORD1001'")
        raw.commit()
        result = check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")
        assert result["outcome"] == "order_status_ineligible"
        assert result["order_status"] == "Returned"

    @pytest.mark.parametrize("released", ["Rejected", "Completed"])
    def test_released_returns_do_not_block(self, temp_db, raw, released):
        create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")
        raw.execute("UPDATE return_requests SET status = ?", (released,))
        raw.commit()
        assert check_return_eligibility("C101", "ORD1001", "IT101",
                                        reason_category="standard")["outcome"] == "eligible"

    def test_check_is_read_only(self, temp_db):
        before = fingerprint(temp_db)
        for args in [("C101", "ORD1001", "IT101"), ("C102", "ORD1001", "IT101"),
                     ("C101", "ORD1002", "IT102")]:
            check_return_eligibility(*args, reason_category="standard")
        assert fingerprint(temp_db) == before

    def test_check_uses_one_captured_time(self, temp_db, monkeypatch):
        calls = []
        monkeypatch.setattr(clock, "utc_now", lambda: calls.append(1) or T0)
        check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")
        assert len(calls) == 1


class TestEligibilityDataIntegrity:
    def test_delivered_without_timestamp_is_a_defensive_outcome(self, temp_db, corrupt):
        corrupt(temp_db, "UPDATE orders SET delivered_at = NULL WHERE order_id = 'ORD1001'")
        result = check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")
        assert result["outcome"] == "delivery_timestamp_missing"
        assert result["window_reason"] == "delivery_not_recorded"
        assert create_return_request("C101", "ORD1001", "IT101", "r",
                                     reason_category="standard") == {
            "outcome": "delivery_timestamp_missing", "return_id": None}

    @pytest.mark.parametrize("bad", ["garbage", "2026-11-04T00:00:00+00:00",
                                     "2026-02-30T00:00:00Z", "2026-11-04T00:00:00.5Z"])
    def test_malformed_persisted_timestamp_raises(self, temp_db, corrupt, bad):
        corrupt(temp_db, "UPDATE orders SET delivered_at = ? WHERE order_id = 'ORD1001'", (bad,))
        with pytest.raises(DatabaseDataIntegrityError):
            check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")
        with pytest.raises(DatabaseDataIntegrityError):
            create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")
        assert return_rows(temp_db) == []

    def test_future_persisted_timestamp_raises(self, temp_db, corrupt, set_now):
        set_now(datetime(2026, 11, 3, tzinfo=UTC))  # before ORD1001's delivery
        with pytest.raises(DatabaseDataIntegrityError):
            check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")

    def test_malformed_order_date_raises(self, temp_db, corrupt):
        corrupt(temp_db, "UPDATE orders SET order_date = 'bad' WHERE order_id = 'ORD1001'")
        with pytest.raises(DatabaseDataIntegrityError):
            check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")


# ---------------------------------------------------------------------------
# Authoritative write
# ---------------------------------------------------------------------------

class TestCreate:
    def test_created_result_shape(self, temp_db):
        result = create_return_request("C101", "ORD1001", "IT101", "arrived broken",
                                       reason_category="standard")
        assert set(result) == {"outcome", "return_id"}
        assert result["outcome"] == "created" and RET_ID.fullmatch(result["return_id"])

    def test_created_row_contents(self, temp_db):
        result = create_return_request("C101", "ORD1001", "IT101", "arrived broken",
                                       reason_category="standard")
        row = return_rows(temp_db)[0]
        assert row == {
            "return_id": result["return_id"], "customer_id": "C101", "order_id": "ORD1001",
            "item_id": "IT101", "reason": "arrived broken", "reason_category": "standard",
            "status": "Requested", "requested_at": "2026-11-07T00:00:00Z",
        }

    @pytest.mark.parametrize("category", ["damaged", "wrong_product", "standard"])
    def test_category_persisted(self, temp_db, category):
        create_return_request("C103", "ORD1005", "IT105", "r", reason_category=category)
        assert return_rows(temp_db)[0]["reason_category"] == category

    def test_reason_stored_exactly_without_prefix_or_stripping(self, temp_db):
        text = "  leading and trailing  \nsecond line  "
        create_return_request("C101", "ORD1001", "IT101", text, reason_category="standard")
        assert return_rows(temp_db)[0]["reason"] == text

    def test_full_thousand_code_points_stored(self, temp_db):
        text = "é" * 1000
        create_return_request("C101", "ORD1001", "IT101", text, reason_category="standard")
        assert return_rows(temp_db)[0]["reason"] == text

    @pytest.mark.parametrize(
        "customer,order_id,item,expected",
        [
            ("C102", "ORD1001", "IT101", "order_not_found"),
            ("C101", "ORD9999", "IT101", "order_not_found"),
            ("C101", "ORD1002", "IT102", "order_status_ineligible"),
            ("C105", "ORD1010", "IT111", "order_status_ineligible"),
            ("C101", "ORD1001", "NOPE", "item_not_found"),
            ("C102", "ORD1003", "IT103", "outside_return_window"),
        ],
    )
    def test_rejections_are_compact_and_write_nothing(self, temp_db, customer, order_id,
                                                      item, expected):
        before = fingerprint(temp_db)
        result = create_return_request(customer, order_id, item, "r", reason_category="standard")
        assert result == {"outcome": expected, "return_id": None}
        assert "order_status" not in result
        assert return_rows(temp_db) == []
        assert fingerprint(temp_db) == before

    def test_write_rechecks_after_a_passing_check(self, temp_db, set_now):
        assert check_return_eligibility("C101", "ORD1001", "IT101",
                                        reason_category="standard")["outcome"] == "eligible"
        set_now(ORD1001_DELIVERED + timedelta(hours=168, seconds=1))
        assert create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard") == {
            "outcome": "outside_return_window", "return_id": None}

    def test_write_rechecks_status_change(self, temp_db, raw):
        raw.execute("UPDATE orders SET status = 'Returned' WHERE order_id = 'ORD1001'")
        raw.commit()
        assert create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard") == {
            "outcome": "order_status_ineligible", "return_id": None}

    def test_second_request_is_a_duplicate(self, temp_db):
        first = create_return_request("C101", "ORD1001", "IT101", "r1", reason_category="standard")
        second = create_return_request("C101", "ORD1001", "IT101", "r2", reason_category="standard")
        assert first["outcome"] == "created"
        assert second == {"outcome": "active_return_exists", "return_id": None}
        assert len(return_rows(temp_db)) == 1

    @pytest.mark.parametrize("released", ["Rejected", "Completed"])
    def test_released_status_allows_a_new_request(self, temp_db, raw, released):
        create_return_request("C101", "ORD1001", "IT101", "r1", reason_category="standard")
        raw.execute("UPDATE return_requests SET status = ?", (released,))
        raw.commit()
        assert create_return_request("C101", "ORD1001", "IT101", "r2",
                                     reason_category="standard")["outcome"] == "created"
        assert len(return_rows(temp_db)) == 2

    def test_approved_still_blocks(self, temp_db, raw):
        create_return_request("C101", "ORD1001", "IT101", "r1", reason_category="standard")
        raw.execute("UPDATE return_requests SET status = 'Approved'")
        raw.commit()
        assert create_return_request("C101", "ORD1001", "IT101", "r2",
                                     reason_category="standard")["outcome"] == "active_return_exists"

    def test_ids_are_unique_and_formatted(self, temp_db):
        ids = {
            create_return_request("C103", "ORD1005", item, "r", reason_category="standard")["return_id"]
            for item in ("IT105", "IT106")
        }
        assert len(ids) == 2 and all(RET_ID.fullmatch(i) for i in ids)

    def test_get_customer_returns_exposes_category_and_orders_desc(self, temp_db, set_now):
        create_return_request("C103", "ORD1005", "IT105", "a", reason_category="damaged")
        set_now(T0 + timedelta(minutes=5))
        create_return_request("C103", "ORD1005", "IT106", "b", reason_category="standard")
        returns = get_customer_returns("C103")
        assert [r["reason"] for r in returns] == ["b", "a"]
        assert [r["reason_category"] for r in returns] == ["standard", "damaged"]
        assert set(returns[0]) == {"return_id", "customer_id", "order_id", "item_id", "reason",
                                   "reason_category", "status", "requested_at"}


# ---------------------------------------------------------------------------
# Validation happens before any database access
# ---------------------------------------------------------------------------

class TestValidation:
    @pytest.fixture(autouse=True)
    def _no_database(self, monkeypatch):
        def boom():
            raise AssertionError("the database was opened before validation")
        monkeypatch.setattr(repository, "get_connection", boom)

    GOOD = dict(authenticated_customer_id="C101", order_id="ORD1001", item_id="IT101",
                reason="r")

    @pytest.mark.parametrize("field", ["authenticated_customer_id", "order_id", "item_id", "reason"])
    @pytest.mark.parametrize("bad", [None, 5, b"x"])
    def test_wrong_types(self, field, bad):
        args = dict(self.GOOD, **{field: bad})
        with pytest.raises(TypeError):
            create_return_request(**args, reason_category="standard")

    @pytest.mark.parametrize("field", ["authenticated_customer_id", "order_id", "item_id", "reason"])
    @pytest.mark.parametrize("bad", ["", "   ", "a\x00b", "\ud800"])
    def test_invalid_values(self, field, bad):
        args = dict(self.GOOD, **{field: bad})
        with pytest.raises(ValueError):
            create_return_request(**args, reason_category="standard")

    def test_reason_limits(self):
        with pytest.raises(ValueError):
            create_return_request(**dict(self.GOOD, reason="a" * 1001), reason_category="standard")
        with pytest.raises(ValueError):
            create_return_request(**dict(self.GOOD, reason=" 　"), reason_category="standard")

    def test_category_validation(self):
        with pytest.raises(TypeError):
            create_return_request(**self.GOOD, reason_category=None)
        with pytest.raises(ValueError):
            create_return_request(**self.GOOD, reason_category="other")
        with pytest.raises(ValueError):
            check_return_eligibility("C101", "ORD1001", "IT101", reason_category="x")

    def test_category_is_required_and_keyword_only(self):
        with pytest.raises(TypeError):
            create_return_request("C101", "ORD1001", "IT101", "r")
        with pytest.raises(TypeError):
            create_return_request("C101", "ORD1001", "IT101", "r", "standard")

    def test_no_caller_supplied_time_parameter(self):
        with pytest.raises(TypeError):
            create_return_request(**self.GOOD, reason_category="standard",
                                  reference_time=datetime(2026, 1, 1, tzinfo=UTC))
        with pytest.raises(TypeError):
            check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard",
                                     reference_time=datetime(2026, 1, 1, tzinfo=UTC))

    def test_check_argument_validation(self):
        with pytest.raises(TypeError):
            check_return_eligibility(None, "ORD1001", "IT101", reason_category="standard")
        with pytest.raises(ValueError):
            check_return_eligibility("C101", "", "IT101", reason_category="standard")


# ---------------------------------------------------------------------------
# Transactions, clock capture and failures (fault injection)
# ---------------------------------------------------------------------------

class FaultyConnection:
    """Delegating proxy that records calls and can inject failures."""

    def __init__(self, real, log, *, fail_on=None, fail_commit=None, fail_rollback=None,
                 fail_close=None, rewrite=None):
        self.real, self.log = real, log
        self.fail_on, self.fail_commit = fail_on, fail_commit
        self.fail_rollback, self.fail_close, self.rewrite = fail_rollback, fail_close, rewrite

    @property
    def in_transaction(self):
        return self.real.in_transaction

    def execute(self, sql, params=()):
        verb = " ".join(sql.split()[:2]).upper() if sql.strip().upper().startswith("BEGIN") \
            else sql.split()[0].upper()
        self.log.append(verb)
        if self.fail_on and verb.startswith(self.fail_on[0]):
            raise self.fail_on[1]
        if self.rewrite:
            sql = self.rewrite(sql)
        return self.real.execute(sql, params)

    def commit(self):
        self.log.append("COMMIT")
        if self.fail_commit:
            raise self.fail_commit
        self.real.commit()

    def rollback(self):
        self.log.append("ROLLBACK")
        if self.fail_rollback:
            raise self.fail_rollback
        self.real.rollback()

    def close(self):
        self.log.append("CLOSE")
        self.real.close()
        if self.fail_close:
            raise self.fail_close


@pytest.fixture
def faulty(monkeypatch, temp_db):
    """``faulty(**faults)`` swaps the repository connection for a proxy."""
    log: list[str] = []

    def install(**faults):
        monkeypatch.setattr(repository, "get_connection",
                            lambda: FaultyConnection(get_connection(temp_db), log, **faults))
        return log

    return install


class TestTransactions:
    def test_begin_immediate_then_clock_then_commit(self, temp_db, faulty, monkeypatch):
        log = faulty()
        monkeypatch.setattr(clock, "utc_now", lambda: log.append("NOW") or T0)
        assert create_return_request("C101", "ORD1001", "IT101", "r",
                                     reason_category="standard")["outcome"] == "created"
        assert log[0] == "BEGIN IMMEDIATE" and log[1] == "NOW"
        assert log.count("NOW") == 1
        assert log[-2:] == ["COMMIT", "CLOSE"]

    def test_created_only_after_commit(self, temp_db, faulty):
        faulty(fail_commit=sqlite3.OperationalError("disk full"))
        with pytest.raises(DatabaseOperationalError) as info:
            create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")
        assert "disk full" not in str(info.value)
        assert return_rows(temp_db) == []

    def test_business_rejection_rolls_back(self, temp_db, faulty):
        log = faulty()
        create_return_request("C102", "ORD1001", "IT101", "r", reason_category="standard")
        assert "ROLLBACK" in log and "COMMIT" not in log

    def test_insert_failure_is_operational_error_and_rolls_back(self, temp_db, faulty):
        log = faulty(fail_on=("INSERT", sqlite3.OperationalError("boom")))
        with pytest.raises(DatabaseOperationalError):
            create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")
        assert "ROLLBACK" in log and log[-1] == "CLOSE"
        assert return_rows(temp_db) == []

    def test_begin_failure_is_operational_error(self, temp_db, faulty):
        faulty(fail_on=("BEGIN", sqlite3.OperationalError("locked")))
        with pytest.raises(DatabaseOperationalError):
            create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")

    def test_id_collision_is_a_constraint_error(self, temp_db, monkeypatch):
        create_return_request("C103", "ORD1005", "IT105", "r", reason_category="standard")
        existing = return_rows(temp_db)[0]["return_id"]
        monkeypatch.setattr(repository, "_new_id", lambda prefix: existing)
        with pytest.raises(DatabaseConstraintError):
            create_return_request("C103", "ORD1005", "IT106", "r", reason_category="standard")
        assert len(return_rows(temp_db)) == 1

    def test_unexpected_exception_rolls_back_and_reraises_unchanged(self, temp_db, faulty):
        boom = RuntimeError("programming error")
        log = faulty(fail_on=("INSERT", boom))
        with pytest.raises(RuntimeError) as info:
            create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")
        assert info.value is boom
        assert "ROLLBACK" in log

    def test_rollback_failure_never_masks_the_original(self, temp_db, faulty):
        faulty(fail_on=("INSERT", sqlite3.OperationalError("first")),
               fail_rollback=sqlite3.OperationalError("second"))
        with pytest.raises(DatabaseOperationalError) as info:
            create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")
        assert isinstance(info.value.__cause__, sqlite3.OperationalError)
        assert "first" in str(info.value.__cause__)

    def test_close_failure_does_not_mask_the_result(self, temp_db, faulty):
        faulty(fail_close=sqlite3.OperationalError("close failed"))
        assert create_return_request("C101", "ORD1001", "IT101", "r",
                                     reason_category="standard")["outcome"] == "created"

    def test_unique_index_is_the_backstop_when_the_precheck_is_bypassed(self, temp_db, faulty):
        create_return_request("C101", "ORD1001", "IT101", "first", reason_category="standard")
        faulty(rewrite=lambda sql: sql.replace("status IN ('Requested', 'Approved')",
                                               "status = 'NeverMatches'"))
        result = create_return_request("C101", "ORD1001", "IT101", "second",
                                       reason_category="standard")
        assert result == {"outcome": "active_return_exists", "return_id": None}
        assert [r["reason"] for r in return_rows(temp_db)] == ["first"]

    def test_check_failure_is_operational_error(self, temp_db, faulty):
        faulty(fail_on=("SELECT", sqlite3.OperationalError("boom")))
        with pytest.raises(DatabaseOperationalError):
            check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")


class TestConnectionFailures:
    def test_missing_database_raises_and_creates_nothing(self, tmp_path, monkeypatch):
        target = tmp_path / "sub" / "missing.db"
        monkeypatch.setenv("APP_DB_PATH", str(target))
        with pytest.raises(DatabaseNotInitializedError):
            create_return_request("C101", "ORD1001", "IT101", "r", reason_category="standard")
        assert not target.parent.exists()

    def test_legacy_database_raises_incompatible(self, tmp_path, monkeypatch):
        target = tmp_path / "legacy.db"
        connection = sqlite3.connect(target)
        connection.execute("CREATE TABLE orders (x)")
        connection.commit()
        connection.close()
        monkeypatch.setenv("APP_DB_PATH", str(target))
        with pytest.raises(SchemaIncompatibleError):
            check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_only_one_of_many_concurrent_requests_creates_a_return(self, temp_db):
        barrier = threading.Barrier(8)

        def attempt(n):
            barrier.wait()
            return create_return_request("C101", "ORD1001", "IT101", f"r{n}",
                                         reason_category="standard")["outcome"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))
        assert outcomes.count("created") == 1
        assert outcomes.count("active_return_exists") == 7
        assert len(return_rows(temp_db)) == 1

    def test_distinct_items_can_be_created_concurrently(self, temp_db):
        def attempt(item):
            return create_return_request("C103", "ORD1005", item, "r",
                                         reason_category="standard")["outcome"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, ["IT105", "IT106"]))
        assert outcomes == ["created", "created"]
        assert len(return_rows(temp_db)) == 2
