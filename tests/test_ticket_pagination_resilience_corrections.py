"""Phase 1, step 1d (remaining approved items): paging, malformed-row tolerance and typed text while a ticket card is open
(design NT-54 to NT-56, NT-62, NT-64; harness G06, G19, C19, D12, D25, D26).

Not covered, because not implemented: category corrections and description corrections (cancel and start again).
Typed text never confirms anything; only the Confirm decision does.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from app.backend import Backend
from app.db import repository
from app.db.errors import DatabaseDataIntegrityError
from app.orchestrator import guard
from app.tools import flow_texts
from backend_helpers import Client, ScriptedLLM

BASE = datetime(2026, 11, 10, 0, 0, 0, tzinfo=timezone.utc)
STORE = "My earbuds from ORD1001 stopped working after two days"
ID = re.compile(r"TKT_[0-9a-f]{32}")


def seed_tickets(set_now, n, customer="C101"):
    ids = []
    for i in range(n):
        set_now(BASE + timedelta(minutes=i))
        ids.append(repository.create_support_ticket(customer, "general", f"issue {i}", "summary")["ticket_id"])
    set_now(BASE + timedelta(days=1))
    return ids  # oldest first


def rows(raw):
    return [dict(r) for r in raw.execute("SELECT * FROM support_tickets").fetchall()]


# ---------------------------------------------------------------- paging (G06, NT-62)

class TestPaging:
    def test_every_ticket_is_reachable_exactly_once_newest_first(self, temp_db, set_now):
        ids = seed_tickets(set_now, 25)
        llm = ScriptedLLM()
        c = Client(Backend(llm=llm), "C101")
        first = c.say("show my tickets")
        assert "You have 25 support ticket(s)" in first.text and "Showing the 10 most recent of 25." in first.text
        assert "type: show more tickets" in first.text
        seen = ID.findall(first.text)
        calls_after_first = llm.chat_calls
        second = c.say("show more tickets")
        third = c.say("more tickets")
        assert llm.chat_calls == calls_after_first  # the next pages are rendered by code: no model call
        assert "Showing 11 to 20 of 25." in second.text and "Showing 21 to 25 of 25." in third.text
        assert "type: show more tickets" in second.text and "type: show more tickets" not in third.text
        seen += ID.findall(second.text) + ID.findall(third.text)
        assert seen == list(reversed(ids))  # all 25, once each, newest first, stable order

    def test_after_the_last_page_the_request_is_no_longer_intercepted(self, temp_db, set_now):
        seed_tickets(set_now, 11)
        c = Client(Backend(llm=ScriptedLLM()), "C101")
        c.say("show my tickets")
        last = c.say("show more tickets")
        assert "Showing 11 to 11 of 11." in last.text
        sess = c.backend.registry.get(c.state._handle)
        assert sess.ticket_next_offset is None

    def test_show_more_without_a_list_is_not_treated_as_a_paging_request(self, temp_db, set_now):
        seed_tickets(set_now, 15)
        c = Client(Backend(llm=ScriptedLLM()), "C101")
        out = c.say("show more tickets")  # no list was shown first: the normal path handles it, nothing is invented
        assert "Showing 11 to" not in out.text

    def test_a_short_list_has_no_hint_and_no_paging_state(self, temp_db, set_now):
        seed_tickets(set_now, 3)
        backend = Backend(llm=ScriptedLLM())
        c = Client(backend, "C101")
        out = c.say("show my tickets")
        assert "show more tickets" not in out.text
        assert backend.registry.get(c.state._handle).ticket_next_offset is None

    def test_pages_only_ever_hold_this_customers_rows(self, temp_db, set_now):
        seed_tickets(set_now, 12, "C102")
        mine = seed_tickets(set_now, 11, "C101")
        c = Client(Backend(llm=ScriptedLLM()), "C101")
        texts = c.say("show my tickets").text + c.say("show more tickets").text
        assert set(ID.findall(texts)) == set(mine)

    @pytest.mark.parametrize("text", ["show more tickets", "more tickets", "next tickets", "Show me the rest of my tickets.",
                                      "please show the remaining support tickets", "show older tickets"])
    def test_the_paging_phrases(self, text):
        assert guard.is_more_tickets_request(text)

    @pytest.mark.parametrize("text", ["show my tickets", "I want more tickets created", "more", "tickets", "what is my order status"])
    def test_other_phrases_are_not_paging_requests(self, text):
        assert not guard.is_more_tickets_request(text)

    def test_the_repository_page_function_validates_its_arguments(self, temp_db):
        for bad in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": "5"}):
            with pytest.raises(ValueError):
                repository.list_customer_tickets_page("C101", **bad)


# ---------------------------------------------------------------- one malformed row no longer hides the rest (G19, NT-64)

class TestMalformedRows:
    def future_row(self, raw):
        raw.execute("INSERT INTO support_tickets (ticket_id, customer_id, order_id, issue_type, description, conversation_summary,"
                    " status, priority, created_at) VALUES ('TKT_' || lower(hex(randomblob(16))), 'C101', NULL, 'general', 'd', 's',"
                    " 'Open', 'Normal', '2099-01-01T00:00:00Z')")
        raw.commit()

    def test_a_future_dated_row_is_skipped_and_counted(self, temp_db, set_now, raw):
        ids = seed_tickets(set_now, 3)
        self.future_row(raw)
        page = repository.list_customer_tickets_page("C101")
        assert page["total"] == 3 and page["skipped"] == 1
        assert [t["ticket_id"] for t in page["tickets"]] == list(reversed(ids))

    def test_the_customer_still_sees_the_valid_tickets(self, temp_db, set_now, raw):
        ids = seed_tickets(set_now, 3)
        self.future_row(raw)
        out = Client(Backend(llm=ScriptedLLM()), "C101").say("show my tickets")
        assert out.status == "ok" and set(ID.findall(out.text)) == set(ids) and "You have 3 support ticket(s)" in out.text

    def test_the_original_strict_read_is_unchanged(self, temp_db, set_now, raw):
        seed_tickets(set_now, 2)
        self.future_row(raw)
        with pytest.raises(DatabaseDataIntegrityError):
            repository.get_customer_tickets("C101")

    def test_a_skipped_row_is_logged_with_opaque_references_only(self, temp_db, set_now, raw, caplog):
        import logging
        seed_tickets(set_now, 1)
        raw.execute("INSERT INTO support_tickets (ticket_id, customer_id, order_id, issue_type, description, conversation_summary,"
                    " status, priority, created_at) VALUES ('TKT_" + "e" * 32 + "', 'C101', NULL, 'general', 'SECRET-DESCRIPTION', 'SECRET-SUMMARY',"
                    " 'Open', 'Normal', '2099-01-01T00:00:00Z')")
        raw.commit()
        with caplog.at_level(logging.WARNING, logger="app.db.repository"):
            page = repository.list_customer_tickets_page("C101")
        assert page["skipped"] == 1
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "skipped" in logged and "TKT_" + "e" * 32 in logged and "customer_ref=" in logged
        assert "C101" not in logged and "SECRET" not in logged  # no raw customer ID and no ticket text

    def test_the_unreadable_row_text_is_never_exposed(self, temp_db, set_now, raw):
        seed_tickets(set_now, 1)
        self.future_row(raw)
        out = Client(Backend(llm=ScriptedLLM()), "C101").say("show my tickets")
        assert "2099" not in out.text


# ---------------------------------------------------------------- typed text while a ticket card is open (C19, D12, D25, D26)

def open_card(backend=None, customer="C101"):
    c = Client(backend or Backend(llm=ScriptedLLM()), customer)
    c.say("I want to speak to a human agent")
    card = c.say(STORE)
    assert card.status == "needs_confirmation" and "order ORD1001" in card.pending.summary
    return c, card


class TestTypedTextWithAnOpenCard:
    @pytest.mark.parametrize("word", ["no", "No.", "nope", "I changed my mind", "never mind"])
    def test_a_plain_typed_no_still_only_gets_the_reminder_and_the_card_stays(self, temp_db, raw, word):
        """Existing tests pin this. Whether a plain no should close the card is an open owner decision (not decided here)."""
        c, card = open_card()
        out = c.say(word)
        assert out.text == flow_texts.text("remind_pending", "en") and out.pending.action_id == card.pending.action_id
        assert rows(raw) == []
        assert "has been created" in c.confirm(card).text

    @pytest.mark.parametrize("word", ["not yet", "wait", "hold on", "Wait a moment"])
    def test_not_yet_keeps_the_card_says_so_and_it_can_still_be_confirmed(self, temp_db, raw, word):
        c, card = open_card()
        out = c.say(word)
        assert out.text == flow_texts.text("pending_paused", "en") and out.pending is not None
        assert out.pending.action_id == card.pending.action_id and rows(raw) == []
        assert "has been created" in c.confirm(card).text and len(rows(raw)) == 1

    @pytest.mark.parametrize("word", ["yes", "confirm", "ok go ahead", "yes, but change the order ID"])
    def test_typed_agreement_never_submits(self, temp_db, raw, word):
        c, card = open_card()
        c.say(word)
        assert rows(raw) == []

    def test_an_unrelated_question_only_gets_the_reminder(self, temp_db, raw):
        c, card = open_card()
        out = c.say("what's the weather in Delhi?")
        assert out.text == flow_texts.text("remind_pending", "en") and out.pending.action_id == card.pending.action_id

    def test_a_question_is_answered_with_the_cards_own_text_and_the_draft_is_kept(self, temp_db, raw):
        c, card = open_card()
        out = c.say("what will this ticket say?")
        assert card.pending.summary in out.text and out.pending is not None
        assert out.pending.action_id == card.pending.action_id and rows(raw) == []

    def test_a_different_owned_order_replaces_the_card_and_needs_a_fresh_confirm(self, temp_db, raw):
        other = [r[0] for r in raw.execute("SELECT order_id FROM orders WHERE customer_id = 'C101' ORDER BY order_id")
                 if r[0] != "ORD1001"][0]
        c, old = open_card()
        new = c.say(f"sorry, it is actually order {other}")
        assert new.status == "needs_confirmation" and f"order {other}" in new.pending.summary
        assert new.pending.action_id != old.pending.action_id and rows(raw) == []
        c.confirm(old)  # the old card was revoked: confirming it creates nothing
        assert rows(raw) == []
        assert "has been created" in c.confirm(new).text
        assert [r["order_id"] for r in rows(raw)] == [other]

    def test_a_foreign_order_leaves_the_old_card_exactly_as_it_was(self, temp_db, raw):
        c, card = open_card()
        out = c.say("no wait it is order ORD1003")  # ORD1003 belongs to another customer
        assert out.text == flow_texts.text("order_not_found_check", "en")
        assert out.pending is not None and out.pending.action_id == card.pending.action_id
        assert "has been created" in c.confirm(card).text
        assert [r["order_id"] for r in rows(raw)] == ["ORD1001"]

    def test_the_same_order_again_is_just_the_usual_reminder(self, temp_db, raw):
        c, card = open_card()
        out = c.say("it is order ORD1001")
        assert out.text == flow_texts.text("remind_pending", "en") and out.pending.action_id == card.pending.action_id

    def test_a_correction_still_creates_exactly_one_ticket(self, temp_db, raw):
        c, old = open_card(Backend(llm=ScriptedLLM()))
        other = [r[0] for r in raw.execute("SELECT order_id FROM orders WHERE customer_id = 'C101' ORDER BY order_id")
                 if r[0] != "ORD1001"][0]
        new = c.say(f"it is order {other}")
        assert new.pending is not None
        c.confirm(old)  # revoked
        assert "has been created" in c.confirm(new).text
        assert len(rows(raw)) == 1 and rows(raw)[0]["order_id"] == other  # one ticket only

    def test_plain_refusal_words_only_count_as_the_whole_message(self):
        assert guard.plain_refusal("no") == "close" and guard.plain_refusal("wait") == "pause"
        for text in ("no problem with ORD1001", "wait for the courier", "I have no idea", "not yet received my refund"):
            assert guard.plain_refusal(text) is None


# ---------------------------------------------------------------- the paging instruction is localized (Hinglish and Hindi are DRAFT)

class TestLocalizedPaging:
    def test_the_hint_names_the_phrase_in_each_language(self):
        assert flow_texts.text("tickets_more_hint", "en").endswith("show more tickets")
        assert flow_texts.text("tickets_more_hint", "hinglish").endswith("aur tickets dikhao")
        assert flow_texts.text("tickets_more_hint", "hi").endswith("और टिकट दिखाइए")

    @pytest.mark.parametrize("language", ["en", "hinglish", "hi"])
    def test_the_phrase_the_hint_names_is_the_phrase_that_works(self, language):
        phrase = flow_texts.text("tickets_more_hint", language).split(": ", 1)[1]
        assert guard.is_more_tickets_request(phrase)

    @pytest.mark.parametrize("text", ["aur tickets dikhao", "Agle tickets dikhaiye", "baaki tickets", "please aur tickets dikhao",
                                      "और टिकट दिखाइए", "अगले टिकट दिखाओ", "बाकी टिकट", "कृपया और टिकट दिखाइए।"])
    def test_hinglish_and_hindi_phrases_are_recognised(self, text):
        assert guard.is_more_tickets_request(text)

    @pytest.mark.parametrize("text", ["aur", "aur order dikhao", "mere tickets dikhao", "और ऑर्डर दिखाइए", "टिकट", "naya ticket banao"])
    def test_other_text_is_not_a_paging_request(self, text):
        assert not guard.is_more_tickets_request(text)

    def test_paging_works_end_to_end_in_hinglish_and_in_hindi(self, temp_db, set_now):
        ids = seed_tickets(set_now, 25)
        backend = Backend(llm=ScriptedLLM())
        c = Client(backend, "C101")
        c.say("show my tickets")
        session = backend.registry.get(c.state._handle)
        session.language = "hinglish"
        second = c.say("aur tickets dikhao")
        assert "mein se 11 se 20 tak" in second.text and "likhiye: aur tickets dikhao" in second.text
        third = c.say("और टिकट दिखाइए")  # Devanagari switches the session language to Hindi
        assert "में से 21 से 25 तक" in third.text and "लिखिए" not in third.text  # last page: no hint
        seen = ID.findall(second.text) + ID.findall(third.text)
        assert seen == list(reversed(ids))[10:]  # pages 2 and 3, database order, nothing repeated or invented

    def test_the_counts_still_come_from_the_database(self, temp_db, set_now):
        seed_tickets(set_now, 12)
        c = Client(Backend(llm=ScriptedLLM()), "C101")
        first = c.say("show my tickets")
        assert "You have 12 support ticket(s)" in first.text and "Showing the 10 most recent of 12." in first.text

    def test_the_review_sheet_marks_the_wording_as_draft_pending_review(self):
        from pathlib import Path
        sheet = (Path(__file__).resolve().parents[1] / "docs" / "localization_review_hinglish_hindi.md").read_text(encoding="utf-8")
        assert "tickets_more_hint" in sheet and "NOT approved" in sheet and "pending fluent human review" in sheet
        assert flow_texts.REVIEW_STATUS["hinglish"].startswith("draft") and flow_texts.REVIEW_STATUS["hi"].startswith("draft")
