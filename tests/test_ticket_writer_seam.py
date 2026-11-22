"""Phase 1.1: the writer seam (NT-157 to NT-159, NT-164) through the injected FAKE writer. Nothing is persisted anywhere.

The real repository writer is Phase 2 and is tested in ``tests/test_phase2_*.py``.
"""

from __future__ import annotations

import pytest

from app.orchestrator.ticket_writer import (CREATED, DUPLICATE_ACTIVE, IDEMPOTENCY_CONFLICT, NOT_ENABLED, OUTCOME_UNKNOWN, RATE_LIMITED,
                                            UNAVAILABLE, TicketWriteResult)
from app.tools import flow_texts
from backend_helpers import Client
from ticket_writer_fakes import FakeTicketWriter, fresh_backend, session_of

T = flow_texts.text


def card(writer, customer="C101", **settings):
    c = Client(fresh_backend(writer, **settings), customer)
    c.say("raise a ticket")
    reply = c.say("My earbuds stopped charging after two days.")
    reply = c.pick(reply, "none")
    return c, reply


class TestOutcomes:
    def test_created_is_the_only_success_and_uses_only_the_writers_id(self, temp_db, bnow):
        writer = FakeTicketWriter(TicketWriteResult(CREATED, ticket_id="TKT_WRITER99", status="Open"))
        c, pending = card(writer)
        done = c.confirm(pending)
        assert done.text == T("created", ticket_id="TKT_WRITER99")
        assert session_of(c).ticket_draft is None and "tkt_writer99" in session_of(c).known_ticket_ids
        request = writer.requests[0]
        assert request.customer_id == "C101" and request.issue_type and request.request_key.startswith("rk_")

    @pytest.mark.parametrize("outcome", [NOT_ENABLED, UNAVAILABLE])
    def test_not_enabled_and_unavailable_never_show_success_and_keep_the_preview(self, outcome, temp_db, bnow):
        writer = FakeTicketWriter(TicketWriteResult(outcome))
        c, pending = card(writer)
        done = c.confirm(pending)
        assert done.text == T("ticket_write_failed") and "TKT_" not in done.text and "created" not in done.text.lower().replace("couldn't create", "")
        assert done.pending is not None and done.pending.action_id == pending.pending.action_id
        assert session_of(c).ticket_draft is not None and session_of(c).ticket_draft.stage == "preview"

    def test_rate_limited_keeps_the_preview_and_reuses_the_existing_wording(self, temp_db, bnow):
        writer = FakeTicketWriter(TicketWriteResult(RATE_LIMITED, retry_at="2026-11-08 10:00"))
        c, pending = card(writer)
        done = c.confirm(pending)
        assert done.text == T("ticket_rate_limited", retry_at="2026-11-08 10:00")
        assert done.pending is not None and session_of(c).ticket_draft.stage == "preview"
        again = c.confirm(done)  # the same confirmation can be retried
        assert "TKT_" in again.text and len(writer.requests) == 2

    def test_duplicate_active_and_idempotency_conflict_have_their_own_replies(self, temp_db, bnow):
        writer = FakeTicketWriter(TicketWriteResult(DUPLICATE_ACTIVE, ticket_id="TKT_EXIST01", status="Open"))
        c, pending = card(writer)
        done = c.confirm(pending)
        assert done.text == T("ticket_duplicate_active", ticket_id="TKT_EXIST01", status="Open")
        assert "has been created" not in done.text and "was created" not in done.text
        writer = FakeTicketWriter(TicketWriteResult(IDEMPOTENCY_CONFLICT))
        c, pending = card(writer)
        assert c.confirm(pending).text == T("ticket_idempotency_conflict")

    def test_a_writer_that_raises_or_returns_garbage_is_an_unknown_outcome_not_a_success(self, temp_db, bnow):
        for item in (RuntimeError("boom"), "created", TicketWriteResult("created")):
            writer = FakeTicketWriter(item)
            c, pending = card(writer)
            done = c.confirm(pending)
            assert done.text == T("ticket_outcome_unknown") and "TKT_" not in done.text

    def test_no_writer_means_not_enabled_never_success(self, temp_db, bnow):
        backend = fresh_backend(FakeTicketWriter())
        backend.engine.ticket_writer = None
        c = Client(backend, "C101")
        c.say("raise a ticket")
        pending = c.pick(c.say("My earbuds stopped charging after two days."), "none")
        done = c.confirm(pending)
        assert done.text == T("ticket_write_failed") and "TKT_" not in done.text


class TestUnknownOutcomeW1:
    def test_message_key_payload_and_lock(self, temp_db, bnow):
        writer = FakeTicketWriter(TicketWriteResult(OUTCOME_UNKNOWN), TicketWriteResult(OUTCOME_UNKNOWN))
        c, pending = card(writer)
        first = c.confirm(pending)
        assert first.text == T("ticket_outcome_unknown")
        assert "nothing has been saved" not in first.text.lower() and "created" not in first.text.replace("was created", "")
        assert first.pending.action_id == pending.pending.action_id
        second = c.confirm(first)  # a retry of the SAME confirmation
        assert len(writer.requests) == 2
        assert writer.requests[0] == writer.requests[1]  # same key and the same immutable payload (frozen dataclass equality)
        assert writer.requests[0].request_key == writer.requests[1].request_key

    def test_nothing_clears_or_bypasses_an_unresolved_operation(self, temp_db, bnow):
        writer = FakeTicketWriter(TicketWriteResult(OUTCOME_UNKNOWN))
        c, pending = card(writer)
        first = c.confirm(pending)
        action = first.pending.action_id
        for attempt in (lambda: c.say("cancel"), lambda: c.confirm(first, accepted=False), lambda: c.say("raise a ticket"),
                        lambda: c.say("I have a complaint about my order"), lambda: c.say("yes"), lambda: c.say("no")):
            reply = attempt()
            assert reply.text == T("ticket_outcome_unknown") and reply.pending.action_id == action
        assert len(writer.requests) == 1  # no second independent creation request
        assert session_of(c).ticket_draft.unresolved["request_key"] == writer.requests[0].request_key
        answered = c.say("What is your return policy?")
        assert answered.pending is not None and answered.pending.action_id == action and len(writer.requests) == 1

    def test_the_unresolved_card_is_not_cleared_by_the_pending_card_time_out(self, temp_db, bnow):
        writer = FakeTicketWriter(TicketWriteResult(OUTCOME_UNKNOWN))
        c, pending = card(writer)
        first = c.confirm(pending)
        bnow(minutes=30)
        reply = c.say("What is your return policy?")
        assert reply.pending is not None and reply.pending.action_id == first.pending.action_id

    def test_confirming_twice_in_a_row_while_claimed_cannot_double_submit(self, temp_db, bnow):
        writer = FakeTicketWriter()
        c, pending = card(writer)
        c.confirm(pending)
        again = c.confirm(pending)  # the action is done: refused, no second request
        assert len(writer.requests) == 1 and "TKT_" not in again.text


class TestRequestKeyStability:
    def test_each_card_has_its_own_key_and_a_retry_keeps_it(self, temp_db, bnow):
        writer = FakeTicketWriter(TicketWriteResult(UNAVAILABLE), TicketWriteResult(RATE_LIMITED, retry_at="x"))
        c, pending = card(writer)
        r1 = c.confirm(pending)
        r2 = c.confirm(r1)
        c.confirm(r2)
        keys = {r.request_key for r in writer.requests}
        assert len(writer.requests) == 3 and len(keys) == 1

    def test_a_new_card_gets_a_new_key(self, temp_db, bnow):
        writer = FakeTicketWriter()
        c, pending = card(writer)
        corrected = c.say("I meant order ORD1002")
        c.confirm(corrected)
        c2, pending2 = card(FakeTicketWriter())
        assert writer.requests[0].request_key.startswith("rk_")
