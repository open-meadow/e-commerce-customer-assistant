"""Phase 1.1 flow plus the Phase 2 real writer, end to end on a scratch database (NT-141, NT-157 to NT-159, NT-164, NT-126/127).

Only temporary databases are used. The real writer is the production class; the gate interlock is exercised through
``build_ticket_writer``. No live model, no protected database.
"""

from __future__ import annotations

import pytest

from app.backend import Backend, Settings
from app.backend.api import build_ticket_writer
from app.backend.config import ConfigError
from app.db import repository
from app.db.errors import DatabaseOutcomeUnknownError
from app.db.ticket_writer import RepositoryTicketWriter
from app.tools import flow_texts
from backend_helpers import Client, ScriptedLLM
from phase2_support import build_v1_database
from ticket_writer_fakes import fresh_backend, session_of

T = flow_texts.text
GATE_ON = Settings(fresh_ticket_flow_enabled=True)


def real_client(customer="C101", **settings):
    return Client(fresh_backend(RepositoryTicketWriter(), **settings), customer)


def new_ticket(c, description, order="none"):
    c.say("raise a ticket")
    reply = c.say(description)
    if reply.choice is not None:
        reply = c.pick(reply, order)
    assert reply.pending is not None, reply
    return reply


def rows(raw):
    return [dict(r) for r in raw.execute("SELECT * FROM support_tickets ORDER BY created_at, ticket_id").fetchall()]


class TestGateAgainstTheSchema:
    def test_gate_on_works_against_a_migrated_database(self, temp_db):
        assert type(build_ticket_writer(GATE_ON)) is RepositoryTicketWriter
        Backend(llm=ScriptedLLM(), settings=GATE_ON)  # the production composition starts

    def test_gate_on_cannot_start_against_an_unmigrated_database(self, tmp_path, monkeypatch):
        path = build_v1_database(str(tmp_path / "v1.db"))
        monkeypatch.setenv("APP_DB_PATH", path)
        with pytest.raises(ConfigError):
            build_ticket_writer(GATE_ON)
        with pytest.raises(ConfigError):
            Backend(llm=ScriptedLLM(), settings=GATE_ON)

    def test_gate_off_never_builds_a_writer_even_on_an_unmigrated_database(self, tmp_path, monkeypatch):
        path = build_v1_database(str(tmp_path / "v1.db"))
        monkeypatch.setenv("APP_DB_PATH", path)
        assert build_ticket_writer(Settings()) is None

    def test_the_default_settings_leave_the_production_gate_off(self):
        assert Settings().fresh_ticket_flow_enabled is False and Settings.from_env({}).fresh_ticket_flow_enabled is False


class TestFreshFlowCreatesTheRealRow:
    def test_description_is_verbatim_summary_is_null_and_the_id_comes_from_the_database(self, temp_db, bnow, raw):
        c = real_client()
        text = "  Screen अभि cracked <i>on arrival</i>\n\twith *Markdown* \U0001F600  "
        done = c.confirm(new_ticket(c, text, order="ORD1001"))
        row = rows(raw)[0]
        assert row["description"] == text and row["conversation_summary"] is None
        assert row["order_id"] == "ORD1001" and row["customer_id"] == "C101" and row["status"] == "Open"
        assert row["request_key"].startswith("rk_") and row["ticket_id"] in done.text
        assert raw.execute("SELECT COUNT(*) FROM ticket_events WHERE ticket_id = ?", (row["ticket_id"],)).fetchone()[0] == 1
        assert raw.execute("SELECT state FROM ticket_write_intents").fetchone()[0] == "committed"

    def test_exactly_2000_code_points_are_stored_unmodified(self, temp_db, bnow, raw):
        c = real_client()
        text = "न" * 2000
        c.confirm(new_ticket(c, text))
        assert rows(raw)[0]["description"] == text and len(rows(raw)[0]["description"]) == 2000

    def test_a_ticket_without_a_summary_is_listed_and_tolerated(self, temp_db, bnow, raw):
        c = real_client()
        done = c.confirm(new_ticket(c, "My order has not arrived yet", order="ORD1002"))
        listing = c.say("show my tickets")
        assert rows(raw)[0]["ticket_id"] in listing.text

    def test_the_legacy_gate_off_path_still_stores_its_summary_and_the_description_it_built(self, temp_db, bnow, raw):
        c = Client(Backend(llm=ScriptedLLM(), settings=Settings()), "C101")
        s = session_of(c)
        text = "My ORD1001 earbuds stopped charging after two days."
        c.backend.engine._start_issue(s, text)
        card = c.backend.engine._propose_ticket(s, {}, text)
        assert card.pending is not None
        c.confirm(card)
        row = rows(raw)[0]
        assert row["conversation_summary"] is not None and row["description"] == text and row["request_key"].startswith("legacy:")


class TestRateLimitAndDuplicates:
    def test_the_limit_is_enforced_at_creation_and_the_preview_is_kept(self, temp_db, bnow, raw):
        c = real_client(ticket_rate_max=1)
        c.confirm(new_ticket(c, "My order has not arrived yet", order="ORD1002"))
        # the early check is only a courtesy that informs the customer; it is bypassed here so the TRANSACTIONAL check decides
        c.backend.engine._rate_limited_turn = lambda s: None
        card = new_ticket(c, "My payment failed twice for this purchase", order="ORD1001")
        limited = c.confirm(card)
        assert "limit for creating new support tickets" in limited.text
        assert limited.pending is not None and limited.pending.action_id == card.pending.action_id
        assert len(rows(raw)) == 1  # nothing was written for the refused one

    def test_drafts_that_are_never_confirmed_consume_nothing(self, temp_db, bnow, raw):
        c = real_client(ticket_rate_max=1)
        for _ in range(3):
            c.say("raise a ticket")
            c.say("cancel")
        c.confirm(new_ticket(c, "My order has not arrived yet", order="ORD1002"))
        assert len(rows(raw)) == 1

    def test_a_second_request_for_an_active_issue_is_reported_not_created(self, temp_db, bnow, raw):
        c = real_client(ticket_rate_max=50)
        first = c.confirm(new_ticket(c, "My order has not arrived yet", order="ORD1002"))
        second = c.confirm(new_ticket(c, "My order still has not arrived", order="ORD1002"))
        assert len(rows(raw)) == 1
        assert rows(raw)[0]["ticket_id"] in second.text and "created" not in second.text.replace("haven't created", "")


class TestUncertainOutcomeWithTheRealWriter:
    def test_retry_after_an_unknown_outcome_reuses_the_key_and_never_creates_two(self, temp_db, bnow, raw, monkeypatch):
        c = real_client()
        card = new_ticket(c, "My order has not arrived yet", order="ORD1002")
        real = repository.create_ticket_from_description
        seen = []

        def flaky(*args, **kwargs):
            seen.append(args[3])
            if len(seen) == 1:
                raise DatabaseOutcomeUnknownError("commit failed")  # nothing reached the database in this simulation
            return real(*args, **kwargs)

        monkeypatch.setattr(repository, "create_ticket_from_description", flaky)
        first = c.confirm(card)
        assert first.text == T("ticket_outcome_unknown") and rows(raw) == []
        assert c.say("cancel").text == T("ticket_outcome_unknown")  # nothing clears the unresolved operation
        second = c.confirm(first)  # the same confirmation, the same key
        assert len(rows(raw)) == 1 and rows(raw)[0]["ticket_id"] in second.text
        assert seen[0] == seen[1] == rows(raw)[0]["request_key"]
        again = c.confirm(first)  # the card is finished: no second creation
        assert len(rows(raw)) == 1
