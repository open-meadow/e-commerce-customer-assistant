"""Choice expiry, typed cancel, and that typed words never confirm a write."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.backend import Backend, Settings
from backend_helpers import Client, ScriptedLLM

REQUEST = "I want to return item IT101 from order ORD1001 because I changed my mind about it"


def rows(raw, table="return_requests"):
    return [dict(r) for r in raw.execute(f"SELECT * FROM {table}").fetchall()]


@pytest.fixture
def backend(temp_db):
    return Backend(llm=ScriptedLLM(), settings=Settings())


def session(backend, c):
    return backend.registry.get(c.state._handle)


class TestChoiceExpiry:
    def test_choice_expires_after_the_ttl_and_cannot_execute(self, backend, raw, bnow):
        c = Client(backend, "C101")
        choice = c.say(REQUEST)
        bnow(minutes=11)
        out = c.pick(choice, "standard")
        assert "expired" in out.text and out.pending is None and out.choice is None
        assert session(backend, c).choice is None and session(backend, c).draft is None
        assert backend.registry._pending == {} and rows(raw) == []

    def test_just_inside_the_ttl_still_works(self, backend, bnow):
        c = Client(backend, "C101")
        choice = c.say(REQUEST)
        bnow(minutes=9, seconds=59)
        assert c.pick(choice, "standard").status == "needs_confirmation"

    def test_ttl_follows_the_pending_action_ttl_setting(self, temp_db, bnow):
        backend = Backend(llm=ScriptedLLM(), settings=Settings(pending_ttl=timedelta(minutes=1)))
        c = Client(backend, "C101")
        choice = c.say(REQUEST)
        bnow(minutes=2)
        assert "expired" in c.pick(choice, "standard").text

    def test_redisplaying_does_not_extend_the_expiry(self, backend, bnow):
        c = Client(backend, "C101")
        choice = c.say(REQUEST)
        bnow(minutes=6)
        again = c.say("hello?")  # re-shows the same choice
        assert again.status == "needs_choice" and again.choice.prompt_id == choice.choice.prompt_id
        bnow(minutes=5)  # 11 minutes after creation, only 5 after the re-display
        assert "expired" in c.pick(again, "standard").text

    def test_irrelevant_text_cannot_keep_a_choice_alive(self, backend, bnow):
        c = Client(backend, "C101")
        c.say(REQUEST)
        for _ in range(5):
            bnow(minutes=3)
            c.say("still here")
        assert session(backend, c).choice is None  # the session stayed active, the choice did not

    def test_text_after_expiry_is_handled_normally_with_a_notice(self, backend, bnow):
        c = Client(backend, "C101")
        c.say(REQUEST)
        bnow(minutes=11)
        reply = c.say("hello")
        assert reply.status == "ok" and "timed out" in reply.text and reply.choice is None

    def test_expired_relation_choice_leaves_issues_untouched(self, temp_db, bnow):
        llm = ScriptedLLM(labels=["related_but_unclear"])
        backend = Backend(llm=llm, settings=Settings())
        c = Client(backend, "C101")
        c.say("My earbuds stopped working after two days")
        choice = c.say("My charger arrived damaged and the box was broken")
        bnow(minutes=11)
        assert "expired" in c.pick(choice, "new_issue").text
        book = session(backend, c).issues
        assert book.active.messages == ["My earbuds stopped working after two days"] and book.suspended is None

    def test_pending_confirmation_still_expires(self, backend, raw, bnow):
        c = Client(backend, "C101")
        pending = c.pick(c.say(REQUEST), "standard")
        bnow(minutes=11)
        assert "expired" in c.confirm(pending).text and rows(raw) == []


class TestTypedCancel:
    @pytest.mark.parametrize("word", ["cancel", "CANCEL", "Cancel", "  cancel  ", "cancel!", "Cancel."])
    def test_cancels_an_open_choice(self, backend, raw, word):
        c = Client(backend, "C101")
        choice = c.say(REQUEST)
        out = c.say(word)
        assert "cancelled" in out.text and out.status == "ok" and out.choice is None
        assert session(backend, c).choice is None and session(backend, c).draft is None
        assert "no longer available" in c.pick(choice, "standard").text and rows(raw) == []

    def test_cancels_a_pending_confirmation_and_revokes_the_action(self, backend, raw):
        c = Client(backend, "C101")
        pending = c.pick(c.say(REQUEST), "standard")
        out = c.say("Cancel")
        assert "cancelled" in out.text and out.pending is None
        late = c.confirm(pending)
        assert "RET_" not in late.text and "cancelled" in late.text
        assert rows(raw) == []

    def test_cancels_the_request_for_a_reason(self, backend):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        assert "cancelled" in c.say("cancel").text
        assert session(backend, c).draft is None

    def test_cancel_with_nothing_open_is_just_a_message(self, backend):
        reply = Client(backend, "C101").say("cancel")
        assert reply.status in ("ok", "needs_choice") and "Nothing was changed" not in reply.text

    def test_cancelling_a_ticket_proposal_does_not_resolve_the_issue(self, backend, raw):
        c = Client(backend, "C101")
        c.say("My earbuds stopped working after two days")
        pending = c.say("create a ticket for this")
        assert pending.status == "needs_confirmation"
        assert "cancelled" in c.say("cancel").text
        book = session(backend, c).issues
        assert book.active is not None and book.active.messages == ["My earbuds stopped working after two days"]
        assert rows(raw, "support_tickets") == []
        # The same issue can still produce a ticket afterwards.
        again = c.say("create a ticket for this")
        assert again.status == "needs_confirmation"
        assert "TKT_" in c.confirm(again).text

    def test_cancelling_a_choice_keeps_both_tracked_issues(self, temp_db):
        llm = ScriptedLLM(labels=["new_issue", "new_issue"])
        backend = Backend(llm=llm, settings=Settings())
        c = Client(backend, "C101")
        for text in ("My earbuds stopped working after two days", "My charger arrived damaged and broken"):
            c.say(text)
        c.say("My monitor has a defect, the screen is cracked")  # third issue -> choice
        assert "cancelled" in c.say("cancel").text
        book = session(backend, c).issues
        assert book.active is not None and book.suspended is not None


class TestTypedAcknowledgementsNeverConfirm:
    @pytest.mark.parametrize("word", ["yes", "Yes", "confirm", "ok", "okay do it", "sure", "yes please", "y"])
    def test_pending_return_is_not_confirmed_by_text(self, backend, raw, word):
        c = Client(backend, "C101")
        pending = c.pick(c.say(REQUEST), "standard")
        out = c.say(word)
        assert out.status == "needs_confirmation" and out.pending.action_id == pending.pending.action_id
        assert rows(raw) == []

    @pytest.mark.parametrize("word", ["yes", "confirm", "ok"])
    def test_pending_ticket_is_not_confirmed_by_text(self, backend, raw, word):
        c = Client(backend, "C101")
        c.say("My earbuds stopped working after two days")
        pending = c.say("create a ticket for this")
        out = c.say(word)
        assert out.status == "needs_confirmation" and out.pending.action_id == pending.pending.action_id
        assert rows(raw, "support_tickets") == []

    def test_typed_yes_does_not_pick_a_choice(self, backend):
        c = Client(backend, "C101")
        c.say(REQUEST)
        assert c.say("yes").status == "needs_choice"
        assert backend.registry._pending == {}
