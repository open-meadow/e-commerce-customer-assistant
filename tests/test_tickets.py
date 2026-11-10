"""Tests for ``create_support_ticket`` (Checkpoint 8).

Contract under test: docs/01_database_design.md Sections G and H and
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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

import app.db.repository as repository
from app.db.connection import DEFAULT_DB_PATH, ConnectionError_, get_connection
from app.db.repository import create_support_ticket, get_customer_tickets
from app.db.schema import create_schema
from app.db.seed_data import seed_database

UTC = timezone.utc
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# ---------------------------------------------------------------------------
# Seed facts (app/db/seed_data.py)
# ---------------------------------------------------------------------------

ORDER_OWNER: dict[str, str] = {
    "ORD1001": "C101", "ORD1002": "C101", "ORD1014": "C101",
    "ORD1003": "C102", "ORD1004": "C102", "ORD1015": "C102",
    "ORD1005": "C103", "ORD1006": "C103",
    "ORD1007": "C104", "ORD1008": "C104", "ORD1009": "C104",
    "ORD1010": "C105", "ORD1011": "C105",
    "ORD1012": "C106", "ORD1013": "C106",
}
CUSTOMERS = sorted(set(ORDER_OWNER.values()))

TICKET_ID_RE = re.compile(r"TKT-[0-9A-F]{12}")
STORED_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

FAILED = {"outcome": "failed", "ticket_id": None, "order_id": None, "order_linked": False}
RESULT_KEYS = {"outcome", "ticket_id", "order_id", "order_linked"}


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
    ) -> None:
        self.real = real
        self.log: list[str] = []
        self.sql: list[str] = []
        self._fail_on = fail_on
        self._commit_exc = commit_exc
        self._rollback_exc = rollback_exc
        self._close_exc = close_exc
        self._before_commit = before_commit

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

    def tickets(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM support_tickets ORDER BY ticket_id")

    def count_tickets(self) -> int:
        return self.query("SELECT COUNT(*) AS n FROM support_tickets")[0]["n"]

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
    db = TempDatabase(str(tmp_path / "tickets_test.db"), monkeypatch)
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


def ticket(
    *,
    customer: str = "C101",
    issue_type: str = "Order issue",
    description: str = "Where is my order?",
    order: Optional[str] = None,
    summary: Optional[str] = None,
) -> dict[str, Any]:
    return create_support_ticket(
        customer, issue_type, description,
        order_id=order, conversation_summary=summary,
    )


def without_id(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k != "ticket_id"}


def assert_closed(connection: Any) -> None:
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


# ---------------------------------------------------------------------------
# Harness canaries
# ---------------------------------------------------------------------------

class TestHarness:
    def test_seed_ownership_the_tests_rely_on(self, temp_db):
        rows = temp_db.query("SELECT order_id, customer_id FROM orders")
        assert {r["order_id"]: r["customer_id"] for r in rows} == ORDER_OWNER

    def test_no_tickets_before_any_write(self, temp_db):
        assert temp_db.count_tickets() == 0

    def test_temp_database_is_not_the_real_database(self, temp_db):
        assert os.path.abspath(temp_db.path) != os.path.abspath(DEFAULT_DB_PATH)

    def test_guard_blocks_the_real_database_path(self):
        with pytest.raises(AssertionError, match="real database"):
            sqlite3.connect(DEFAULT_DB_PATH)


# ---------------------------------------------------------------------------
# Order linking
# ---------------------------------------------------------------------------

class TestOrderLinking:
    def test_owned_order_is_linked(self, temp_db):
        result = ticket(order="ORD1001")
        assert result == {
            "outcome": "created",
            "ticket_id": result["ticket_id"],
            "order_id": "ORD1001",
            "order_linked": True,
        }
        stored = temp_db.tickets()
        assert len(stored) == 1
        assert stored[0]["order_id"] == "ORD1001"
        assert stored[0]["customer_id"] == "C101"
        assert stored[0]["ticket_id"] == result["ticket_id"]

    def test_no_order_id_creates_a_general_ticket(self, temp_db):
        result = ticket(order=None)
        assert result == {
            "outcome": "created",
            "ticket_id": result["ticket_id"],
            "order_id": None,
            "order_linked": False,
        }
        assert temp_db.tickets()[0]["order_id"] is None

    def test_general_ticket_runs_no_ownership_query(self, temp_db):
        proxies = temp_db.use_faulty_connection()
        ticket(order=None)
        assert proxies[0].log == ["BEGIN IMMEDIATE", "INSERT", "COMMIT", "CLOSE"]

    def test_order_ownership_is_checked_inside_the_transaction(self, temp_db):
        proxies = temp_db.use_faulty_connection()
        ticket(order="ORD1001")
        assert proxies[0].log == ["BEGIN IMMEDIATE", "SELECT", "INSERT", "COMMIT", "CLOSE"]

    def test_nonexistent_order_creates_a_general_ticket(self, temp_db):
        result = ticket(order="ORD9999")
        assert result["outcome"] == "created"
        assert result["order_id"] is None and result["order_linked"] is False
        assert temp_db.tickets()[0]["order_id"] is None

    def test_another_customers_order_creates_a_general_ticket(self, temp_db):
        result = ticket(customer="C101", order="ORD1003")
        assert result["outcome"] == "created"
        assert result["order_id"] is None and result["order_linked"] is False
        stored = temp_db.tickets()
        assert len(stored) == 1
        assert stored[0]["customer_id"] == "C101" and stored[0]["order_id"] is None

    def test_unowned_and_nonexistent_orders_behave_identically(self, temp_db):
        unowned = ticket(customer="C101", order="ORD1003")
        missing = ticket(customer="C101", order="ORD9999")
        assert without_id(unowned) == without_id(missing)
        assert list(unowned) == list(missing)
        stored = {t["ticket_id"]: t for t in temp_db.tickets()}
        for result in (unowned, missing):
            row = stored[result["ticket_id"]]
            assert row["order_id"] is None and row["customer_id"] == "C101"
            assert row["status"] == "Open" and row["priority"] == "Normal"

    def test_result_never_mentions_the_unowned_order(self, temp_db):
        result = ticket(customer="C101", order="ORD1003")
        assert "ORD1003" not in repr(result)
        assert "C102" not in repr(result)

    def test_every_customer_links_each_of_their_own_orders(self, temp_db):
        for order, owner in ORDER_OWNER.items():
            result = ticket(customer=owner, order=order)
            assert result["order_id"] == order and result["order_linked"] is True, order
        assert temp_db.count_tickets() == len(ORDER_OWNER)

    def test_no_customer_can_link_an_order_they_do_not_own(self, temp_db):
        """Exhaustive: every customer x every foreign order yields an unlinked ticket."""
        attempts = 0
        for customer in CUSTOMERS:
            for order, owner in ORDER_OWNER.items():
                if owner == customer:
                    continue
                result = ticket(customer=customer, order=order)
                assert result["outcome"] == "created", (customer, order)
                assert result["order_id"] is None and result["order_linked"] is False, (customer, order)
                attempts += 1
        assert attempts == len(CUSTOMERS) * len(ORDER_OWNER) - len(ORDER_OWNER)
        stored = temp_db.tickets()
        assert len(stored) == attempts
        assert all(row["order_id"] is None for row in stored)

    def test_foreign_order_ids_never_appear_in_stored_tickets(self, temp_db):
        ticket(customer="C101", order="ORD1003")
        ticket(customer="C102", order="ORD1001")
        for row in temp_db.tickets():
            assert row["order_id"] is None
        assert [t["customer_id"] for t in get_customer_tickets("C101")] == ["C101"]
        assert [t["customer_id"] for t in get_customer_tickets("C102")] == ["C102"]

    def test_tickets_are_stored_only_under_the_authenticated_customer(self, temp_db):
        ticket(customer="C103", order="ORD1005")
        assert get_customer_tickets("C101") == []
        assert get_customer_tickets("C102") == []
        assert [t["customer_id"] for t in get_customer_tickets("C103")] == ["C103"]

    def test_order_ids_are_matched_exactly(self, temp_db):
        for variant in ("ord1001", " ORD1001", "ORD1001 ", "ORD 1001"):
            result = ticket(order=variant)
            assert result["order_id"] is None and result["order_linked"] is False, variant
        assert all(row["order_id"] is None for row in temp_db.tickets())

    def test_order_link_does_not_depend_on_the_orders_status(self, temp_db):
        """Cancelled (ORD1006) and Processing (ORD1004) orders are still linkable by the owner."""
        assert ticket(customer="C103", order="ORD1006")["order_linked"] is True
        assert ticket(customer="C102", order="ORD1004")["order_linked"] is True

    def test_multiple_tickets_for_the_same_order_are_allowed(self, temp_db):
        a = ticket(order="ORD1001")
        b = ticket(order="ORD1001")
        assert a["ticket_id"] != b["ticket_id"]
        assert temp_db.count_tickets() == 2


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

VALID = dict(
    authenticated_customer_id="C101", issue_type="Order issue",
    description="Where is my order?", order_id=None, conversation_summary=None,
)

TYPE_ERRORS = [
    pytest.param(dict(authenticated_customer_id=101), id="customer-int"),
    pytest.param(dict(authenticated_customer_id=None), id="customer-None"),
    pytest.param(dict(issue_type=None), id="issue-None"),
    pytest.param(dict(issue_type=7), id="issue-int"),
    pytest.param(dict(description=None), id="description-None"),
    pytest.param(dict(description=5), id="description-int"),
    pytest.param(dict(description=b"bytes"), id="description-bytes"),
    pytest.param(dict(order_id=1001), id="order-int"),
    pytest.param(dict(order_id=1.5), id="order-float"),
    pytest.param(dict(order_id=True), id="order-bool"),
    pytest.param(dict(order_id=["ORD1001"]), id="order-list"),
    pytest.param(dict(order_id=b"ORD1001"), id="order-bytes"),
    pytest.param(dict(conversation_summary=5), id="summary-int"),
    pytest.param(dict(conversation_summary=False), id="summary-bool"),
    pytest.param(dict(conversation_summary=["a"]), id="summary-list"),
    pytest.param(dict(conversation_summary={"k": "v"}), id="summary-dict"),
    pytest.param(dict(conversation_summary=b"bytes"), id="summary-bytes"),
]

VALUE_ERRORS = [
    pytest.param(dict(authenticated_customer_id=""), id="customer-empty"),
    pytest.param(dict(authenticated_customer_id="   "), id="customer-blank"),
    pytest.param(dict(issue_type=""), id="issue-empty"),
    pytest.param(dict(issue_type="  \t"), id="issue-blank"),
    pytest.param(dict(description=""), id="description-empty"),
    pytest.param(dict(description=" \n "), id="description-blank"),
    pytest.param(dict(order_id=""), id="order-empty"),
    pytest.param(dict(order_id="   "), id="order-whitespace"),
    pytest.param(dict(order_id="\t\n"), id="order-tab-newline"),
    pytest.param(dict(conversation_summary=""), id="summary-empty"),
    pytest.param(dict(conversation_summary="   "), id="summary-blank"),
    pytest.param(dict(conversation_summary=" \n\t"), id="summary-whitespace"),
]


class TestValidation:
    @pytest.mark.parametrize("override", TYPE_ERRORS)
    def test_wrong_type_raises_type_error(self, temp_db, override):
        with pytest.raises(TypeError):
            create_support_ticket(**{**VALID, **override})
        assert temp_db.repository_connections == []     # no connection was opened
        assert temp_db.count_tickets() == 0

    @pytest.mark.parametrize("override", VALUE_ERRORS)
    def test_invalid_value_raises_value_error(self, temp_db, override):
        with pytest.raises(ValueError):
            create_support_ticket(**{**VALID, **override})
        assert temp_db.repository_connections == []
        assert temp_db.count_tickets() == 0

    def test_type_error_is_not_a_value_error_and_vice_versa(self, temp_db):
        with pytest.raises(TypeError) as type_info:
            create_support_ticket(**{**VALID, "description": None})
        assert not isinstance(type_info.value, ValueError)
        with pytest.raises(ValueError) as value_info:
            create_support_ticket(**{**VALID, "description": " "})
        assert not isinstance(value_info.value, TypeError)

    def test_blank_order_id_is_not_silently_treated_as_none(self, temp_db):
        with pytest.raises(ValueError):
            ticket(order="")
        with pytest.raises(ValueError):
            ticket(order="   ")
        assert temp_db.count_tickets() == 0

    def test_blank_summary_is_not_silently_treated_as_none(self, temp_db):
        with pytest.raises(ValueError):
            ticket(summary="")
        assert temp_db.count_tickets() == 0

    def test_optional_arguments_may_be_omitted(self, temp_db):
        result = create_support_ticket("C101", "General", "How do I contact support?")
        assert result["outcome"] == "created"
        row = temp_db.tickets()[0]
        assert row["order_id"] is None and row["conversation_summary"] is None

    def test_optional_arguments_may_be_passed_positionally(self, temp_db):
        result = create_support_ticket("C101", "Order issue", "Where?", "ORD1001", "summary")
        assert result["order_id"] == "ORD1001"
        assert temp_db.tickets()[0]["conversation_summary"] == "summary"

    def test_validation_happens_before_any_write_even_when_later_args_are_bad(self, temp_db):
        with pytest.raises(TypeError):
            create_support_ticket("C101", "t", "d", order_id="ORD1001", conversation_summary=5)
        assert temp_db.count_tickets() == 0

    def test_validation_errors_win_over_connection_failures(self, temp_db, monkeypatch):
        def boom():  # pragma: no cover - must not be reached
            raise ConnectionError_("x")

        monkeypatch.setattr(repository, "get_connection", boom)
        with pytest.raises(ValueError):
            ticket(description=" ")


# ---------------------------------------------------------------------------
# Stored values, IDs, defaults
# ---------------------------------------------------------------------------

class TestStoredValues:
    def test_ticket_id_format(self, temp_db):
        result = ticket()
        assert TICKET_ID_RE.fullmatch(result["ticket_id"])

    def test_ticket_id_is_prefix_plus_first_12_uppercase_hex_of_a_uuid4(self, temp_db, monkeypatch):
        import uuid

        monkeypatch.setattr(repository.uuid, "uuid4", lambda: uuid.UUID("abcdef01-2345-4678-89ab-cdef01234567"))
        assert ticket()["ticket_id"] == "TKT-ABCDEF012345"

    def test_ids_are_unique(self, temp_db):
        ids = {ticket()["ticket_id"] for _ in range(25)}
        assert len(ids) == 25

    def test_result_dictionary_shape(self, temp_db):
        result = ticket(order="ORD1001")
        assert set(result) == RESULT_KEYS
        assert result["outcome"] == "created"
        assert isinstance(result["order_linked"], bool)

    def test_result_ticket_id_matches_the_stored_row(self, temp_db):
        result = ticket()
        assert [r["ticket_id"] for r in temp_db.tickets()] == [result["ticket_id"]]

    def test_status_and_priority_defaults(self, temp_db):
        ticket()
        row = temp_db.tickets()[0]
        assert row["status"] == "Open"
        assert row["priority"] == "Normal"

    def test_created_at_uses_the_schema_default_in_utc_iso_format(self, temp_db):
        before = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=2)
        ticket()
        after = datetime.now(UTC) + timedelta(seconds=2)
        stored = temp_db.tickets()[0]["created_at"]
        assert STORED_TS_RE.fullmatch(stored)
        stored_dt = datetime.strptime(stored, TS_FORMAT).replace(tzinfo=UTC)
        assert before <= stored_dt <= after

    def test_created_at_is_not_supplied_by_the_function(self, temp_db):
        """The INSERT must not name created_at, status or priority (schema defaults apply)."""
        proxies = temp_db.use_faulty_connection()
        ticket()
        insert = next(s for s in proxies[0].sql if s.lstrip().upper().startswith("INSERT"))
        for column in ("created_at", "status", "priority"):
            assert column not in insert

    def test_all_statements_are_parameterized(self, temp_db):
        """Hostile values never appear in the SQL text, only as bound parameters."""
        proxies = temp_db.use_faulty_connection()
        ticket(issue_type="ISSUE_MARKER", description="DESC_MARKER", order="ORD1001", summary="SUMMARY_MARKER")
        for statement in proxies[0].sql:
            for marker in ("ISSUE_MARKER", "DESC_MARKER", "SUMMARY_MARKER", "ORD1001", "C101"):
                assert marker not in statement

    def test_summary_none_is_stored_as_sql_null(self, temp_db):
        ticket(summary=None)
        row = temp_db.query(
            "SELECT conversation_summary, typeof(conversation_summary) AS t FROM support_tickets"
        )[0]
        assert row["conversation_summary"] is None
        assert row["t"] == "null"

    def test_summary_string_is_stored_verbatim(self, temp_db):
        summary = "  Customer asked about ORD1001.\nAgent: shipped.  é中 "
        ticket(summary=summary)
        assert temp_db.tickets()[0]["conversation_summary"] == summary

    def test_issue_type_and_description_are_stored_verbatim(self, temp_db):
        ticket(issue_type="  Refund  ", description="  line one\nline two é  ")
        row = temp_db.tickets()[0]
        assert row["issue_type"] == "  Refund  "
        assert row["description"] == "  line one\nline two é  "

    def test_hostile_text_is_stored_as_data_not_executed(self, temp_db):
        evil = "x'); DROP TABLE support_tickets; --"
        result = ticket(issue_type=evil, description=evil, summary=evil, order=evil)
        assert result["outcome"] == "created" and result["order_id"] is None
        row = temp_db.tickets()[0]
        assert row["issue_type"] == row["description"] == row["conversation_summary"] == evil

    def test_customer_id_is_the_authenticated_one(self, temp_db):
        ticket(customer="C104")
        assert temp_db.tickets()[0]["customer_id"] == "C104"

    def test_unknown_customer_creates_nothing_and_fails_generically(self, temp_db):
        result = ticket(customer="C999")
        assert result == FAILED
        assert temp_db.count_tickets() == 0

    def test_nothing_else_is_written(self, temp_db):
        ticket(order="ORD1001")
        counts = temp_db.query(
            "SELECT (SELECT COUNT(*) FROM return_requests) AS r, "
            "(SELECT COUNT(*) FROM orders) AS o, (SELECT COUNT(*) FROM order_items) AS i"
        )[0]
        assert counts == {"r": 0, "o": 15, "i": 16}


# ---------------------------------------------------------------------------
# Transaction behaviour and database failures
# ---------------------------------------------------------------------------

LEAKY = re.compile(r"sqlite|no such table|disk i/o|constraint|foreign key|traceback|secret|\.db", re.IGNORECASE)


class TestTransactions:
    def test_uses_begin_immediate_and_commits_after_the_insert(self, temp_db):
        proxies = temp_db.use_faulty_connection()
        assert ticket(order="ORD1001")["outcome"] == "created"
        log = proxies[0].log
        assert log[0] == "BEGIN IMMEDIATE"
        assert log.index("INSERT") < log.index("COMMIT")
        assert log[-2:] == ["COMMIT", "CLOSE"]
        assert "ROLLBACK" not in log

    def test_row_is_not_visible_to_others_until_commit(self, temp_db):
        seen_before_commit: list[int] = []
        temp_db.use_faulty_connection(
            before_commit=lambda: seen_before_commit.append(temp_db.count_tickets())
        )
        assert ticket()["outcome"] == "created"
        assert seen_before_commit == [0]       # uncommitted insert invisible
        assert temp_db.count_tickets() == 1    # visible after commit

    def test_success_is_not_reported_when_commit_fails(self, temp_db):
        proxies = temp_db.use_faulty_connection(commit_exc=sqlite3.OperationalError("disk I/O error"))
        result = ticket(order="ORD1001")
        assert result == FAILED
        assert result["outcome"] != "created"
        assert temp_db.count_tickets() == 0
        assert "ROLLBACK" in proxies[0].log and proxies[0].log[-1] == "CLOSE"

    @pytest.mark.parametrize("verb", ["BEGIN", "SELECT", "INSERT"])
    def test_sqlite_error_at_any_stage_returns_generic_failed(self, temp_db, verb):
        proxies = temp_db.use_faulty_connection(
            fail_on=(verb, sqlite3.OperationalError("disk I/O error at /secret/path.db"))
        )
        result = ticket(order="ORD1001")
        assert result == FAILED
        assert not LEAKY.search(repr(result))
        assert temp_db.count_tickets() == 0
        assert proxies[0].log[-1] == "CLOSE"

    @pytest.mark.parametrize("error", [
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.OperationalError("database is locked"),
        sqlite3.IntegrityError("FOREIGN KEY constraint failed"),
        sqlite3.ProgrammingError("Incorrect number of bindings supplied"),
    ], ids=lambda e: type(e).__name__)
    def test_any_sqlite_error_family_is_generic_failed(self, temp_db, error):
        temp_db.use_faulty_connection(fail_on=("INSERT", error))
        result = ticket()
        assert result == FAILED
        assert str(error) not in repr(result)

    def test_failed_result_is_exactly_the_documented_dictionary(self, temp_db):
        temp_db.use_faulty_connection(fail_on=("INSERT", sqlite3.OperationalError("x")))
        assert ticket() == {"outcome": "failed", "ticket_id": None, "order_id": None, "order_linked": False}

    def test_failed_result_does_not_echo_a_linked_order(self, temp_db):
        temp_db.use_faulty_connection(fail_on=("INSERT", sqlite3.OperationalError("x")))
        result = ticket(order="ORD1001")
        assert result["order_id"] is None and result["order_linked"] is False

    def test_missing_schema_returns_generic_failed(self, empty_db):
        result = ticket(order="ORD1001")
        assert result == FAILED
        assert not LEAKY.search(repr(result))

    def test_write_lock_held_by_another_connection_returns_failed_then_recovers(self, temp_db):
        temp_db.use_zero_busy_timeout()
        holder = temp_db.open()
        holder.execute("BEGIN IMMEDIATE")
        try:
            result = ticket()
        finally:
            holder.rollback()
            holder.close()
        assert result == FAILED
        assert temp_db.count_tickets() == 0
        assert ticket()["outcome"] == "created"   # nothing was left locked or half-done

    # ---- connection acquisition ------------------------------------------

    def test_connection_error_on_acquisition_returns_generic_failed(self, temp_db, monkeypatch):
        def boom():
            raise ConnectionError_("Failed to open SQLite connection at '/secret/path.db': unable to open")

        monkeypatch.setattr(repository, "get_connection", boom)
        result = ticket()
        assert result == FAILED
        assert not LEAKY.search(repr(result))

    def test_connection_error_is_not_propagated(self, temp_db, monkeypatch):
        def boom():
            raise ConnectionError_("x")

        monkeypatch.setattr(repository, "get_connection", boom)
        try:
            ticket()
        except ConnectionError_:  # pragma: no cover - failure path
            pytest.fail("ConnectionError_ must not propagate to the caller")

    def test_other_exceptions_from_acquisition_are_not_swallowed(self, temp_db, monkeypatch):
        def boom():
            raise RuntimeError("unexpected programming error")

        monkeypatch.setattr(repository, "get_connection", boom)
        with pytest.raises(RuntimeError, match="unexpected programming error"):
            ticket()

    # ---- non-database exceptions ----------------------------------------

    @pytest.mark.parametrize("verb", ["SELECT", "INSERT"])
    def test_non_database_exception_is_reraised_after_rollback(self, temp_db, verb):
        proxies = temp_db.use_faulty_connection(fail_on=(verb, RuntimeError("programming bug")))
        with pytest.raises(RuntimeError, match="programming bug"):
            ticket(order="ORD1001")
        log = proxies[0].log
        assert "ROLLBACK" in log and "COMMIT" not in log
        assert log[-1] == "CLOSE"
        assert temp_db.count_tickets() == 0

    def test_database_is_still_writable_after_a_reraised_exception(self, temp_db):
        temp_db.use_faulty_connection(fail_on=("INSERT", RuntimeError("programming bug")))
        with pytest.raises(RuntimeError):
            ticket()
        other = temp_db.open()
        other.execute("PRAGMA busy_timeout = 0")
        other.execute("BEGIN IMMEDIATE")      # no lock was left behind
        other.rollback()
        other.close()

    def test_exception_object_is_preserved(self, temp_db):
        class CustomError(Exception):
            pass

        error = CustomError("custom")
        temp_db.use_faulty_connection(fail_on=("INSERT", error))
        with pytest.raises(CustomError) as info:
            ticket()
        assert info.value is error

    # ---- rollback and close failures -------------------------------------

    def test_rollback_failure_does_not_mask_the_original_exception(self, temp_db):
        temp_db.use_faulty_connection(
            fail_on=("INSERT", RuntimeError("original bug")),
            rollback_exc=sqlite3.OperationalError("rollback boom"),
        )
        with pytest.raises(RuntimeError, match="original bug") as info:
            ticket()
        assert "rollback boom" not in str(info.value)

    def test_rollback_failure_on_a_database_error_still_returns_generic_failed(self, temp_db):
        temp_db.use_faulty_connection(
            commit_exc=sqlite3.OperationalError("disk I/O error"),
            rollback_exc=sqlite3.OperationalError("rollback boom"),
        )
        assert ticket() == FAILED

    def test_rollback_failure_never_reports_success(self, temp_db):
        temp_db.use_faulty_connection(
            fail_on=("INSERT", sqlite3.OperationalError("disk I/O error")),
            rollback_exc=sqlite3.OperationalError("rollback boom"),
        )
        assert ticket() == FAILED and temp_db.count_tickets() == 0

    def test_close_failure_does_not_turn_a_committed_write_into_an_error(self, temp_db):
        temp_db.use_faulty_connection(close_exc=sqlite3.OperationalError("close boom"))
        result = ticket()
        assert result["outcome"] == "created"
        assert temp_db.count_tickets() == 1

    def test_close_failure_does_not_mask_a_reraised_exception(self, temp_db):
        temp_db.use_faulty_connection(
            fail_on=("INSERT", RuntimeError("original bug")),
            close_exc=sqlite3.OperationalError("close boom"),
        )
        with pytest.raises(RuntimeError, match="original bug"):
            ticket()

    # ---- connection lifecycle --------------------------------------------

    @pytest.mark.parametrize("kwargs", [
        pytest.param(dict(), id="general"),
        pytest.param(dict(order="ORD1001"), id="owned-order"),
        pytest.param(dict(order="ORD1003"), id="unowned-order"),
        pytest.param(dict(order="ORD9999"), id="nonexistent-order"),
    ])
    def test_connection_is_always_closed(self, temp_db, kwargs):
        ticket(**kwargs)
        assert len(temp_db.repository_connections) == 1
        assert_closed(temp_db.repository_connections[0])

    def test_one_connection_per_call_and_none_left_open(self, temp_db):
        ticket()
        ticket(order="ORD1001")
        assert len(temp_db.repository_connections) == 2
        for connection in temp_db.repository_connections:
            assert_closed(connection)
