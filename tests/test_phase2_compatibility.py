"""Gate x schema-version matrix (verification pass): gate OFF/ON against a version-1 and a version-2 database.

Contract proved here: no migration ever runs automatically; gate ON needs the real writer AND a migrated schema; gate OFF keeps
the legacy write path on a version-2 database. A version-1 database is refused by EVERY runtime connection (the version check
in ``app.db.connection`` is exact), so gate OFF against version 1 does not run: the application upgrade requires the operator
migration (or a re-seed) first. These tests pin that behaviour so it cannot change silently.
"""

from __future__ import annotations

import hashlib

import pytest

from app.backend import Backend, Settings
from app.backend.api import build_ticket_writer
from app.backend.config import ConfigError
from app.db import repository
from app.db.connection import get_connection
from app.db.errors import DatabaseError
from app.db.health import check_database_health
from app.db.ticket_writer import RepositoryTicketWriter
from backend_helpers import Client, ScriptedLLM
from phase2_support import build_v1_database
from ticket_writer_fakes import session_of

OFF = Settings()
ON = Settings(fresh_ticket_flow_enabled=True)


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


@pytest.fixture
def v1(tmp_path, monkeypatch):
    path = build_v1_database(str(tmp_path / "v1.db"))
    monkeypatch.setenv("APP_DB_PATH", path)
    return path


def version_of(path):
    import sqlite3
    raw = sqlite3.connect(path)
    try:
        return raw.execute("PRAGMA user_version").fetchone()[0]
    finally:
        raw.close()


class TestGateOffVersion1:
    def test_the_backend_starts_but_no_runtime_connection_is_possible(self, v1):
        before = digest(v1)
        backend = Backend(llm=ScriptedLLM(), settings=OFF)
        assert backend.ticket_writer is None
        with pytest.raises(DatabaseError):
            get_connection()
        assert check_database_health().status == "schema_incompatible"
        assert backend.list_login_options() == []
        started = backend.start_session("C101")
        assert started.ok is False and started.error_code in ("unavailable", "unknown_customer")
        assert digest(v1) == before and version_of(v1) == 1  # nothing migrated, nothing written

    def test_the_documented_dependency_is_the_exact_version_check(self, v1):
        with pytest.raises(DatabaseError) as raised:
            repository.list_customers_for_login()
        assert "migrate" in str(raised.value).lower()


class TestGateOffVersion2:
    def test_the_legacy_path_creates_a_ticket(self, temp_db, bnow, raw):
        backend = Backend(llm=ScriptedLLM(), settings=OFF)
        assert backend.ticket_writer is None
        client = Client(backend, "C101")
        s = session_of(client)
        text = "My ORD1001 earbuds stopped charging after two days."
        backend.engine._start_issue(s, text)
        card = backend.engine._propose_ticket(s, {}, text)
        client.confirm(card)
        row = raw.execute("SELECT request_key, description FROM support_tickets").fetchone()
        assert row["request_key"].startswith("legacy:") and row["description"] == text

    def test_the_fresh_flow_is_not_reachable_while_the_gate_is_off(self, temp_db, bnow, raw):
        client = Client(Backend(llm=ScriptedLLM(), settings=OFF), "C101")
        assert client.backend.engine.fresh_flow is False
        assert raw.execute("SELECT COUNT(*) FROM ticket_write_intents").fetchone()[0] == 0


class TestGateOnVersion1:
    def test_startup_fails_closed_and_nothing_is_migrated(self, v1):
        before = digest(v1)
        with pytest.raises(ConfigError):
            build_ticket_writer(ON)
        with pytest.raises(ConfigError):
            Backend(llm=ScriptedLLM(), settings=ON)
        assert digest(v1) == before and version_of(v1) == 1


class TestGateOnVersion2:
    def test_the_real_writer_is_composed_and_creates_a_fresh_ticket(self, temp_db, bnow, raw):
        backend = Backend(llm=ScriptedLLM(), settings=ON)
        assert type(backend.ticket_writer) is RepositoryTicketWriter
        client = Client(backend, "C101")
        client.say("raise a ticket")
        reply = client.say("My order has not arrived yet")
        reply = client.pick(reply, "ORD1002")
        client.confirm(reply)
        row = raw.execute("SELECT conversation_summary, request_key FROM support_tickets").fetchone()
        assert row["conversation_summary"] is None and row["request_key"].startswith("rk_")


class TestNothingMigratesAutomatically:
    @pytest.mark.parametrize("settings", [OFF, ON], ids=["gate-off", "gate-on"])
    def test_constructing_a_backend_never_changes_a_version_1_file(self, v1, settings):
        before = digest(v1)
        try:
            Backend(llm=ScriptedLLM(), settings=settings)
        except ConfigError:
            pass
        assert digest(v1) == before
