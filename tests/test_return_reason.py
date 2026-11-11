"""Return reason provenance: the stored reason is the customer's own message.

* ``reason_category`` comes only from the customer's explicit selection.
* ``reason`` is the exact customer-authored message. The model's own
  ``reason`` argument is ignored, nothing is trimmed or rewritten, and an
  over-long message is sent back for shortening instead of truncated.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.backend import Backend, Settings
from app.db import repository
from app.db.errors import DatabaseOperationalError
from app.db.validation import RETURN_REASON_MAX
from app.tools import llm_tool_definitions
from backend_helpers import Client, ScriptedLLM, calls

PREFIX = "I want to return item IT101 from order ORD1001. "
EXACT = PREFIX + "\n  Changed my mind\tabout the colour — too bright \U0001F600  "


def rows(raw, sql="SELECT * FROM return_requests"):
    return [dict(r) for r in raw.execute(sql).fetchall()]


@pytest.fixture
def backend(temp_db):
    return Backend(llm=ScriptedLLM(), settings=Settings())


def submit(c, text, category="standard"):
    first = c.say(text)
    assert first.status == "needs_choice", first.text
    pending = c.pick(first, category)
    assert pending.status == "needs_confirmation", pending.text
    return c.confirm(pending)


class TestStoredReason:
    def test_exact_customer_text_is_stored(self, backend, raw):
        out = submit(Client(backend, "C101"), EXACT)
        assert "RET_" in out.text
        assert rows(raw)[0]["reason"] == EXACT  # byte for byte: spaces, tab, newline, emoji

    def test_llm_supplied_reason_is_ignored(self, temp_db, raw):
        llm = ScriptedLLM(chat=lambda s, m, t: calls(("start_return_request", {
            "order_id": "ORD1001", "item_id": "IT101",
            "reason": "LLM-INVENTED: the customer says the device exploded"})))
        message = "Please process my return, the colour is too bright for me"
        out = submit(Client(Backend(llm=llm), "C101"), message)
        assert "RET_" in out.text
        stored = rows(raw)[0]["reason"]
        assert stored == message and "LLM-INVENTED" not in stored

    def test_tool_schema_does_not_offer_a_reason_or_category(self):
        spec = next(d for d in llm_tool_definitions() if d["name"] == "start_return_request")
        assert set(spec["parameters"]["properties"]) == {"order_id", "item_id"}

    def test_model_cannot_choose_the_category(self, temp_db, raw):
        llm = ScriptedLLM(chat=lambda s, m, t: calls(("start_return_request", {
            "order_id": "ORD1001", "item_id": "IT101", "reason_category": "damaged"})))
        reply = Client(Backend(llm=llm), "C101").say("return it, the colour is too bright for me")
        assert reply.status == "ok" and reply.choice is None and rows(raw) == []

    @pytest.mark.parametrize("category", ["standard", "wrong_product", "damaged"])
    def test_selected_category_is_what_is_stored(self, backend, raw, category):
        # ORD1005 was delivered 36 hours ago, so all three categories are in their windows.
        text = "I want to return item IT105 from order ORD1005. The shop sent the wrong colour."
        submit(Client(backend, "C103"), text, category)
        saved = rows(raw)[0]
        assert saved["reason_category"] == category
        assert saved["reason"] == text

    def test_reason_comes_from_the_message_that_requested_the_return(self, backend, raw):
        c = Client(backend, "C101")
        c.say("hello")  # earlier customer text is not mixed in
        submit(c, PREFIX + "Too bright for me.")
        assert rows(raw)[0]["reason"] == PREFIX + "Too bright for me."


class TestOversizedReason:
    def message(self, total):
        return PREFIX + "x" * (total - len(PREFIX))

    def test_boundary_1000_is_accepted_and_stored_exactly(self, backend, raw):
        text = self.message(RETURN_REASON_MAX)
        assert len(text) == 1000
        submit(Client(backend, "C101"), text)
        assert rows(raw)[0]["reason"] == text

    def test_1001_is_refused_without_truncation_and_creates_nothing(self, backend, raw):
        c = Client(backend, "C101")
        reply = c.say(self.message(RETURN_REASON_MAX + 1))
        assert reply.status == "ok" and reply.choice is None and reply.pending is None
        assert "1001" in reply.text and "1000" in reply.text and "shorter" in reply.text
        assert rows(raw) == []

    def test_after_refusal_a_shorter_message_is_accepted_and_stored_exactly(self, backend, raw):
        c = Client(backend, "C101")
        c.say(self.message(1500))
        short = "It is too bright for me."
        choice = c.say(short)
        assert choice.status == "needs_choice"
        c.confirm(c.pick(choice, "standard"))
        assert rows(raw)[0]["reason"] == short  # not the over-long text, not a truncation of it

    def test_a_second_over_long_message_is_also_refused(self, backend, raw):
        c = Client(backend, "C101")
        c.say(self.message(1200))
        again = c.say("y" * 1100)
        assert again.choice is None and "shorter" in again.text and rows(raw) == []

    def test_model_cannot_bypass_the_limit_with_a_short_reason(self, temp_db, raw):
        llm = ScriptedLLM(chat=lambda s, m, t: calls(("start_return_request", {
            "order_id": "ORD1001", "item_id": "IT101", "reason": "short"})))
        reply = Client(Backend(llm=llm), "C101").say("return it because " + "z" * 1500)
        assert reply.choice is None and "shorter" in reply.text and rows(raw) == []


class TestAskingForAReason:
    def test_bare_request_asks_for_an_explanation(self, backend, raw):
        c = Client(backend, "C101")
        reply = c.say("I want to return item IT101 from order ORD1001")
        assert reply.status == "ok" and reply.choice is None and "why" in reply.text.lower()
        assert rows(raw) == []

    def test_next_message_becomes_the_exact_reason(self, backend, raw):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        reason = " The stitching is not what I expected. "
        choice = c.say(reason)
        assert choice.status == "needs_choice"
        c.confirm(c.pick(choice, "standard"))
        assert rows(raw)[0]["reason"] == reason

    @pytest.mark.parametrize("unusable", ["hi", "yes", "ok", "confirm", "NUL\x00 inside", "   "])
    def test_unusable_replies_ask_again(self, backend, raw, unusable):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        reply = c.say(unusable)
        assert reply.choice is None and reply.pending is None and rows(raw) == []

    def test_typed_yes_never_becomes_a_reason_or_a_confirmation(self, backend, raw):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        assert c.say("yes").choice is None
        assert rows(raw) == []

    def test_asking_for_a_reason_expires(self, backend, raw, bnow):
        c = Client(backend, "C101")
        c.say("I want to return item IT101 from order ORD1001")
        bnow(minutes=11)
        reply = c.say("hello")
        assert "timed out" in reply.text and reply.choice is None
        assert backend.registry.get(c.state._handle).draft is None

    def test_other_customers_order_is_refused_before_asking(self, backend):
        reply = Client(backend, "C101").say("I want to return item IT103 from order ORD1003")
        assert "couldn't find" in reply.text.lower()


class TestFailedConfirmation:
    def test_failed_write_stores_nothing_and_a_new_request_keeps_its_own_reason(
            self, backend, raw, monkeypatch):
        c = Client(backend, "C101")
        first_text = PREFIX + "FIRST reason text."
        pending = c.pick(c.say(first_text), "standard")
        real = repository.create_return_request

        def boom(*args, **kwargs):
            raise DatabaseOperationalError("down")

        monkeypatch.setattr(repository, "create_return_request", boom)
        failed = c.confirm(pending)
        assert "RET_" not in failed.text and "couldn't complete" in failed.text
        assert rows(raw) == []

        monkeypatch.setattr(repository, "create_return_request", real)
        replay = c.confirm(pending)  # the consumed action cannot be reused
        assert "already handled" in replay.text and rows(raw) == []

        second_text = PREFIX + "SECOND reason text."
        out = c.confirm(c.pick(c.say(second_text), "standard"))
        assert "RET_" in out.text
        saved = rows(raw)
        assert len(saved) == 1 and saved[0]["reason"] == second_text
        assert "FIRST" not in saved[0]["reason"]

    def test_declining_stores_nothing(self, backend, raw):
        c = Client(backend, "C101")
        pending = c.pick(c.say(PREFIX + "Too bright."), "standard")
        c.confirm(pending, accepted=False)
        assert rows(raw) == []
        assert backend.registry.get(c.state._handle).draft is None


class TestReasonFollowUp:
    """While a reason is awaited, only a reason is stored; other messages are not."""

    BARE = "I want to return item IT101 from order ORD1001"

    def awaiting(self, temp_db):
        llm = ScriptedLLM()
        backend = Backend(llm=llm, settings=Settings())
        client = Client(backend, "C101")
        first = client.say(self.BARE)
        return backend, llm, client, first

    def test_the_request_tells_the_customer_what_will_be_stored_and_how_to_stop(self, temp_db):
        _backend, _llm, _client, first = self.awaiting(temp_db)
        assert "saved exactly as you write it" in first.text and "reason for this return" in first.text
        assert "Type cancel" in first.text

    def test_every_repeat_of_the_request_carries_the_same_notice(self, temp_db):
        _backend, _llm, client, _first = self.awaiting(temp_db)
        for reply in (client.say("y" * 1500), client.say("What is your refund policy?")):
            assert "saved exactly as you write it" in reply.text and "Type cancel" in reply.text

    @pytest.mark.parametrize("unrelated", [
        "What is your refund policy?", "hello", "thanks", "tell me a joke", "status of ORD1003",
        "show my orders", "please cancel my order ORD1002", "Can I change my address?",
        "where is my order", "I meant ORD1002", "how long does delivery take",
    ])
    def test_unrelated_messages_are_not_stored_and_the_draft_survives(self, temp_db, raw, unrelated):
        backend, llm, client, _first = self.awaiting(temp_db)
        session = backend.registry.get(client.state._handle)
        before = dict(session.draft)
        chats = llm.chat_calls
        reply = client.say(unrelated)
        assert reply.status == "ok" and reply.choice is None and reply.pending is None
        assert "still waiting for your reason" in reply.text and "type cancel" in reply.text.lower()
        assert dict(session.draft) == before  # same order, item and the ORIGINAL expiry
        assert llm.chat_calls == chats and llm.complete_calls == []  # no model involved, no paraphrase
        assert rows(raw) == []
        # The customer can still give the real reason, and exactly that text is stored.
        reason = "The stitching is not what I expected."
        out = client.confirm(client.pick(client.say(reason), "standard"))
        assert "RET_" in out.text
        assert [r["reason"] for r in rows(raw)] == [reason]

    @pytest.mark.parametrize("reason", [
        "It is too small for me.", "The colour is wrong.", "It stopped working after two days",
        "Because it does not fit my phone", "ORD1001 arrived scratched", "Not what I expected, sorry",
    ])
    def test_real_reasons_are_accepted_and_stored_exactly(self, temp_db, raw, reason):
        _backend, _llm, client, _first = self.awaiting(temp_db)
        choice = client.say(reason)
        assert choice.status == "needs_choice", choice.text
        client.confirm(client.pick(choice, "standard"))
        assert [r["reason"] for r in rows(raw)] == [reason]

    def test_unrelated_messages_do_not_extend_the_wait(self, temp_db, bnow):
        backend, _llm, client, _first = self.awaiting(temp_db)
        bnow(minutes=6)
        client.say("What is your refund policy?")
        bnow(minutes=5)  # 11 minutes after the request
        reply = client.say("hello")
        assert "timed out" in reply.text
        assert backend.registry.get(client.state._handle).draft is None

    def test_typed_cancel_ends_the_wait_and_nothing_is_stored(self, temp_db, raw):
        backend, _llm, client, _first = self.awaiting(temp_db)
        client.say("What is your refund policy?")
        assert "cancelled" in client.say("cancel").text
        assert backend.registry.get(client.state._handle).draft is None and rows(raw) == []


class TestConfirmationShowsTheStoredReason:
    """The customer sees, before confirming, exactly the text that will be stored."""

    def pending_for(self, backend, text, category="standard", customer="C101"):
        client = Client(backend, customer)
        return client, client.pick(client.say(text), category)

    def test_the_summary_contains_the_exact_reason(self, backend):
        _client, pending = self.pending_for(backend, EXACT)
        assert pending.status == "needs_confirmation"
        assert EXACT in pending.pending.summary  # byte for byte: spaces, tab, newline, emoji
        assert "saved exactly as you wrote it" in pending.pending.summary
        assert "Nothing is saved until you confirm" in pending.pending.summary

    def test_what_is_shown_is_what_is_stored(self, backend, raw):
        client, pending = self.pending_for(backend, PREFIX + "The shop sent the wrong colour.")
        shown = pending.pending.summary
        client.confirm(pending)
        stored = rows(raw)[0]["reason"]
        assert stored in shown and stored == PREFIX + "The shop sent the wrong colour."

    def test_in_window_summary_names_item_order_and_category(self, backend):
        text = "I want to return item IT105 from order ORD1005. The shop sent the wrong colour."
        _client, pending = self.pending_for(backend, text, "wrong_product", customer="C103")
        summary = pending.pending.summary
        assert "IT105" in summary and "ORD1005" in summary and "wrong product" in summary and text in summary

    def test_a_model_supplied_reason_never_appears_in_the_summary(self, temp_db):
        llm = ScriptedLLM(chat=lambda s, m, t: calls(("start_return_request", {
            "order_id": "ORD1001", "item_id": "IT101", "reason": "LLM-INVENTED REASON"})))
        client = Client(Backend(llm=llm), "C101")
        message = "Please process my return, the colour is too bright for me"
        pending = client.pick(client.say(message), "standard")
        assert message in pending.pending.summary and "LLM-INVENTED" not in pending.pending.summary

    def test_a_reason_at_the_1000_limit_is_shown_in_full(self, backend):
        text = PREFIX + "x" * (1000 - len(PREFIX))
        _client, pending = self.pending_for(backend, text)
        assert text in pending.pending.summary

    def test_an_ineligible_request_shows_no_confirmation_and_so_no_reason(self, backend):
        client = Client(backend, "C101")
        reply = client.pick(client.say(PREFIX + "It stopped working."), "damaged")  # 72h > 48h window
        assert reply.pending is None and "stopped working" not in reply.text
