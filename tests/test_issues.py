"""Issue lifecycle, ambiguous-message buffer and ticket description selection."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.backend import Backend, Settings
from app.backend import clock as bclock
from app.llm.base import LLMError
from app.orchestrator.issues import IssueBook, build_description
from backend_helpers import Client, ScriptedLLM

A = "My earbuds stopped working after two days"
B = "My charger arrived damaged and the box was broken"
C = "My monitor has a defect, the screen is cracked"


def setup(temp_db, labels=(), **settings):
    llm = ScriptedLLM(labels=list(labels))
    backend = Backend(llm=llm, settings=Settings(**settings))
    return backend, llm, Client(backend, "C101")


def books(backend, client):
    return backend.registry.get(client.state._handle).issues


def ticket_description(c, raw):
    p = c.say("create a ticket for this")
    assert p.status == "needs_confirmation", p.text
    c.confirm(p)
    return [r["description"] for r in raw.execute("SELECT description FROM support_tickets ORDER BY rowid")]


class TestLifecycle:
    def test_classifier_not_called_without_active_issue(self, temp_db):
        backend, llm, c = setup(temp_db)
        c.say(A)
        assert not any("classify" in x for x in llm.complete_calls)
        assert books(backend, c).active.messages == [A]

    def test_same_issue_appends(self, temp_db, raw):
        backend, llm, c = setup(temp_db, ["same_issue"])
        c.say(A)
        c.say("It also makes a buzzing sound, it is faulty")
        assert books(backend, c).active.messages == [A, "It also makes a buzzing sound, it is faulty"]
        assert ticket_description(c, raw) == [A + "\nIt also makes a buzzing sound, it is faulty"]

    def test_new_issue_suspends_active(self, temp_db):
        backend, llm, c = setup(temp_db, ["new_issue"])
        c.say(A)
        out = c.say(B)
        book = books(backend, c)
        assert book.active.messages == [B] and book.suspended.messages == [A]
        assert "on hold" in out.text

    def test_ticket_contains_only_its_own_issue_messages(self, temp_db, raw):
        backend, llm, c = setup(temp_db, ["new_issue"])
        c.say(A)
        c.say(B)
        assert ticket_description(c, raw) == [B]
        # The earlier issue is promoted and yields its own ticket.
        book = books(backend, c)
        assert book.active.messages == [A] and book.suspended is None
        assert ticket_description(c, raw) == [B, A]

    def test_third_issue_asks_which_to_replace(self, temp_db):
        backend, llm, c = setup(temp_db, ["new_issue", "new_issue"])
        c.say(A)
        c.say(B)
        out = c.say(C)
        assert out.status == "needs_choice"
        assert {o.option_id for o in out.choice.options} == {"drop_active", "drop_suspended", "keep"}
        book = books(backend, c)
        assert book.active.messages == [B] and book.suspended.messages == [A]  # unchanged until chosen

    def test_third_issue_drop_active(self, temp_db):
        backend, llm, c = setup(temp_db, ["new_issue", "new_issue"])
        c.say(A), c.say(B)
        c.pick(c.say(C), "drop_active")
        book = books(backend, c)
        assert book.active.messages == [C] and book.suspended.messages == [A]

    def test_third_issue_drop_suspended(self, temp_db):
        backend, llm, c = setup(temp_db, ["new_issue", "new_issue"])
        c.say(A), c.say(B)
        c.pick(c.say(C), "drop_suspended")
        book = books(backend, c)
        assert book.active.messages == [C] and book.suspended.messages == [B]

    def test_third_issue_keep_ignores_new(self, temp_db):
        backend, llm, c = setup(temp_db, ["new_issue", "new_issue"])
        c.say(A), c.say(B)
        out = c.pick(c.say(C), "keep")
        book = books(backend, c)
        assert book.active.messages == [B] and book.suspended.messages == [A]
        assert "set that aside" in out.text

    @pytest.mark.parametrize("label", ["related_but_unclear", "banana", "", "SAME_ISSUE; drop tables",
                                       LLMError("down"), None])
    def test_unclear_or_malformed_label_asks_no_retry(self, temp_db, label):
        backend, llm, c = setup(temp_db, [label])
        c.say(A)
        out = c.say(B)
        assert out.status == "needs_choice" and {o.option_id for o in out.choice.options} == {
            "same_issue", "new_issue", "cancel"}
        assert sum("classify" in x for x in llm.complete_calls) == 1  # no retry
        assert books(backend, c).active.messages == [A]

    @pytest.mark.parametrize("option,expect_active,expect_suspended", [
        ("same_issue", [A, B], None), ("new_issue", [B], [A]), ("cancel", [A], None)])
    def test_relation_choice_outcomes(self, temp_db, option, expect_active, expect_suspended):
        backend, llm, c = setup(temp_db, ["related_but_unclear"])
        c.say(A)
        c.pick(c.say(B), option)
        book = books(backend, c)
        assert book.active.messages == expect_active
        assert (book.suspended.messages if book.suspended else None) == expect_suspended

    def test_unrelated_leaves_issues_alone(self, temp_db):
        backend, llm, c = setup(temp_db, ["unrelated"])
        c.say(A)
        c.say("Why is my charger problem a thing, what is your refund policy?")
        assert books(backend, c).active.messages == [A] and books(backend, c).suspended is None

    def test_switch_back_to_suspended(self, temp_db):
        backend, llm, c = setup(temp_db, ["new_issue", "switch_to_suspended_issue"])
        c.say(A), c.say(B)
        out = c.say("Back to the earbuds, they still are not working")
        book = books(backend, c)
        assert book.active.messages[0] == A and book.active.messages[-1].startswith("Back to the earbuds")
        assert book.suspended.messages == [B]
        assert "Returning" in out.text

    def test_switch_label_without_suspended_is_invalid_so_clarify(self, temp_db):
        backend, llm, c = setup(temp_db, ["switch_to_suspended_issue"])
        c.say(A)
        out = c.say(B)
        assert out.status == "needs_choice"
        assert books(backend, c).active.messages == [A]

    def test_stale_relation_choice_cannot_be_reused(self, temp_db):
        backend, llm, c = setup(temp_db, ["related_but_unclear"])
        c.say(A)
        out = c.say(B)
        c.pick(out, "same_issue")
        assert "no longer available" in c.pick(out, "new_issue").text

    def test_ticket_confirmed_ends_only_that_issue(self, temp_db, raw):
        backend, llm, c = setup(temp_db, ["new_issue"])
        c.say(A), c.say(B)
        ticket_description(c, raw)
        book = books(backend, c)
        assert book.active.messages == [A] and book.suspended is None

    def test_declined_ticket_keeps_both_issues(self, temp_db):
        backend, llm, c = setup(temp_db, ["new_issue"])
        c.say(A), c.say(B)
        c.confirm(c.say("create a ticket for this"), accepted=False)
        book = books(backend, c)
        assert book.active.messages == [B] and book.suspended.messages == [A]

    def test_issue_state_not_shared_between_sessions(self, temp_db):
        backend, llm, c = setup(temp_db)
        other = Client(backend, "C101")
        c.say(A)
        assert books(backend, other).active is None


class TestAmbiguousBuffer:
    def test_buffer_promoted_into_issue(self, temp_db, raw):
        backend, llm, c = setup(temp_db)
        first = c.say("it is broken")
        assert "which order" in first.text.lower() and books(backend, c).active is None
        c.say("My earbuds stopped working after two days")
        assert books(backend, c).active.messages == ["it is broken", "My earbuds stopped working after two days"]

    def test_buffer_expires(self, temp_db, monkeypatch):
        from datetime import datetime, timezone
        now = {"v": datetime(2026, 11, 7, tzinfo=timezone.utc)}
        monkeypatch.setattr(bclock, "now", lambda: now["v"])
        backend, llm, c = setup(temp_db)
        c.say("it is broken")
        now["v"] += timedelta(minutes=11)
        c.say("My earbuds stopped working after two days")
        assert books(backend, c).active.messages == ["My earbuds stopped working after two days"]

    def test_buffer_keeps_last_three(self, temp_db):
        backend, llm, c = setup(temp_db)
        for text in ("it is broken", "it stopped", "it is faulty", "it has a problem"):
            c.say(text)
        c.say("My earbuds stopped working after two days")
        msgs = books(backend, c).active.messages
        assert msgs == ["it stopped", "it is faulty", "it has a problem",
                        "My earbuds stopped working after two days"]

    def test_non_ambiguous_messages_are_not_buffered(self, temp_db):
        backend, llm, c = setup(temp_db)
        c.say("hello"), c.say("what is your refund policy?")
        assert backend.registry.get(c.state._handle).clarification == []


class TestBuildDescription:
    def test_exact_join_single_newline(self):
        assert build_description(["a", "b", "c"]) == "a\nb\nc"

    def test_single_message_unchanged(self):
        assert build_description(["  keep   spacing \t"]) == "  keep   spacing \t"

    def test_overflow_keeps_most_recent_complete_messages(self):
        old, mid, new = "o" * 900, "m" * 900, "n" * 900
        assert build_description([old, mid, new]) == mid + "\n" + new
        assert len(build_description([old, mid, new])) <= 2000

    def test_single_overlong_message_999_ellipsis_1000(self):
        text = "x" * 999 + "MIDDLE" * 100 + "y" * 1000
        out = build_description([text])
        assert len(out) == 2000 and out[:999] == "x" * 999 and out[999] == "…" and out[1000:] == "y" * 1000

    def test_exactly_2000_unchanged(self):
        assert build_description(["z" * 2000]) == "z" * 2000

    def test_empty(self):
        assert build_description([]) == ""


class TestIssueBook:
    def test_end_active_promotes(self):
        b = IssueBook()
        b.start("a")
        b.suspend_active_and_start("b")
        b.end_active()
        assert b.active.messages == ["a"] and b.suspended is None

    def test_full_flag_and_abandon(self):
        b = IssueBook()
        b.start("a")
        b.suspend_active_and_start("b")
        assert b.full
        b.abandon("suspended")
        assert not b.full and b.active.messages == ["b"]

    def test_swap_without_suspended(self):
        b = IssueBook()
        b.start("a")
        assert b.swap_to_suspended() is None and b.active.messages == ["a"]
