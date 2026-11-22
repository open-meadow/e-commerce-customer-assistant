"""Recovery of an uncertain ticket creation across session expiry and process restart (verification pass).

A "restart" is a brand-new Backend and session over the same database: nothing in memory survives, only ``ticket_write_intents``.
The real repository writer is used; only the database failure is simulated.
"""

from __future__ import annotations

import pytest

from app.db import repository
from app.db.errors import DatabaseOperationalError, DatabaseOutcomeUnknownError
from app.db.ticket_writer import RepositoryTicketWriter
from app.tools import flow_texts
from backend_helpers import Client
from ticket_writer_fakes import fresh_backend

T = flow_texts.text
DESCRIPTION = "My order has not arrived yet, tracking has not moved for a week"


def client_for(customer="C101", **settings):
    return Client(fresh_backend(RepositoryTicketWriter(), **settings), customer)


def card_for(c, description=DESCRIPTION, order="ORD1002"):
    c.say("raise a ticket")
    reply = c.say(description)
    if reply.choice is not None:
        reply = c.pick(reply, order)
    assert reply.pending is not None, reply
    return reply


def tickets(raw):
    return [dict(r) for r in raw.execute("SELECT * FROM support_tickets").fetchall()]


def intents(raw):
    return [dict(r) for r in raw.execute("SELECT * FROM ticket_write_intents").fetchall()]


@pytest.fixture
def unknown_first(monkeypatch):
    """The creation attempt's COMMIT outcome is unknown while armed; nothing reached the database after the intent was stored."""
    real = repository._insert_ticket_attempt
    state = {"armed": True}

    def flaky(*args, **kwargs):
        if state["armed"]:
            raise DatabaseOutcomeUnknownError("commit failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(repository, "_insert_ticket_attempt", flaky)
    return state


class TestRestartRecovery:
    def test_a_new_session_after_restart_completes_the_original_request_once(self, temp_db, bnow, raw, unknown_first):
        first = client_for()
        outcome = first.confirm(card_for(first))
        assert outcome.text == T("ticket_outcome_unknown") and tickets(raw) == []
        stored = intents(raw)
        assert len(stored) == 1 and stored[0]["state"] == "prepared" and stored[0]["last_outcome"] is None
        key = stored[0]["request_key"]

        unknown_first["armed"] = False  # the database is healthy again; the process has restarted
        second = client_for()
        reply = second.say("raise a ticket")
        rows = tickets(raw)
        assert len(rows) == 1 and rows[0]["request_key"] == key and rows[0]["description"] == DESCRIPTION
        assert reply.text == T("ticket_recovered_created", "en", ticket_id=rows[0]["ticket_id"])
        assert reply.pending is None and reply.choice is None  # no new draft was started in the same turn
        assert intents(raw)[0]["state"] == "committed"

    def test_reconciliation_is_repeatable_and_never_creates_a_second_ticket(self, temp_db, bnow, raw, unknown_first):
        first = client_for()
        first.confirm(card_for(first))
        unknown_first["armed"] = False
        c = client_for()
        c.say("raise a ticket")
        assert len(tickets(raw)) == 1
        again = c.say("raise a ticket")  # nothing is unresolved now: the ordinary request for a description
        assert again.text == T("ticket_fresh_request") and len(tickets(raw)) == 1
        assert RepositoryTicketWriter().reconcile("C101") == [] and len(tickets(raw)) == 1

    def test_still_uncertain_blocks_a_new_request_and_creates_nothing(self, temp_db, bnow, raw, unknown_first):
        first = client_for()
        first.confirm(card_for(first))
        second = client_for()  # the database still cannot answer
        reply = second.say("raise a ticket")
        assert reply.text == T("ticket_recovery_unknown") and reply.pending is None and reply.choice is None
        assert tickets(raw) == [] and len(intents(raw)) == 1  # no independent creation, no second intent

    def test_a_mid_session_retry_of_the_same_card_still_uses_the_same_key(self, temp_db, bnow, raw, unknown_first):
        c = client_for()
        unresolved = c.confirm(card_for(c))
        unknown_first["armed"] = False
        done = c.confirm(unresolved)
        assert len(tickets(raw)) == 1 and tickets(raw)[0]["ticket_id"] in done.text
        assert len(intents(raw)) == 1


class TestOwnershipIsolation:
    def test_another_customers_intent_is_never_listed_replayed_or_exposed(self, temp_db, bnow, raw, unknown_first):
        c101 = client_for("C101")
        c101.confirm(card_for(c101))
        unknown_first["armed"] = False
        c102 = client_for("C102")
        reply = c102.say("raise a ticket")
        assert reply.text == T("ticket_fresh_request")  # no recovery for C102
        assert tickets(raw) == []  # C101's request was not replayed by C102's session
        assert repository.list_unresolved_ticket_requests("C102") == []
        key = intents(raw)[0]["request_key"]
        assert [e["request_key"] for e in repository.list_unresolved_ticket_requests("C101")] == [key]
        assert repository.retry_ticket_request("C102", key) == {"outcome": "unknown_request"}
        assert repository.get_ticket_request_state("C102", key) == {"state": "unknown"}
        assert RepositoryTicketWriter().reconcile("C102") == []


class TestToldNothingWasSavedIsNeverReplayed:
    def test_an_unavailable_outcome_closes_the_stored_request(self, temp_db, bnow, raw, monkeypatch):
        real = repository._insert_ticket_attempt
        failing = [True]

        def refused(*args, **kwargs):
            if failing[0]:
                raise DatabaseOperationalError("database write failed")
            return real(*args, **kwargs)

        monkeypatch.setattr(repository, "_insert_ticket_attempt", refused)
        first = client_for()
        reply = first.confirm(card_for(first))
        assert reply.text == T("ticket_write_failed")
        assert intents(raw)[0]["last_outcome"] == "unavailable"
        failing[0] = False
        second = client_for()  # restart: the request the customer was told failed must NOT appear later
        assert second.say("raise a ticket").text == T("ticket_fresh_request")
        assert tickets(raw) == []

    def test_the_same_card_can_still_be_retried_after_unavailable(self, temp_db, bnow, raw, monkeypatch):
        real = repository._insert_ticket_attempt
        calls = []

        def once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise DatabaseOperationalError("database write failed")
            return real(*args, **kwargs)

        monkeypatch.setattr(repository, "_insert_ticket_attempt", once)
        c = client_for()
        failed = c.confirm(card_for(c))
        done = c.confirm(failed)
        assert len(tickets(raw)) == 1 and tickets(raw)[0]["ticket_id"] in done.text
        assert intents(raw)[0]["state"] == "committed" and intents(raw)[0]["last_outcome"] is None

    def test_a_committed_request_is_never_closed(self, temp_db, bnow, raw):
        c = client_for()
        c.confirm(card_for(c))
        key = intents(raw)[0]["request_key"]
        assert repository.close_ticket_request("C101", key) == {"closed": False}
        assert intents(raw)[0]["state"] == "committed"

    def test_close_is_customer_scoped(self, temp_db, bnow, raw, unknown_first):
        c = client_for()
        c.confirm(card_for(c))
        key = intents(raw)[0]["request_key"]
        assert repository.close_ticket_request("C102", key) == {"closed": False}
        assert intents(raw)[0]["last_outcome"] is None


class TestReconcileEdges:
    def test_an_unreadable_intent_table_is_treated_as_uncertain_not_as_empty(self, temp_db, monkeypatch):
        def broken(customer_id):
            raise DatabaseOperationalError("read failed")

        monkeypatch.setattr(repository, "list_unresolved_ticket_requests", broken)
        results = RepositoryTicketWriter().reconcile("C101")
        assert [r.outcome for r in results] == ["outcome_unknown"]

    def test_the_gate_off_engine_has_nothing_to_reconcile(self, temp_db):
        from app.backend import Backend, Settings
        from backend_helpers import ScriptedLLM
        assert Backend(llm=ScriptedLLM(), settings=Settings()).engine.ticket_writer is None
