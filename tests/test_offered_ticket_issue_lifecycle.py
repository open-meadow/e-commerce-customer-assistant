"""Objective D: the return issue behind an offered support ticket is closed only after the ticket is created.

Everything goes through the real orchestration: the W1 refusal, the offer, detail collection, the pending card,
the Confirm decision and the repository write. RuleBasedLLM, temporary databases and fixed clocks only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backend import Backend, Settings
from app.db import repository
from app.db.errors import DatabaseOperationalError
from app.llm.rule_based import RuleBasedLLM
from backend_helpers import Client

UTC = timezone.utc
DELIVERED = datetime(2026, 11, 4, tzinfo=UTC)
REQUEST = "I want to return item IT101 from order ORD1001 because it is damaged"
DETAILS = "Why is the left earbud not working?"


def rows(raw, table):
    return [dict(r) for r in raw.execute(f"SELECT * FROM {table}").fetchall()]


@pytest.fixture
def backend(temp_db):
    return Backend(llm=RuleBasedLLM(), settings=Settings())


def session_of(backend, client):
    return backend.registry.get(client.state._handle)


def active_id(backend, client):
    issue = session_of(backend, client).issues.active
    return issue.issue_id if issue else None


def offer(backend, set_now):
    set_now(DELIVERED + timedelta(hours=72))
    c = Client(backend, "C101")
    c.pick(c.say(REQUEST), "damaged")
    original = active_id(backend, c)
    assert original is not None  # the damaged report started the return issue
    return c, original


def card(backend, set_now):
    c, original = offer(backend, set_now)
    assert "describe the problem" in c.say("yes").text
    proposal = c.say(DETAILS)
    assert proposal.status == "needs_confirmation"
    return c, original, proposal


class TestOfferedTicketClosesItsIssueOnlyOnSuccess:
    def test_confirmation_creates_one_ticket_and_closes_the_original_issue(self, backend, raw, set_now):
        c, original, proposal = card(backend, set_now)
        done = c.confirm(proposal)
        assert "TKT_" in done.text and len(rows(raw, "support_tickets")) == 1
        assert session_of(backend, c).issues.active is None and session_of(backend, c).issues.suspended is None

    def test_a_later_ticket_request_does_not_reuse_the_old_return_message(self, backend, raw, set_now):
        c, _, proposal = card(backend, set_now)
        c.confirm(proposal)
        asked = c.say("I want to raise a ticket")
        assert "What is the issue" in asked.text and asked.pending is None
        assert len(rows(raw, "support_tickets")) == 1

    def test_the_pending_payload_carries_the_exact_issue_id(self, backend, set_now):
        c, original, proposal = card(backend, set_now)
        action = backend.registry._pending[proposal.pending.action_id]
        assert action.payload["issue_id"] == original

    def test_creating_the_proposal_does_not_close_the_issue(self, backend, raw, set_now):
        c, original, proposal = card(backend, set_now)
        assert active_id(backend, c) == original and rows(raw, "support_tickets") == []

    def test_each_earlier_step_keeps_the_issue_active(self, backend, raw, set_now):
        c, original = offer(backend, set_now)
        assert active_id(backend, c) == original  # while the customer decides
        c.say("hmm")
        assert active_id(backend, c) == original  # an ambiguous reply
        c.say("yes")
        assert active_id(backend, c) == original  # while the details are being collected
        c.say("!!!")
        assert active_id(backend, c) == original  # a refused description

    @pytest.mark.parametrize("answer", ["no", "cancel"])
    def test_cancelling_the_offer_keeps_the_issue_active(self, backend, raw, set_now, answer):
        c, original = offer(backend, set_now)
        c.say(answer)
        assert active_id(backend, c) == original and rows(raw, "support_tickets") == []

    def test_cancelling_during_detail_collection_keeps_the_issue_active(self, backend, raw, set_now):
        c, original = offer(backend, set_now)
        c.say("yes")
        c.say("cancel")
        assert active_id(backend, c) == original and rows(raw, "support_tickets") == []

    def test_declining_the_card_with_the_button_keeps_the_issue_active(self, backend, raw, set_now):
        c, original, proposal = card(backend, set_now)
        c.confirm(proposal, accepted=False)
        assert active_id(backend, c) == original and rows(raw, "support_tickets") == []

    def test_typed_cancel_on_the_card_keeps_the_issue_active(self, backend, raw, set_now):
        c, original, proposal = card(backend, set_now)
        c.say("cancel")
        assert active_id(backend, c) == original and rows(raw, "support_tickets") == []

    def test_an_expired_card_keeps_the_issue_active(self, backend, raw, set_now, bnow):
        c, original, proposal = card(backend, set_now)
        bnow(minutes=11)
        assert "expired" in c.confirm(proposal).text
        assert active_id(backend, c) == original and rows(raw, "support_tickets") == []

    def test_a_failed_write_keeps_the_issue_active(self, backend, raw, set_now, monkeypatch):
        c, original, proposal = card(backend, set_now)

        def down(*args, **kwargs):
            raise DatabaseOperationalError("down")

        monkeypatch.setattr(repository, "create_support_ticket", down)
        out = c.confirm(proposal)
        assert "TKT_" not in out.text and rows(raw, "support_tickets") == []
        assert active_id(backend, c) == original
        session = session_of(backend, c)
        assert session.committed["support_ticket"] == 0 and session.reserved["support_ticket"] == 0

    def test_a_newer_active_issue_is_not_closed(self, backend, raw, set_now):
        c, original, proposal = card(backend, set_now)
        book = session_of(backend, c).issues
        book.abandon("active")
        newer = book.start("A different problem the customer raised")
        c.confirm(proposal)
        assert len(rows(raw, "support_tickets")) == 1
        assert book.active is newer and book.active.messages == ["A different problem the customer raised"]

    def test_a_suspended_original_and_a_newer_active_issue_both_survive(self, backend, raw, set_now):
        c, original, proposal = card(backend, set_now)
        book = session_of(backend, c).issues
        book.suspend_active_and_start("A different problem")
        newer_id = book.active.issue_id
        c.confirm(proposal)
        assert len(rows(raw, "support_tickets")) == 1
        assert book.active.issue_id == newer_id and book.suspended.issue_id == original

    def test_duplicate_confirmation_creates_nothing_and_leaves_the_lifecycle_alone(self, backend, raw, set_now):
        c, original, proposal = card(backend, set_now)
        c.confirm(proposal)
        book = session_of(backend, c).issues
        assert book.active is None
        newer = book.start("A new problem after the ticket")
        again = c.confirm(proposal)
        assert "TKT_" not in again.text and len(rows(raw, "support_tickets")) == 1
        assert book.active is newer  # the repeat did not close the newer issue

    def test_without_an_active_issue_nothing_is_closed_and_the_ticket_is_still_created(self, backend, raw, set_now):
        c, original, proposal = card(backend, set_now)
        session_of(backend, c).issues.clear()
        c.confirm(proposal)
        assert len(rows(raw, "support_tickets")) == 1 and session_of(backend, c).issues.active is None


class TestOrdinaryTicketFlowIsUnchanged:
    def test_an_ordinary_ticket_still_closes_its_own_issue(self, backend, raw):
        c = Client(backend, "C101")
        c.say("I want to raise a ticket")
        proposal = c.say("My earbuds from ORD1001 stopped working after two days")
        assert proposal.status == "needs_confirmation"
        issue = session_of(backend, c).issues.active
        assert issue is not None
        c.confirm(proposal)
        assert len(rows(raw, "support_tickets")) == 1 and session_of(backend, c).issues.active is None

    def test_an_ordinary_ticket_does_not_close_a_newer_issue(self, backend, raw):
        c = Client(backend, "C101")
        c.say("I want to raise a ticket")
        proposal = c.say("My earbuds from ORD1001 stopped working after two days")
        book = session_of(backend, c).issues
        book.abandon("active")
        newer = book.start("Something else")
        c.confirm(proposal)
        assert book.active is newer
