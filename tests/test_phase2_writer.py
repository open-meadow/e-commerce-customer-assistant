"""Phase 2: the real writer implements the Phase 1.1 interface, maps every outcome, and fails closed (U-1, U-4, W-1)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.db import repository
from app.db.errors import DatabaseOperationalError, DatabaseOutcomeUnknownError
from app.db.ticket_writer import RepositoryTicketWriter, TicketWriterContractError, verify_write_contract
from app.orchestrator import ticket_writer as contract
from app.orchestrator.ticket_writer import TicketWriteRequest, TicketWriter
from conftest import build_database
from phase2_support import CommitProxy, an_item, build_v1_database

T0 = datetime(2026, 11, 7, tzinfo=timezone.utc)
DAY = 86400


def request(key="rk-1", description="screen cracked", issue="general", **kw):
    return TicketWriteRequest(customer_id=kw.pop("customer_id", "C101"), issue_type=issue, description=description,
                              request_key=key, **kw)


@pytest.fixture
def writer(temp_db):
    return RepositoryTicketWriter()


class TestFailClosed:
    def test_the_writer_refuses_an_unmigrated_version_one_database(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_DB_PATH", build_v1_database(str(tmp_path / "v1.db")))
        with pytest.raises(TicketWriterContractError):
            RepositoryTicketWriter()
        with pytest.raises(TicketWriterContractError):
            verify_write_contract()

    def test_the_writer_refuses_a_missing_database(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "missing.db"))
        with pytest.raises(TicketWriterContractError):
            RepositoryTicketWriter()

    def test_the_writer_refuses_a_database_without_the_contract_even_at_the_right_version(self, tmp_path, monkeypatch):
        path = build_database(str(tmp_path / "broken.db"))
        connection = sqlite3.connect(path)
        connection.execute("DROP TRIGGER trg_ticket_write_intents_no_delete")
        connection.execute("DROP TABLE ticket_write_intents")
        connection.commit()
        connection.close()
        monkeypatch.setenv("APP_DB_PATH", path)
        with pytest.raises(TicketWriterContractError):
            RepositoryTicketWriter()

    def test_construction_never_changes_the_database(self, temp_db):
        import hashlib
        before = hashlib.sha256(open(temp_db, "rb").read()).hexdigest()
        RepositoryTicketWriter()
        assert hashlib.sha256(open(temp_db, "rb").read()).hexdigest() == before

    def test_the_real_writer_satisfies_the_interface_and_is_a_distinct_class_from_any_fake(self, writer):
        assert isinstance(writer, RepositoryTicketWriter)
        assert callable(getattr(writer, "create_ticket"))
        class Fake:
            def create_ticket(self, request):
                return contract.TicketWriteResult(contract.CREATED, ticket_id="TKT_FAKE")
        assert not isinstance(Fake(), RepositoryTicketWriter)  # production composition keys on this class
        _: TicketWriter = writer  # structural conformance for type checkers


class TestOutcomeMapping:
    def test_created_carries_only_the_writers_own_ticket_id(self, writer, raw):
        result = writer.create_ticket(request())
        stored = raw.execute("SELECT ticket_id FROM support_tickets").fetchone()[0]
        assert result == contract.TicketWriteResult(contract.CREATED, ticket_id=stored)

    def test_a_retry_of_the_same_confirmation_returns_the_original_ticket(self, writer, raw):
        first = writer.create_ticket(request())
        second = writer.create_ticket(request())
        assert second.outcome == contract.CREATED and second.ticket_id == first.ticket_id
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1

    def test_the_same_key_with_another_payload_is_an_idempotency_conflict(self, writer, raw):
        writer.create_ticket(request())
        assert writer.create_ticket(request(description="other")).outcome == contract.IDEMPOTENCY_CONFLICT
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1

    def test_duplicate_active_names_the_existing_ticket_and_writes_nothing(self, writer, raw):
        item = an_item("ORD1001")
        first = writer.create_ticket(request("a", issue="product", order_id="ORD1001", item_id=item))
        duplicate = writer.create_ticket(request("b", issue="product", order_id="ORD1001", item_id=item))
        assert duplicate == contract.TicketWriteResult(contract.DUPLICATE_ACTIVE, ticket_id=first.ticket_id, status="Open")
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1

    def test_rate_limited_carries_retry_at_and_writes_nothing(self, writer, raw, set_now):
        set_now(T0)
        writer.create_ticket(request("a", rate_limit=(1, DAY)))
        limited = writer.create_ticket(request("b", "second", rate_limit=(1, DAY)))
        assert limited == contract.TicketWriteResult(contract.RATE_LIMITED, retry_at="2026-11-08T00:00:00Z")
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1

    def test_an_error_before_any_write_is_unavailable(self, writer, raw, monkeypatch):
        def refuse(*a, **k):
            raise DatabaseOperationalError("database write failed")
        monkeypatch.setattr(repository, "create_ticket_from_description", refuse)
        assert writer.create_ticket(request()).outcome == contract.UNAVAILABLE

    def test_invalid_input_is_unavailable_with_nothing_written(self, writer, raw):
        assert writer.create_ticket(request(description="a" * 2001)).outcome == contract.UNAVAILABLE
        assert writer.create_ticket(request(description="  ")).outcome == contract.UNAVAILABLE
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0

    def test_an_item_that_is_not_the_customers_is_a_definite_no_write(self, writer, raw):
        result = writer.create_ticket(request(issue="product", order_id="ORD1001", item_id="IT_NOT_THERE"))
        assert result.outcome == contract.UNAVAILABLE and raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0

    def test_an_uncertain_commit_is_outcome_unknown_and_the_same_key_recovers(self, writer, raw, monkeypatch):
        real = repository.get_connection
        calls = {"n": 0}

        def proxy():
            calls["n"] += 1
            connection = real()
            return CommitProxy(connection, commit_first=True) if calls["n"] == 2 else connection

        monkeypatch.setattr(repository, "get_connection", proxy)
        assert writer.create_ticket(request("rk-9")).outcome == contract.OUTCOME_UNKNOWN
        monkeypatch.setattr(repository, "get_connection", real)
        retry = writer.create_ticket(request("rk-9"))  # the SAME request: same key, same immutable payload
        assert retry.outcome == contract.CREATED
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1

    def test_unresolved_requests_are_recovered_through_the_writer_after_a_failed_commit(self, writer, raw, monkeypatch):
        real = repository.get_connection
        calls = {"n": 0}

        def proxy():
            calls["n"] += 1
            connection = real()
            return CommitProxy(connection, commit_first=False) if calls["n"] == 2 else connection

        monkeypatch.setattr(repository, "get_connection", proxy)
        assert writer.create_ticket(request("rk-8")).outcome == contract.OUTCOME_UNKNOWN
        monkeypatch.setattr(repository, "get_connection", real)
        pending = repository.list_unresolved_ticket_requests("C101")
        assert len(pending) == 1
        result = writer.retry_unresolved("C101", pending[0]["request_key"])
        assert result.outcome == contract.CREATED and raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1
        assert writer.retry_unresolved("C101", "nope").outcome == contract.UNAVAILABLE

    def test_the_writer_never_returns_an_outcome_outside_the_vocabulary(self, writer):
        for key, kw in (("a", {}), ("b", dict(description="x" * 2001)), ("c", dict(rate_limit=(1, DAY)))):
            assert writer.create_ticket(request(key, **kw)).outcome in contract.OUTCOMES

    def test_a_success_is_only_ever_reported_for_a_row_that_exists(self, writer, raw):
        result = writer.create_ticket(request())
        assert raw.execute("SELECT COUNT(*) FROM support_tickets WHERE ticket_id = ?", (result.ticket_id,)).fetchone()[0] == 1
