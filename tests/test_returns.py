"""Tests for ``create_return_request`` (Checkpoint 8).

Contract under test: docs/01_database_design.md Sections F, H and I and
docs/02_database_implementation_plan.md Sections 4 and 5, as implemented
in ``app/db/repository.py`` (Checkpoint 7).

Isolation
---------
* Every test runs against its own temporary SQLite file under pytest's
  ``tmp_path``, built with the real ``create_schema`` and ``seed_database``.
* ``app.db.repository.get_connection`` is monkeypatched to open that
  file, so the repository never uses the default ``data/app.db``.
* An autouse guard patches ``sqlite3.connect`` so that any attempt to
  open the real database path raises immediately. The guard compares
  path *strings* only: these tests never open, stat, read, write, seed
  or delete ``data/app.db``.
* Eligibility tests pass an explicit ``reference_time``. The only test
  that uses the real clock builds its ``delivered_at`` relative to it.
* Expected seed facts are hard-coded (not re-queried), so a change to
  the seed fails loudly instead of silently changing what is tested.

Fault injection uses a thin proxy around a real connection
(``FaultyConnection``) so database failures, commit failures and
rollback failures can be simulated without touching production code.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pytest

import app.db.repository as repository
from app.db.connection import DEFAULT_DB_PATH, ConnectionError_, get_connection
from app.db.repository import create_return_request
from app.db.schema import create_schema
from app.db.seed_data import seed_database

UTC = timezone.utc
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# ---------------------------------------------------------------------------
# Seed facts (app/db/seed_data.py, fixed baseline 2026-11-07T00:00:00Z)
# ---------------------------------------------------------------------------

# ORD1001 (C101, item IT101) is Delivered at this instant.
DELIVERED = datetime(2026, 11, 4, tzinfo=UTC)
DELIVERED_STR = "2026-11-04T00:00:00Z"

ORDER_OWNER: dict[str, str] = {
    "ORD1001": "C101", "ORD1002": "C101", "ORD1014": "C101",
    "ORD1003": "C102", "ORD1004": "C102", "ORD1015": "C102",
    "ORD1005": "C103", "ORD1006": "C103",
    "ORD1007": "C104", "ORD1008": "C104", "ORD1009": "C104",
    "ORD1010": "C105", "ORD1011": "C105",
    "ORD1012": "C106", "ORD1013": "C106",
}
ORDER_ITEMS: dict[str, list[str]] = {
    "ORD1001": ["IT101"], "ORD1002": ["IT102"], "ORD1003": ["IT103"],
    "ORD1004": ["IT104"], "ORD1005": ["IT105", "IT106"], "ORD1006": ["IT107"],
    "ORD1007": ["IT108"], "ORD1008": ["IT109"], "ORD1009": ["IT110"],
    "ORD1010": ["IT111"], "ORD1011": ["IT112"], "ORD1012": ["IT113"],
    "ORD1013": ["IT114"], "ORD1014": ["IT115"], "ORD1015": ["IT116"],
}
CUSTOMERS = sorted(set(ORDER_OWNER.values()))

RETURN_ID_RE = re.compile(r"RET-[0-9A-F]{12}")
STORED_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

_DEFAULT = object()


def at(**delta: float) -> str:
    """ISO timestamp (UTC, whole seconds) at DELIVERED + delta."""
    return (DELIVERED + timedelta(**delta)).strftime(TS_FORMAT)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _never_open_real_database(monkeypatch):
    """Fail loudly if anything tries to open ``data/app.db``.

    Pure string comparison of paths: the real file is never accessed.
    """
    real_path = os.path.abspath(DEFAULT_DB_PATH)
    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if isinstance(database, (str, bytes, os.PathLike)):
            if os.path.abspath(os.fsdecode(database)) == real_path:
                raise AssertionError(
                    "a test tried to open the real database " + real_path
                )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)


class FaultyConnection:
    """Delegating wrapper around a real connection, for fault injection.

    ``fail_on=(verb, exc)`` raises *exc* instead of running the first
    statement whose first word is *verb* (``BEGIN``/``SELECT``/``INSERT``).
    ``rewrite(sql)`` may return replacement SQL for a statement.
    ``log`` records ``BEGIN IMMEDIATE``, statement verbs, ``COMMIT``,
    ``ROLLBACK`` and ``CLOSE`` in order.
    """

    def __init__(
        self,
        real: sqlite3.Connection,
        *,
        fail_on: Optional[tuple[str, BaseException]] = None,
        commit_exc: Optional[BaseException] = None,
        rollback_exc: Optional[BaseException] = None,
        close_exc: Optional[BaseException] = None,
        before_commit=None,
        rewrite=None,
    ) -> None:
        self.real = real
        self.log: list[str] = []
        self.sql: list[str] = []
        self._fail_on = fail_on
        self._commit_exc = commit_exc
        self._rollback_exc = rollback_exc
        self._close_exc = close_exc
        self._before_commit = before_commit
        self._rewrite = rewrite

    @property
    def in_transaction(self) -> bool:
        return self.real.in_transaction

    def execute(self, sql, params=()):
        words = sql.split()
        verb = words[0].upper()
        self.sql.append(sql)
        self.log.append(" ".join(w.upper() for w in words[:2]) if verb == "BEGIN" else verb)
        if self._fail_on is not None and self._fail_on[0] == verb:
            exc = self._fail_on[1]
            self._fail_on = None
            raise exc
        if self._rewrite is not None:
            replacement = self._rewrite(sql)
            if replacement is not None:
                sql = replacement
                params = ()
        return self.real.execute(sql, params)

    def commit(self):
        self.log.append("COMMIT")
        if self._before_commit is not None:
            self._before_commit()
        if self._commit_exc is not None:
            raise self._commit_exc
        return self.real.commit()

    def rollback(self):
        self.log.append("ROLLBACK")
        if self._rollback_exc is not None:
            raise self._rollback_exc
        return self.real.rollback()

    def close(self):
        self.log.append("CLOSE")
        try:
            self.real.close()
        finally:
            if self._close_exc is not None:
                raise self._close_exc


class TempDatabase:
    """Handle to one test's isolated, seeded temporary database."""

    def __init__(self, path: str, monkeypatch) -> None:
        self.path = path
        self._monkeypatch = monkeypatch
        self.repository_connections: list[Any] = []

    def open(self) -> sqlite3.Connection:
        return get_connection(self.path)

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        connection = self.open()
        try:
            return [dict(r) for r in connection.execute(sql, params).fetchall()]
        finally:
            connection.close()

    def execute(self, sql: str, params: tuple = ()) -> None:
        connection = self.open()
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def returns(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM return_requests ORDER BY return_id")

    def count_returns(self) -> int:
        return self.query("SELECT COUNT(*) AS n FROM return_requests")[0]["n"]

    def set_delivered_at(self, order_id: str, value: Optional[str]) -> None:
        self.execute("UPDATE orders SET delivered_at = ? WHERE order_id = ?", (value, order_id))

    def set_return_status(self, status: str) -> None:
        self.execute("UPDATE return_requests SET status = ?", (status,))

    def use_faulty_connection(self, **faults) -> list[FaultyConnection]:
        """Make the repository open FaultyConnections; returns them as created."""
        created: list[FaultyConnection] = []

        def factory() -> FaultyConnection:
            proxy = FaultyConnection(get_connection(self.path), **faults)
            created.append(proxy)
            self.repository_connections.append(proxy)
            return proxy

        self._monkeypatch.setattr(repository, "get_connection", factory)
        return created

    def use_zero_busy_timeout(self) -> None:
        """Repository connections fail immediately instead of waiting on locks."""

        def factory() -> sqlite3.Connection:
            connection = get_connection(self.path)
            connection.execute("PRAGMA busy_timeout = 0")
            self.repository_connections.append(connection)
            return connection

        self._monkeypatch.setattr(repository, "get_connection", factory)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db = TempDatabase(str(tmp_path / "returns_test.db"), monkeypatch)
    assert os.path.abspath(db.path) != os.path.abspath(DEFAULT_DB_PATH)

    setup = get_connection(db.path)
    try:
        create_schema(setup)
        seed_database(setup)
    finally:
        setup.close()

    def patched() -> sqlite3.Connection:
        connection = get_connection(db.path)
        db.repository_connections.append(connection)
        return connection

    monkeypatch.setattr(repository, "get_connection", patched)
    try:
        yield db
    finally:
        for connection in db.repository_connections:
            try:
                connection.close()
            except Exception:
                pass


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """Temporary database with NO schema: every statement fails."""
    db = TempDatabase(str(tmp_path / "empty_test.db"), monkeypatch)
    get_connection(db.path).close()

    def patched() -> sqlite3.Connection:
        connection = get_connection(db.path)
        db.repository_connections.append(connection)
        return connection

    monkeypatch.setattr(repository, "get_connection", patched)
    return db


def request(
    *,
    customer: str = "C101",
    order: str = "ORD1001",
    item: str = "IT101",
    reason: str = "Item not as described",
    category: Any = "standard",
    reference: Any = _DEFAULT,
) -> dict[str, Any]:
    """Call ``create_return_request`` with valid defaults (3 days after delivery)."""
    if reference is _DEFAULT:
        reference = at(days=3)
    return create_return_request(
        customer, order, item, reason,
        reason_category=category, reference_time=reference,
    )


def rejected(outcome: str) -> dict[str, Any]:
    return {"outcome": outcome, "return_id": None}


def assert_closed(connection: Any) -> None:
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


FAILED = rejected("failed")


# ---------------------------------------------------------------------------
# Harness canaries
# ---------------------------------------------------------------------------

class TestHarness:
    def test_seed_facts_the_tests_rely_on(self, temp_db):
        rows = temp_db.query(
            "SELECT o.order_id, o.customer_id, o.delivered_at, i.item_id "
            "FROM orders o JOIN order_items i ON i.order_id = o.order_id"
        )
        owner = {r["order_id"]: r["customer_id"] for r in rows}
        items: dict[str, list[str]] = {}
        for r in rows:
            items.setdefault(r["order_id"], []).append(r["item_id"])
        assert owner == ORDER_OWNER
        assert {k: sorted(v) for k, v in items.items()} == ORDER_ITEMS
        delivered = {r["order_id"]: r["delivered_at"] for r in rows}
        assert delivered["ORD1001"] == DELIVERED_STR
        assert delivered["ORD1002"] is None

    def test_no_return_rows_before_any_write(self, temp_db):
        assert temp_db.count_returns() == 0

    def test_temp_database_is_not_the_real_database(self, temp_db):
        assert os.path.abspath(temp_db.path) != os.path.abspath(DEFAULT_DB_PATH)

    def test_guard_blocks_the_real_database_path(self):
        with pytest.raises(AssertionError, match="real database"):
            sqlite3.connect(DEFAULT_DB_PATH)


# ---------------------------------------------------------------------------
# Eligibility windows
# ---------------------------------------------------------------------------

WINDOW_CASES = [
    pytest.param("standard", dict(hours=36), "created", id="standard-36h"),
    pytest.param("standard", dict(days=3), "created", id="standard-3d"),
    pytest.param("standard", dict(hours=167, minutes=59, seconds=59), "created", id="standard-168h-minus-1s"),
    pytest.param("standard", dict(hours=168), "created", id="standard-exactly-168h"),
    pytest.param("standard", dict(hours=168, seconds=1), "outside_window", id="standard-168h-plus-1s"),
    pytest.param("standard", dict(days=10), "outside_window", id="standard-10d"),
    pytest.param("damaged", dict(hours=36), "created", id="damaged-36h"),
    pytest.param("damaged", dict(hours=47, minutes=59, seconds=59), "created", id="damaged-48h-minus-1s"),
    pytest.param("damaged", dict(hours=48), "created", id="damaged-exactly-48h"),
    pytest.param("damaged", dict(hours=48, seconds=1), "outside_window", id="damaged-48h-plus-1s"),
    pytest.param("damaged", dict(hours=60), "outside_window", id="damaged-60h-inside-168h"),
    pytest.param("wrong_product", dict(hours=36), "created", id="wrong-36h"),
    pytest.param("wrong_product", dict(hours=47, minutes=59, seconds=59), "created", id="wrong-48h-minus-1s"),
    pytest.param("wrong_product", dict(hours=48), "created", id="wrong-exactly-48h"),
    pytest.param("wrong_product", dict(hours=48, seconds=1), "outside_window", id="wrong-48h-plus-1s"),
    pytest.param("wrong_product", dict(hours=60), "outside_window", id="wrong-60h-inside-168h"),
    pytest.param("standard", dict(), "created", id="at-the-delivery-instant"),
    pytest.param("standard", dict(seconds=-1), "outside_window", id="one-second-before-delivery"),
]


class TestEligibilityWindows:
    @pytest.mark.parametrize("category, offset, expected", WINDOW_CASES)
    def test_window(self, temp_db, category, offset, expected):
        result = request(category=category, reference=at(**offset))
        if expected == "created":
            assert set(result) == {"outcome", "return_id"}
            assert result["outcome"] == "created"
            assert RETURN_ID_RE.fullmatch(result["return_id"])
            assert temp_db.count_returns() == 1
        else:
            assert result == rejected("outside_window")
            assert temp_db.count_returns() == 0

    def test_standard_window_is_longer_than_damaged_window(self, temp_db):
        """60 hours: still fine for a standard return, too late for damaged."""
        assert request(category="damaged", reference=at(hours=60)) == rejected("outside_window")
        assert request(category="standard", reference=at(hours=60))["outcome"] == "created"

    def test_future_delivery_timestamp_is_outside_window(self, temp_db):
        temp_db.set_delivered_at("ORD1001", at(days=10))
        assert request(reference=at(days=3)) == rejected("outside_window")
        assert temp_db.count_returns() == 0

    def test_null_delivery_timestamp_is_outside_window(self, temp_db):
        # ORD1002 (C101, Shipped) has no delivered_at in the seed.
        assert request(order="ORD1002", item="IT102", reference=at(days=1)) == rejected("outside_window")
        # A NULL set explicitly on a Delivered order behaves the same.
        temp_db.set_delivered_at("ORD1001", None)
        assert request(reference=at(days=1)) == rejected("outside_window")
        assert temp_db.count_returns() == 0

    @pytest.mark.parametrize(
        "malformed",
        [
            "2026-1-4T0:0:0Z",            # non-zero-padded (strptime would accept)
            "2026-11-4T00:00:00Z",
            "2026-11-04T0:00:00Z",
            "2026-11-04T00:0:00Z",
            "2026-11-04T00:00:0Z",
            "2026-11-04t00:00:00z",       # lowercase (strptime would accept)
            "2026-11-04 00:00:00Z",       # space separator
            "2026-11-04T00:00:00",        # no Z
            "2026-11-04T00:00:00+00:00",  # numeric offset instead of Z
            "2026-11-04T00:00:00.000Z",   # fractional seconds
            " 2026-11-04T00:00:00Z",
            "2026-11-04T00:00:00Z ",
            "2026-11-04T00:00:00Z\n",
            "2026-11-04",
            "20261104T000000Z",
            "26-11-04T00:00:00Z",
            "2026-02-30T00:00:00Z",       # impossible date
            "2026-11-04T24:00:00Z",       # impossible time
            "2026-11-04T00:00:60Z",
            "٢٠٢٦-١١-٠٤T٠٠:٠٠:٠٠Z",  # non-ASCII digits
            "",
            "garbage",
        ],
    )
    def test_malformed_delivery_timestamp_is_outside_window(self, temp_db, malformed):
        temp_db.set_delivered_at("ORD1001", malformed)
        assert request(reference=at(days=1)) == rejected("outside_window")
        assert temp_db.count_returns() == 0

    def test_eligibility_is_decided_by_the_utc_instant_not_the_written_offset(self, temp_db):
        """The same instant written with a +05:30 offset gets the same answer."""
        ist = timezone(timedelta(hours=5, minutes=30))
        exact = (DELIVERED + timedelta(hours=48)).astimezone(ist).isoformat()
        late = (DELIVERED + timedelta(hours=48, seconds=1)).astimezone(ist).isoformat()
        assert request(category="damaged", reference=late) == rejected("outside_window")
        assert request(category="damaged", reference=exact)["outcome"] == "created"

    def test_boundary_uses_truncated_reference_time(self, temp_db):
        """48h + 0.999999s truncates to exactly 48h, so it is still eligible.

        (Rounding would give 48h + 1s and reject it.)
        """
        reference = DELIVERED + timedelta(hours=48, microseconds=999999)
        assert request(category="damaged", reference=reference)["outcome"] == "created"

    def test_a_second_past_the_boundary_after_truncation_is_rejected(self, temp_db):
        reference = DELIVERED + timedelta(hours=48, seconds=1, microseconds=1)
        assert request(category="damaged", reference=reference) == rejected("outside_window")


# ---------------------------------------------------------------------------
# Authorization and ownership
# ---------------------------------------------------------------------------

class TestOwnershipAndItems:
    def test_owned_order_and_item_creates_a_correct_row(self, temp_db):
        result = request(reason="Changed my mind", reference=at(days=3))
        assert result["outcome"] == "created"
        rows = temp_db.returns()
        assert len(rows) == 1
        assert rows[0] == {
            "return_id": result["return_id"],
            "customer_id": "C101",
            "order_id": "ORD1001",
            "item_id": "IT101",
            "reason": "Changed my mind",
            "status": "Requested",
            "requested_at": at(days=3),
        }

    def test_nonexistent_order_is_not_found(self, temp_db):
        assert request(order="ORD9999") == rejected("not_found")
        assert temp_db.count_returns() == 0

    def test_another_customers_order_is_not_found(self, temp_db):
        assert request(customer="C101", order="ORD1003", item="IT103") == rejected("not_found")
        assert temp_db.count_returns() == 0

    def test_unowned_and_nonexistent_orders_are_indistinguishable(self, temp_db):
        unowned = request(customer="C101", order="ORD1003", item="IT103")
        missing = request(customer="C101", order="ORD9999", item="IT103")
        assert unowned == missing == rejected("not_found")

    def test_nonexistent_customer_is_not_found_and_writes_nothing(self, temp_db):
        assert request(customer="C999") == rejected("not_found")
        assert temp_db.count_returns() == 0

    def test_order_ids_are_matched_exactly(self, temp_db):
        for variant in ("ord1001", " ORD1001", "ORD1001 ", "ORD 1001"):
            assert request(order=variant) == rejected("not_found")
        assert temp_db.count_returns() == 0

    @pytest.mark.parametrize(
        "item",
        [
            pytest.param("IT102", id="item-from-another-order-of-same-customer"),
            pytest.param("IT103", id="item-from-another-customers-order"),
            pytest.param("IT999", id="nonexistent-item"),
            pytest.param("it101", id="wrong-case-item-id"),
        ],
    )
    def test_item_not_in_the_order_is_item_mismatch(self, temp_db, item):
        assert request(order="ORD1001", item=item) == rejected("item_mismatch")
        assert temp_db.count_returns() == 0

    def test_item_mismatch_does_not_block_the_correct_item_afterwards(self, temp_db):
        assert request(item="IT102") == rejected("item_mismatch")
        assert request(item="IT101")["outcome"] == "created"

    def test_ownership_is_checked_before_the_item(self, temp_db):
        """Foreign order + wrong item reports not_found, never item_mismatch."""
        assert request(customer="C101", order="ORD1003", item="IT101") == rejected("not_found")

    def test_every_customer_is_denied_every_order_they_do_not_own(self, temp_db):
        """Exhaustive: no customer can write a return against anyone else's order."""
        attempts = 0
        for customer in CUSTOMERS:
            for order, owner in ORDER_OWNER.items():
                if owner == customer:
                    continue
                result = request(
                    customer=customer, order=order, item=ORDER_ITEMS[order][0],
                    category="standard", reference="2026-11-05T00:00:00Z",
                )
                assert result == rejected("not_found"), (customer, order)
                attempts += 1
        assert attempts == len(CUSTOMERS) * len(ORDER_OWNER) - len(ORDER_OWNER)
        assert temp_db.count_returns() == 0

    def test_a_successful_return_is_stored_only_under_the_authenticated_customer(self, temp_db):
        assert request(customer="C103", order="ORD1005", item="IT105",
                       category="damaged", reference="2026-11-07T00:00:00Z")["outcome"] == "created"
        # C101 and C102 then try the same order/item and get nothing.
        assert request(customer="C101", order="ORD1005", item="IT106",
                       category="damaged", reference="2026-11-07T00:00:00Z") == rejected("not_found")
        assert request(customer="C102", order="ORD1005", item="IT105",
                       category="damaged", reference="2026-11-07T00:00:00Z") == rejected("not_found")
        rows = temp_db.returns()
        assert [(r["customer_id"], r["order_id"], r["item_id"]) for r in rows] == [("C103", "ORD1005", "IT105")]

    def test_two_customers_can_each_return_their_own_items(self, temp_db):
        temp_db.set_delivered_at("ORD1003", at(days=2))  # C102's order, make it eligible
        a = request(customer="C101", order="ORD1001", item="IT101", reference=at(days=3))
        b = request(customer="C102", order="ORD1003", item="IT103", reference=at(days=3))
        assert a["outcome"] == b["outcome"] == "created"
        assert a["return_id"] != b["return_id"]
        owners = {r["return_id"]: r["customer_id"] for r in temp_db.returns()}
        assert owners == {a["return_id"]: "C101", b["return_id"]: "C102"}

    def test_two_items_of_one_order_are_independent(self, temp_db):
        ref = "2026-11-07T00:00:00Z"
        first = request(customer="C103", order="ORD1005", item="IT105", category="damaged", reference=ref)
        second = request(customer="C103", order="ORD1005", item="IT106", category="damaged", reference=ref)
        assert first["outcome"] == second["outcome"] == "created"
        assert temp_db.count_returns() == 2


# ---------------------------------------------------------------------------
# Duplicate handling
# ---------------------------------------------------------------------------

class TestDuplicates:
    def test_duplicate_of_a_requested_return(self, temp_db):
        first = request(reference=at(days=1))
        assert first["outcome"] == "created"
        assert request(reference=at(days=2)) == rejected("duplicate")
        assert temp_db.count_returns() == 1

    def test_duplicate_of_an_approved_return(self, temp_db):
        request(reference=at(days=1))
        temp_db.set_return_status("Approved")
        assert request(reference=at(days=2)) == rejected("duplicate")
        assert temp_db.count_returns() == 1

    @pytest.mark.parametrize("status", ["Rejected", "Completed"])
    def test_new_return_allowed_after_previous_one_is_closed(self, temp_db, status):
        first = request(reference=at(days=1))
        temp_db.set_return_status(status)
        second = request(reference=at(days=2))
        assert second["outcome"] == "created"
        assert second["return_id"] != first["return_id"]
        statuses = {r["return_id"]: r["status"] for r in temp_db.returns()}
        assert statuses == {first["return_id"]: status, second["return_id"]: "Requested"}

    def test_a_third_request_after_the_second_is_a_duplicate_again(self, temp_db):
        request(reference=at(days=1))
        temp_db.set_return_status("Rejected")
        assert request(reference=at(days=2))["outcome"] == "created"
        assert request(reference=at(days=3)) == rejected("duplicate")
        assert temp_db.count_returns() == 2

    def test_duplicate_is_reported_before_an_expired_window(self, temp_db):
        request(reference=at(days=1))
        assert request(reference=at(days=30)) == rejected("duplicate")

    def test_duplicate_is_reported_for_any_category(self, temp_db):
        request(category="standard", reference=at(days=1))
        assert request(category="damaged", reference=at(hours=2)) == rejected("duplicate")

    def test_duplicate_precheck_runs_after_ownership_and_item_checks(self, temp_db):
        request(reference=at(days=1))
        assert request(customer="C102", reference=at(days=1)) == rejected("not_found")
        assert request(item="IT102", reference=at(days=1)) == rejected("item_mismatch")

    def test_unique_index_violation_at_insert_becomes_duplicate(self, temp_db):
        """With the pre-check neutralised, the partial unique index still maps to duplicate."""
        request(reference=at(days=1))
        proxies = temp_db.use_faulty_connection(
            rewrite=lambda sql: "SELECT 1 WHERE 0" if "FROM return_requests" in sql else None
        )
        result = request(reference=at(days=2))
        assert result == rejected("duplicate")
        assert "INSERT" in proxies[0].log           # the INSERT really was attempted
        assert "COMMIT" not in proxies[0].log       # and rolled back
        assert temp_db.count_returns() == 1

    def test_other_integrity_errors_are_failed_not_duplicate(self, temp_db, monkeypatch):
        """A return_id collision is an IntegrityError too, but not the active-return index."""
        import uuid

        monkeypatch.setattr(repository.uuid, "uuid4", lambda: uuid.UUID("12345678-1234-4123-8123-123456789abc"))
        ref = "2026-11-07T00:00:00Z"
        first = request(customer="C103", order="ORD1005", item="IT105", category="damaged", reference=ref)
        assert first == {"outcome": "created", "return_id": "RET-123456781234"}
        collision = request(customer="C103", order="ORD1005", item="IT106", category="damaged", reference=ref)
        assert collision == FAILED
        assert temp_db.count_returns() == 1


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

VALID = dict(
    authenticated_customer_id="C101", order_id="ORD1001", item_id="IT101",
    reason="Item not as described", reason_category="standard",
    reference_time="2026-11-05T00:00:00Z",
)

TYPE_ERRORS = [
    pytest.param(dict(authenticated_customer_id=101), id="customer-int"),
    pytest.param(dict(authenticated_customer_id=None), id="customer-None"),
    pytest.param(dict(order_id=None), id="order-None"),
    pytest.param(dict(order_id=1001), id="order-int"),
    pytest.param(dict(item_id=5), id="item-int"),
    pytest.param(dict(item_id=b"IT101"), id="item-bytes"),
    pytest.param(dict(reason=None), id="reason-None"),
    pytest.param(dict(reason=["x"]), id="reason-list"),
    pytest.param(dict(reason_category=None), id="category-None"),
    pytest.param(dict(reason_category=1), id="category-int"),
    pytest.param(dict(reason_category=True), id="category-bool"),
    pytest.param(dict(reference_time=12345), id="reference-int"),
    pytest.param(dict(reference_time=3.5), id="reference-float"),
    pytest.param(dict(reference_time=True), id="reference-bool"),
    pytest.param(dict(reference_time=b"2026-11-05T00:00:00Z"), id="reference-bytes"),
    pytest.param(dict(reference_time=date(2026, 11, 5)), id="reference-date-not-datetime"),
    pytest.param(dict(reference_time=["2026-11-05T00:00:00Z"]), id="reference-list"),
]

VALUE_ERRORS = [
    pytest.param(dict(authenticated_customer_id=""), id="customer-empty"),
    pytest.param(dict(authenticated_customer_id="   "), id="customer-blank"),
    pytest.param(dict(order_id=""), id="order-empty"),
    pytest.param(dict(order_id=" \t\n"), id="order-whitespace"),
    pytest.param(dict(item_id=""), id="item-empty"),
    pytest.param(dict(item_id="  "), id="item-blank"),
    pytest.param(dict(reason=""), id="reason-empty"),
    pytest.param(dict(reason="   \t"), id="reason-blank"),
    pytest.param(dict(reason_category=""), id="category-empty"),
    pytest.param(dict(reason_category="refund"), id="category-unknown"),
    pytest.param(dict(reason_category="Damaged"), id="category-wrong-case"),
    pytest.param(dict(reason_category="STANDARD"), id="category-upper"),
    pytest.param(dict(reason_category=" damaged"), id="category-leading-space"),
    pytest.param(dict(reason_category="damaged "), id="category-trailing-space"),
    pytest.param(dict(reason_category="wrong-product"), id="category-hyphen"),
    pytest.param(dict(reference_time=datetime(2026, 11, 5)), id="reference-naive-datetime"),
    pytest.param(dict(reference_time="2026-11-05T00:00:00"), id="reference-string-no-offset"),
    pytest.param(dict(reference_time="2026-11-05"), id="reference-date-only-string"),
    pytest.param(dict(reference_time=""), id="reference-empty-string"),
    pytest.param(dict(reference_time="not a time"), id="reference-garbage"),
    pytest.param(dict(reference_time="2026-13-45T00:00:00Z"), id="reference-impossible-date"),
]


class TestValidation:
    @pytest.mark.parametrize("override", TYPE_ERRORS)
    def test_wrong_type_raises_type_error(self, temp_db, override):
        with pytest.raises(TypeError):
            create_return_request(**{**VALID, **override})
        assert temp_db.repository_connections == []     # no connection was opened
        assert temp_db.count_returns() == 0

    @pytest.mark.parametrize("override", VALUE_ERRORS)
    def test_invalid_value_raises_value_error(self, temp_db, override):
        with pytest.raises(ValueError):
            create_return_request(**{**VALID, **override})
        assert temp_db.repository_connections == []
        assert temp_db.count_returns() == 0

    def test_type_error_is_not_a_value_error_and_vice_versa(self, temp_db):
        with pytest.raises(TypeError) as type_info:
            create_return_request(**{**VALID, "reason": None})
        assert not isinstance(type_info.value, ValueError)
        with pytest.raises(ValueError) as value_info:
            create_return_request(**{**VALID, "reason": " "})
        assert not isinstance(value_info.value, TypeError)

    def test_reason_category_is_required_and_keyword_only(self, temp_db):
        with pytest.raises(TypeError):
            create_return_request("C101", "ORD1001", "IT101", "x")
        with pytest.raises(TypeError):
            create_return_request("C101", "ORD1001", "IT101", "x", "standard")
        assert temp_db.count_returns() == 0

    def test_reference_time_is_keyword_only(self, temp_db):
        with pytest.raises(TypeError):
            create_return_request("C101", "ORD1001", "IT101", "x", "standard", "2026-11-05T00:00:00Z")

    def test_reason_is_stored_verbatim_including_whitespace(self, temp_db):
        reason = "  line one\nline two é中  "
        assert request(reason=reason)["outcome"] == "created"
        assert temp_db.returns()[0]["reason"] == reason

    def test_reason_content_does_not_affect_the_window(self, temp_db):
        """The free-text reason is never interpreted; only reason_category matters."""
        assert request(reason="it arrived damaged", category="standard", reference=at(hours=60))["outcome"] == "created"
        temp_db.set_return_status("Rejected")
        assert request(reason="standard return", category="damaged", reference=at(hours=60)) == rejected("outside_window")

    @pytest.mark.parametrize("category", ["damaged", "wrong_product", "standard"])
    def test_each_valid_category_is_accepted(self, temp_db, category):
        assert request(category=category, reference=at(hours=2))["outcome"] == "created"

    @pytest.mark.parametrize("reference", [
        "2026-11-05T00:00:00Z",
        "2026-11-05T00:00:00+00:00",
        "2026-11-05T05:30:00+05:30",
        "2026-11-04T16:00:00-08:00",
        "2026-11-05T00:00:00.123456Z",
    ])
    def test_valid_aware_reference_strings_are_accepted(self, temp_db, reference):
        result = request(reference=reference)
        assert result["outcome"] == "created"
        assert temp_db.returns()[0]["requested_at"] == "2026-11-05T00:00:00Z"


# ---------------------------------------------------------------------------
# Reference time and clock
# ---------------------------------------------------------------------------

def _install_fake_clock(monkeypatch, instants: list[datetime]) -> list[Any]:
    """Patch the repository's ``datetime`` so ``now()`` walks through *instants*."""
    calls: list[Any] = []
    real = repository.datetime

    class FakeDatetime(real):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            return instants[min(len(calls) - 1, len(instants) - 1)]

    monkeypatch.setattr(repository, "datetime", FakeDatetime)
    return calls


class TestReferenceTime:
    def test_requested_at_is_the_normalized_reference_time(self, temp_db):
        assert request(reference="2026-11-05T05:30:00+05:30")["outcome"] == "created"
        assert temp_db.returns()[0]["requested_at"] == "2026-11-05T00:00:00Z"

    def test_aware_datetime_in_another_zone_is_converted_to_utc(self, temp_db):
        pacific = timezone(timedelta(hours=-8))
        reference = datetime(2026, 11, 4, 16, 0, 0, tzinfo=pacific)  # = 2026-11-05T00:00:00Z
        assert request(reference=reference)["outcome"] == "created"
        assert temp_db.returns()[0]["requested_at"] == "2026-11-05T00:00:00Z"

    def test_stored_timestamp_has_the_approved_format(self, temp_db):
        request(reference=at(days=2, hours=3, minutes=4, seconds=5))
        stored = temp_db.returns()[0]["requested_at"]
        assert STORED_TS_RE.fullmatch(stored)
        assert stored == at(days=2, hours=3, minutes=4, seconds=5)

    @pytest.mark.parametrize("micro", [1, 500000, 999999])
    def test_microseconds_are_truncated_not_rounded(self, temp_db, micro):
        reference = datetime(2026, 11, 5, 0, 0, 0, micro, tzinfo=UTC)
        assert request(reference=reference)["outcome"] == "created"
        assert temp_db.returns()[0]["requested_at"] == "2026-11-05T00:00:00Z"

    def test_fractional_seconds_in_a_string_are_truncated_too(self, temp_db):
        assert request(reference="2026-11-05T00:00:59.999999Z")["outcome"] == "created"
        assert temp_db.returns()[0]["requested_at"] == "2026-11-05T00:00:59Z"

    def test_omitted_reference_reads_the_clock_exactly_once(self, temp_db, monkeypatch):
        first = datetime(2026, 11, 6, 0, 0, 0, 987654, tzinfo=UTC)  # = delivery + 48h + 0.987654s
        calls = _install_fake_clock(monkeypatch, [first + timedelta(hours=h) for h in range(5)])
        result = create_return_request(
            "C101", "ORD1001", "IT101", "x", reason_category="damaged",
        )
        assert len(calls) == 1
        assert calls[0] is UTC or calls[0] == timezone.utc
        # Eligibility used the truncated first reading (exactly 48h -> eligible)...
        assert result["outcome"] == "created"
        # ...and requested_at is that same instant, truncated.
        assert temp_db.returns()[0]["requested_at"] == "2026-11-06T00:00:00Z"

    def test_explicit_none_also_reads_the_clock_exactly_once(self, temp_db, monkeypatch):
        first = datetime(2026, 11, 5, 0, 0, 0, tzinfo=UTC)
        calls = _install_fake_clock(monkeypatch, [first, first + timedelta(days=30)])
        assert request(reference=None)["outcome"] == "created"
        assert len(calls) == 1

    def test_a_clock_that_advances_between_reads_cannot_split_the_instant(self, temp_db, monkeypatch):
        """First reading is eligible; any later reading would not be."""
        first = datetime(2026, 11, 5, 0, 0, 0, tzinfo=UTC)
        _install_fake_clock(monkeypatch, [first, first + timedelta(days=30), first + timedelta(days=60)])
        assert request(reference=None)["outcome"] == "created"
        assert temp_db.returns()[0]["requested_at"] == "2026-11-05T00:00:00Z"

    def test_supplied_reference_never_reads_the_clock(self, temp_db, monkeypatch):
        calls = _install_fake_clock(monkeypatch, [datetime(2030, 1, 1, tzinfo=UTC)])
        assert request(reference="2026-11-05T00:00:00Z")["outcome"] == "created"
        assert calls == []

    def test_validation_failure_with_supplied_reference_never_reads_the_clock(self, temp_db, monkeypatch):
        calls = _install_fake_clock(monkeypatch, [datetime(2030, 1, 1, tzinfo=UTC)])
        with pytest.raises(ValueError):
            request(reference="garbage")
        assert calls == []

    def test_default_reference_is_the_real_utc_clock(self, temp_db):
        """No fake clock: delivered_at is built relative to now, so this never goes stale."""
        before = datetime.now(UTC).replace(microsecond=0)
        temp_db.set_delivered_at("ORD1001", (before - timedelta(hours=1)).strftime(TS_FORMAT))
        result = create_return_request(
            "C101", "ORD1001", "IT101", "x", reason_category="damaged",
        )
        after = datetime.now(UTC)
        assert result["outcome"] == "created"
        stored = temp_db.returns()[0]["requested_at"]
        assert STORED_TS_RE.fullmatch(stored)
        stored_dt = datetime.strptime(stored, TS_FORMAT).replace(tzinfo=UTC)
        assert before <= stored_dt <= after


# ---------------------------------------------------------------------------
# Result shapes and IDs
# ---------------------------------------------------------------------------

def _scenario_not_found(db):
    return request(order="ORD9999"), "not_found"


def _scenario_item_mismatch(db):
    return request(item="IT102"), "item_mismatch"


def _scenario_outside_window(db):
    return request(reference=at(days=10)), "outside_window"


def _scenario_duplicate(db):
    request(reference=at(days=1))
    return request(reference=at(days=2)), "duplicate"


def _scenario_failed(db):
    db.use_faulty_connection(fail_on=("INSERT", sqlite3.OperationalError("disk I/O error")))
    return request(), "failed"


REJECTION_SCENARIOS = [
    pytest.param(_scenario_not_found, id="not_found"),
    pytest.param(_scenario_item_mismatch, id="item_mismatch"),
    pytest.param(_scenario_outside_window, id="outside_window"),
    pytest.param(_scenario_duplicate, id="duplicate"),
    pytest.param(_scenario_failed, id="failed"),
]


class TestResultShapes:
    def test_created_result_shape_and_id_format(self, temp_db):
        result = request()
        assert set(result) == {"outcome", "return_id"}
        assert result["outcome"] == "created"
        assert RETURN_ID_RE.fullmatch(result["return_id"])
        assert temp_db.returns()[0]["return_id"] == result["return_id"]

    def test_return_id_is_prefix_plus_first_12_uppercase_hex_of_a_uuid4(self, temp_db, monkeypatch):
        import uuid

        monkeypatch.setattr(repository.uuid, "uuid4", lambda: uuid.UUID("abcdef01-2345-4678-89ab-cdef01234567"))
        assert request()["return_id"] == "RET-ABCDEF012345"

    def test_ids_are_unique_across_returns(self, temp_db):
        ids = set()
        for order, item in (("ORD1005", "IT105"), ("ORD1005", "IT106")):
            ids.add(request(customer="C103", order=order, item=item, category="damaged",
                            reference="2026-11-07T00:00:00Z")["return_id"])
        ids.add(request()["return_id"])
        assert len(ids) == 3

    @pytest.mark.parametrize("scenario", REJECTION_SCENARIOS)
    def test_rejected_outcomes_have_exact_shape_and_write_nothing(self, temp_db, scenario):
        result, outcome = scenario(temp_db)
        assert result == {"outcome": outcome, "return_id": None}
        assert list(result) == ["outcome", "return_id"]
        expected_rows = 1 if outcome == "duplicate" else 0
        assert temp_db.count_returns() == expected_rows

    def test_only_created_carries_a_return_id(self, temp_db):
        # outside_window must come before the first successful return: once an
        # active return exists the contract reports duplicate before eligibility.
        results = [
            request(reference=at(days=10)),
            request(order="ORD9999"),
            request(item="IT102"),
            request(),
            request(),  # duplicate of the one just created
        ]
        assert [r["outcome"] for r in results] == [
            "outside_window", "not_found", "item_mismatch", "created", "duplicate",
        ]
        assert [r["return_id"] is not None for r in results] == [False, False, False, True, False]

    def test_results_are_independent_dicts(self, temp_db):
        first = request(order="ORD9999")
        first["outcome"] = "tampered"
        assert request(order="ORD9999") == rejected("not_found")

    def test_failed_results_are_independent_dicts(self, temp_db, monkeypatch):
        def boom():
            raise ConnectionError_("nope")
        monkeypatch.setattr(repository, "get_connection", boom)
        first = request()
        first["outcome"] = "tampered"
        assert request() == FAILED

    def test_no_rejected_outcome_leaks_other_customers_data(self, temp_db):
        results = [
            request(customer="C101", order="ORD1003", item="IT103"),
            request(customer="C101", order="ORD9999", item="IT103"),
        ]
        for result in results:
            assert "C102" not in repr(result) and "ORD1003" not in repr(result)


# ---------------------------------------------------------------------------
# Transaction behaviour and database failures
# ---------------------------------------------------------------------------

LEAKY = re.compile(r"sqlite|no such table|disk i/o|constraint|foreign key|traceback|secret|\.db", re.IGNORECASE)


class TestTransactions:
    def test_uses_begin_immediate_and_commits_after_the_insert(self, temp_db):
        proxies = temp_db.use_faulty_connection()
        result = request()
        assert result["outcome"] == "created"
        log = proxies[0].log
        assert log[0] == "BEGIN IMMEDIATE"
        assert log.index("INSERT") < log.index("COMMIT")
        assert log[-2:] == ["COMMIT", "CLOSE"]
        assert "ROLLBACK" not in log

    def test_all_statements_are_parameterized(self, temp_db):
        """Caller-supplied values never appear in the SQL text, only as bound parameters."""
        proxies = temp_db.use_faulty_connection()
        request(reason="REASON_MARKER", reference=at(days=3))
        assert any(s.lstrip().upper().startswith("INSERT") for s in proxies[0].sql)
        for statement in proxies[0].sql:
            for marker in ("REASON_MARKER", "ORD1001", "IT101", "C101", "damaged", "standard"):
                assert marker not in statement

    def test_row_is_not_visible_to_others_until_commit(self, temp_db):
        seen_before_commit: list[int] = []
        temp_db.use_faulty_connection(
            before_commit=lambda: seen_before_commit.append(temp_db.count_returns())
        )
        assert request()["outcome"] == "created"
        assert seen_before_commit == [0]       # uncommitted insert invisible
        assert temp_db.count_returns() == 1    # visible after commit

    def test_success_is_not_reported_when_commit_fails(self, temp_db):
        proxies = temp_db.use_faulty_connection(commit_exc=sqlite3.OperationalError("disk I/O error"))
        result = request()
        assert result == FAILED
        assert result["outcome"] != "created"
        assert temp_db.count_returns() == 0
        assert "ROLLBACK" in proxies[0].log and proxies[0].log[-1] == "CLOSE"

    @pytest.mark.parametrize("outcome_request", [
        pytest.param(dict(order="ORD9999"), id="not_found"),
        pytest.param(dict(item="IT102"), id="item_mismatch"),
        pytest.param(dict(reference=at(days=10)), id="outside_window"),
    ])
    def test_business_rejection_rolls_back_and_never_commits(self, temp_db, outcome_request):
        proxies = temp_db.use_faulty_connection()
        request(**outcome_request)
        log = proxies[0].log
        assert log[0] == "BEGIN IMMEDIATE"
        assert "COMMIT" not in log and "INSERT" not in log
        assert log[-2:] == ["ROLLBACK", "CLOSE"]

    def test_duplicate_rejection_rolls_back_and_never_commits(self, temp_db):
        request(reference=at(days=1))
        proxies = temp_db.use_faulty_connection()
        assert request(reference=at(days=2)) == rejected("duplicate")
        assert "COMMIT" not in proxies[0].log and "INSERT" not in proxies[0].log

    @pytest.mark.parametrize("verb", ["BEGIN", "SELECT", "INSERT"])
    def test_sqlite_error_at_any_stage_returns_generic_failed(self, temp_db, verb):
        proxies = temp_db.use_faulty_connection(
            fail_on=(verb, sqlite3.OperationalError("disk I/O error at /secret/path.db"))
        )
        result = request()
        assert result == FAILED
        assert not LEAKY.search(repr(result))
        assert temp_db.count_returns() == 0
        assert proxies[0].log[-1] == "CLOSE"

    @pytest.mark.parametrize("error", [
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.OperationalError("database is locked"),
        sqlite3.IntegrityError("FOREIGN KEY constraint failed"),
        sqlite3.ProgrammingError("Incorrect number of bindings supplied"),
    ], ids=lambda e: type(e).__name__)
    def test_any_sqlite_error_family_is_generic_failed(self, temp_db, error):
        temp_db.use_faulty_connection(fail_on=("INSERT", error))
        result = request()
        assert result == FAILED
        assert str(error) not in repr(result)

    def test_missing_schema_returns_generic_failed(self, empty_db):
        result = request()
        assert result == FAILED
        assert not LEAKY.search(repr(result))

    def test_write_lock_held_by_another_connection_returns_failed_then_recovers(self, temp_db):
        temp_db.use_zero_busy_timeout()
        holder = temp_db.open()
        holder.execute("BEGIN IMMEDIATE")
        try:
            result = request()
        finally:
            holder.rollback()
            holder.close()
        assert result == FAILED
        assert temp_db.count_returns() == 0
        assert request()["outcome"] == "created"   # nothing was left locked or half-done

    # ---- connection acquisition ------------------------------------------

    def test_connection_error_on_acquisition_returns_generic_failed(self, temp_db, monkeypatch):
        def boom():
            raise ConnectionError_("Failed to open SQLite connection at '/secret/path.db': unable to open")

        monkeypatch.setattr(repository, "get_connection", boom)
        result = request()
        assert result == FAILED
        assert not LEAKY.search(repr(result))

    def test_connection_error_is_not_propagated(self, temp_db, monkeypatch):
        def boom():
            raise ConnectionError_("x")

        monkeypatch.setattr(repository, "get_connection", boom)
        try:
            request()
        except ConnectionError_:  # pragma: no cover - failure path
            pytest.fail("ConnectionError_ must not propagate to the caller")

    def test_other_exceptions_from_acquisition_are_not_swallowed(self, temp_db, monkeypatch):
        def boom():
            raise RuntimeError("unexpected programming error")

        monkeypatch.setattr(repository, "get_connection", boom)
        with pytest.raises(RuntimeError, match="unexpected programming error"):
            request()

    def test_validation_errors_win_over_connection_failures(self, temp_db, monkeypatch):
        def boom():  # pragma: no cover - must not be reached
            raise ConnectionError_("x")

        monkeypatch.setattr(repository, "get_connection", boom)
        with pytest.raises(ValueError):
            request(reason="  ")

    # ---- non-database exceptions ----------------------------------------

    @pytest.mark.parametrize("verb", ["SELECT", "INSERT"])
    def test_non_database_exception_is_reraised_after_rollback(self, temp_db, verb):
        proxies = temp_db.use_faulty_connection(fail_on=(verb, RuntimeError("programming bug")))
        with pytest.raises(RuntimeError, match="programming bug"):
            request()
        log = proxies[0].log
        assert "ROLLBACK" in log and "COMMIT" not in log
        assert log[-1] == "CLOSE"
        assert temp_db.count_returns() == 0

    def test_database_is_still_writable_after_a_reraised_exception(self, temp_db):
        temp_db.use_faulty_connection(fail_on=("SELECT", RuntimeError("programming bug")))
        with pytest.raises(RuntimeError):
            request()
        # A fresh write transaction can start immediately: no lock was left behind.
        other = temp_db.open()
        other.execute("PRAGMA busy_timeout = 0")
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        other.close()

    def test_exception_type_and_message_are_preserved(self, temp_db):
        class CustomError(Exception):
            pass

        error = CustomError("custom")
        temp_db.use_faulty_connection(fail_on=("INSERT", error))
        with pytest.raises(CustomError) as info:
            request()
        assert info.value is error

    # ---- rollback and close failures -------------------------------------

    def test_rollback_failure_does_not_mask_the_original_exception(self, temp_db):
        temp_db.use_faulty_connection(
            fail_on=("SELECT", RuntimeError("original bug")),
            rollback_exc=sqlite3.OperationalError("rollback boom"),
        )
        with pytest.raises(RuntimeError, match="original bug") as info:
            request()
        assert "rollback boom" not in str(info.value)

    def test_rollback_failure_on_a_database_error_still_returns_generic_failed(self, temp_db):
        temp_db.use_faulty_connection(
            commit_exc=sqlite3.OperationalError("disk I/O error"),
            rollback_exc=sqlite3.OperationalError("rollback boom"),
        )
        assert request() == FAILED

    def test_rollback_failure_never_reports_success(self, temp_db):
        temp_db.use_faulty_connection(
            fail_on=("INSERT", sqlite3.OperationalError("disk I/O error")),
            rollback_exc=sqlite3.OperationalError("rollback boom"),
        )
        result = request()
        assert result == FAILED and temp_db.count_returns() == 0

    def test_close_failure_does_not_turn_a_committed_write_into_an_error(self, temp_db):
        temp_db.use_faulty_connection(close_exc=sqlite3.OperationalError("close boom"))
        result = request()
        assert result["outcome"] == "created"
        assert temp_db.count_returns() == 1

    def test_close_failure_does_not_mask_a_reraised_exception(self, temp_db):
        temp_db.use_faulty_connection(
            fail_on=("SELECT", RuntimeError("original bug")),
            close_exc=sqlite3.OperationalError("close boom"),
        )
        with pytest.raises(RuntimeError, match="original bug"):
            request()

    # ---- connection lifecycle --------------------------------------------

    @pytest.mark.parametrize("scenario", [
        pytest.param(lambda db: request(), id="created"),
        pytest.param(lambda db: request(order="ORD9999"), id="not_found"),
        pytest.param(lambda db: request(item="IT102"), id="item_mismatch"),
        pytest.param(lambda db: request(reference=at(days=10)), id="outside_window"),
    ])
    def test_connection_is_always_closed(self, temp_db, scenario):
        scenario(temp_db)
        assert len(temp_db.repository_connections) == 1
        assert_closed(temp_db.repository_connections[0])

    def test_connection_is_closed_after_a_duplicate(self, temp_db):
        request(reference=at(days=1))
        temp_db.repository_connections.clear()
        assert request(reference=at(days=2)) == rejected("duplicate")
        assert len(temp_db.repository_connections) == 1
        assert_closed(temp_db.repository_connections[0])

    def test_one_connection_per_call_and_none_left_open(self, temp_db):
        request()
        request(order="ORD9999")
        assert len(temp_db.repository_connections) == 2
        for connection in temp_db.repository_connections:
            assert_closed(connection)
