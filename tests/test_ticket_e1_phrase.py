"""E-1 while a description is awaited: the exact words "raise a ticket" on word boundaries (verification pass).

Kept apart from the B-10 closed list. No synonyms, stemming or other intent; a question or a negated sentence is not a request.
"""

from __future__ import annotations

import pytest

from app.orchestrator import ticket_flow
from app.tools import flow_texts
from backend_helpers import Client
from ticket_writer_fakes import FakeTicketWriter, fresh_backend, session_of

T = flow_texts.text


def client(writer=None):
    return Client(fresh_backend(writer), "C101")


def draft(c):
    return session_of(c).ticket_draft


class TestPhraseMatching:
    @pytest.mark.parametrize("text", [
        "raise a ticket", "I want to raise a ticket", "please raise a ticket", "Please RAISE A TICKET.",
        "I want to raise a ticket because my screen cracked", "raise a ticket!"])
    def test_the_phrase_is_a_new_ticket_request(self, text):
        assert ticket_flow.contains_raise_a_ticket(text) is True
        assert ticket_flow.classify_draft_message(text) == "conflict"

    @pytest.mark.parametrize("text", [
        "my ticket status is wrong", "The ticket I raised last week was never answered", "the cinema ticket price was wrong",
        "raise a ticketing issue", "I raised a ticket yesterday", "raise  a  ticket", "ticket", "raise the ticket price",
        "how do I raise a ticket?", "don't raise a ticket", "no need to raise a ticket", "never raise a ticket for this"])
    def test_everything_else_is_not_a_phrase_request(self, text):
        assert ticket_flow.contains_raise_a_ticket(text) is False
        assert ticket_flow.classify_draft_message(text) != "conflict" or ticket_flow.is_ticket_request(text)

    def test_the_phrase_is_not_on_the_b10_closed_list_and_the_list_is_unchanged(self):
        assert "raise a ticket" not in ticket_flow.BARE_ENTRIES
        assert ticket_flow.is_bare_description("raise a ticket") is False
        assert ticket_flow.validate_description("I want to raise a ticket") == "ok"

    def test_idle_starts_do_not_widen(self):
        # a how-to question or a sentence merely containing the words must not start a draft from idle
        assert ticket_flow.is_ticket_request("how do I raise a ticket?") is False
        assert ticket_flow.is_ticket_request("don't raise a ticket") is False


class TestWhileADescriptionIsAwaited:
    @pytest.mark.parametrize("text", ["I want to raise a ticket", "please raise a ticket", "raise a ticket"])
    def test_it_offers_keep_or_discard_and_is_never_captured_as_the_description(self, temp_db, bnow, text):
        writer = FakeTicketWriter()
        c = client(writer)
        c.say("raise a ticket")
        reply = c.say(text)
        assert reply.choice is not None and reply.text == T("ticket_conflict_question")
        d = draft(c)
        assert d.description is None and d.attempts == 0  # not a description and not an invalid attempt (B-11)
        assert writer.requests == []

    @pytest.mark.parametrize("text", [
        "The ticket I raised last week was never answered", "my ticket status is wrong",
        "the cinema ticket price was wrong on my order", "My order has not arrived yet"])
    def test_ordinary_descriptions_mentioning_ticket_are_captured_normally(self, temp_db, bnow, text):
        c = client()
        c.say("raise a ticket")
        reply = c.say(text)
        assert reply.text != T("ticket_conflict_question")
        assert draft(c).description == text and draft(c).stage in ("metadata", "preview")

    def test_a_question_about_raising_a_ticket_is_not_a_conflict(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say("how do I raise a ticket?")
        assert reply.text != T("ticket_conflict_question") and draft(c).description is None


class TestTypedNo:
    def test_typed_no_is_accepted_as_description_text_while_awaiting(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        assert "no" not in ticket_flow.BARE_ENTRIES
        reply = c.say("no")
        assert draft(c).description == "no" and reply.text != T("ticket_prompt_two_bare")

    def test_typed_no_leaves_the_preview_open(self, temp_db, bnow):
        writer = FakeTicketWriter()
        c = client(writer)
        c.say("raise a ticket")
        reply = c.say("My earbuds stopped charging after two days.")
        if reply.choice is not None:
            reply = c.pick(reply, "none")
        card = reply.pending
        assert card is not None
        after = c.say("no")
        assert after.pending is not None and after.pending.action_id == card.action_id
        assert draft(c).stage == "preview" and draft(c).description == "My earbuds stopped charging after two days."
        assert writer.requests == []
