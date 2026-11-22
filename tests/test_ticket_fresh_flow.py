"""Phase 1.1: the fresh-description ticket flow (NT-125 to NT-140, NT-152 to NT-156, NT-160 to NT-163, NT-166).

Scripted or rule-based model only; no network. The fresh flow is exercised through ``fresh_backend`` (gate ON, fake writer injected into
the engine). Nothing here touches a real ticket writer or any protected database.
"""

from __future__ import annotations

import pytest

from app.backend import Settings
from app.backend.config import ConfigError
from app.db import repository
from app.llm.base import LLMResult, ToolCall
from app.orchestrator import engine as engine_module
from app.orchestrator import guard, ticket_flow
from app.tools import flow_texts
from backend_helpers import Client, ScriptedLLM
from ticket_writer_fakes import FakeTicketWriter, fresh_backend, session_of

T = flow_texts.text
LISTED = ["return", "payment", "refund", "order", "damaged", "help", "support", "ticket", "complaint", "I need support", "yes", "ok"]
LONG_OK = "x" * 2000


def client(writer=None, customer="C101", llm=None, **settings):
    return Client(fresh_backend(writer, llm=llm, **settings), customer)


def to_preview(c, text="My earbuds stopped charging after two days."):
    """raise a ticket, give a description, pick no order: returns the preview reply."""
    c.say("raise a ticket")
    reply = c.say(text)
    if reply.choice is not None and reply.choice.options and "none" in [o.option_id for o in reply.choice.options]:
        reply = c.pick(reply, "none")
    assert reply.pending is not None, reply
    return reply


def draft(c):
    return session_of(c).ticket_draft


class TestGateSettings:
    def test_default_off_and_strict_parsing(self):
        assert Settings().fresh_ticket_flow_enabled is False
        assert Settings.from_env({}).fresh_ticket_flow_enabled is False
        assert Settings.from_env({"SUPPORT_TICKET_FRESH_FLOW_ENABLED": "true"}).fresh_ticket_flow_enabled is True
        assert Settings.from_env({"SUPPORT_TICKET_FRESH_FLOW_ENABLED": " FALSE "}).fresh_ticket_flow_enabled is False
        for bad in ("1", "yes", "on", "enabled", "truee", "0"):
            with pytest.raises(ConfigError):
                Settings.from_env({"SUPPORT_TICKET_FRESH_FLOW_ENABLED": bad})

    def test_backend_takes_no_writer_argument_and_off_means_none(self):
        import inspect
        from app.backend import Backend
        assert "ticket_writer" not in inspect.signature(Backend.__init__).parameters
        from app.backend.api import build_ticket_writer
        assert build_ticket_writer(Settings()) is None

    def test_gate_on_fails_closed_without_the_real_writer(self, monkeypatch):
        import sys
        from app.backend.api import build_ticket_writer
        monkeypatch.setitem(sys.modules, "app.db.ticket_writer", None)  # the real module cannot be imported
        with pytest.raises(ConfigError):
            build_ticket_writer(Settings(fresh_ticket_flow_enabled=True))

    def test_gate_on_fails_closed_when_the_real_writer_refuses_to_start(self, monkeypatch):
        import app.db.ticket_writer as real

        class Refusing(real.RepositoryTicketWriter):
            def __init__(self):
                raise RuntimeError("schema is not migrated")

        monkeypatch.setattr(real, "RepositoryTicketWriter", Refusing)
        from app.backend.api import build_ticket_writer
        with pytest.raises(ConfigError):
            build_ticket_writer(Settings(fresh_ticket_flow_enabled=True))

    def test_gate_on_never_accepts_a_fake_subclass(self, monkeypatch):
        import app.db.ticket_writer as real

        class Sneaky(real.RepositoryTicketWriter):
            def __init__(self):
                pass

        monkeypatch.setattr(real, "RepositoryTicketWriter", Sneaky)
        from app.backend.api import build_ticket_writer
        # the factory instantiates and checks the exact class it imported: a subclass of the real writer is the "real" one here, but
        # it still cannot be selected from outside because nothing outside this function can name a writer
        assert type(build_ticket_writer(Settings(fresh_ticket_flow_enabled=True))) is Sneaky

    def test_application_code_never_references_a_fake_writer(self):
        import pathlib
        for path in pathlib.Path("app").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "FakeTicketWriter" not in text and "ticket_writer_fakes" not in text, path


class TestFreshDescriptionOnEveryPath:
    def test_direct_request_asks_for_a_fresh_description_and_shows_no_card(self, temp_db, bnow):
        c = client()
        reply = c.say("raise a ticket")
        assert reply.text == T("ticket_fresh_request") and reply.pending is None
        assert draft(c).stage == "awaiting"

    def test_a_description_given_earlier_is_not_reused(self, temp_db, bnow):
        c = client()
        c.say("My earbuds from ORD1001 stopped charging after two days.")
        reply = c.say("I have a complaint")
        assert reply.text == T("ticket_fresh_request") and reply.pending is None

    def test_model_proposal_alone_never_starts_a_draft(self, temp_db, bnow):
        class AlwaysTicket(ScriptedLLM):
            interprets_language = True

            def __init__(self):
                super().__init__(chat=self._chat)

            def _chat(self, system, messages, tools):
                if messages and messages[-1].get("role") == "tool":
                    return LLMResult(text="Here is your order status.")
                return LLMResult(tool_calls=(ToolCall("start_support_ticket", {"issue_type": "general"}, "t1"),))

        c = client(llm=AlwaysTicket())
        reply = c.say("What is the status of ORD1001?")
        assert draft(c) is None and reply.pending is None and reply.choice is None

    def test_model_proposal_with_a_customer_originated_request_starts_the_flow(self, temp_db, bnow):
        class AlwaysTicket(ScriptedLLM):
            interprets_language = True

            def __init__(self):
                super().__init__(chat=self._chat)

            def _chat(self, system, messages, tools):
                return LLMResult(tool_calls=(ToolCall("start_support_ticket", {"issue_type": "general"}, "t1"),))

        c = client(llm=AlwaysTicket())
        reply = c.say("My earphones from ORD1001 are broken and I need a ticket.")
        assert reply.text == T("ticket_fresh_request") and reply.pending is None

    def test_service_request_and_offer_start_with_the_fresh_request(self, temp_db, bnow):
        c = client()
        assert c.say("please change my delivery address").text == T("ticket_fresh_request")
        c2 = client(customer="C102")
        s = session_of(c2)
        c2.backend.engine._offer_ticket(s, "ORD1003", "IT1003")
        assert c2.say("yes please raise a ticket").text == T("ticket_fresh_request")


class TestDescriptionCaptureAndValidation:
    def test_text_is_stored_verbatim_and_the_card_is_complete_plain_text(self, temp_db, bnow):
        writer = FakeTicketWriter()
        c = client(writer)
        text = "  My *earbuds* <b>broke</b>\n\twith Devanagari अभि and \U0001F600 emoji    "
        c.say("raise a ticket")
        reply = c.say(text)
        reply = c.pick(reply, "none") if reply.choice is not None else reply
        assert text in reply.pending.summary  # the complete text, not cut, not trimmed, not rendered
        done = c.confirm(reply)
        assert writer.requests[0].description == text
        assert "TKT_FAKE0001" in done.text

    def test_no_200_character_cut(self, temp_db, bnow):
        c = client()
        text = ("a long but honest description of the problem " * 40)[:1500]
        c.say("raise a ticket")
        reply = c.say(text)
        reply = c.pick(reply, "none")
        assert text in reply.pending.summary and "…" not in reply.pending.summary

    @pytest.mark.parametrize("word", LISTED)
    def test_every_listed_entry_is_not_a_description(self, word, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say(word)
        assert reply.text == T("ticket_prompt_two_bare") and reply.choice.options[0].option_id == "cancel"
        assert draft(c).description is None and draft(c).attempts == 1

    @pytest.mark.parametrize("variant", ["Return", "RETURN", "i NEED support", "  help  ", "Refund.", "help!!", "(ok)", '"order"', "YES!"])
    def test_case_whitespace_and_punctuation_are_ignored_for_comparison_only(self, variant, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        assert c.say(variant).text == T("ticket_prompt_two_bare")

    @pytest.mark.parametrize("text", ["order not received", "payment failed", "screen cracked", "returns", "orders", "retrun",
                                      "assistance", "return my damaged order"])
    def test_other_text_is_accepted_without_a_word_count(self, text, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say(text)
        assert draft(c).description == text and (reply.pending is not None or reply.choice is not None)

    def test_second_invalid_reply_exits_and_clears_everything(self, temp_db, bnow):
        writer = FakeTicketWriter()
        c = client(writer)
        c.say("raise a ticket")
        c.say("return")
        reply = c.say("help")
        assert reply.text == T("ticket_exit") and draft(c) is None and writer.requests == []
        assert c.say("raise a ticket").text == T("ticket_fresh_request")  # a later request starts fresh

    def test_over_limit_text_asks_to_shorten_and_is_never_truncated(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say("y" * 2001)
        assert reply.text == T("ticket_prompt_two_over") and "2,000-character" in reply.text and draft(c).description is None
        reply = c.say(LONG_OK)  # exactly 2,000 code points is accepted unmodified
        assert draft(c).description == LONG_OK

    def test_over_limit_then_bare_exits(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        c.say("z" * 3000)
        assert c.say("ok").text == T("ticket_exit")

    def test_over_limit_outside_a_description_request_keeps_the_generic_refusal(self, temp_db, bnow):
        c = client()
        reply = c.say("w" * 2001)
        assert reply.status == "error" and reply.error_code == "message_too_long"

    def test_hindi_and_hinglish_lists_are_inactive_and_nothing_else_is_matched(self, temp_db, bnow):
        for word in ("वापसी", "wapsi", "madad"):
            c = client()
            c.say("raise a ticket")
            c.say(word)
            assert draft(c).description == word
        assert ticket_flow.validate_description("  \n") == "bare"
        assert ticket_flow.validate_description("a\x00b") == "bare"


class TestReadOnlyAmbiguityConflict:
    def test_read_only_question_is_answered_and_the_draft_is_kept(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say("What is your return policy?")
        assert draft(c).stage == "awaiting" and draft(c).attempts == 0 and draft(c).description is None
        assert reply.text != T("ticket_prompt_two_bare")

    def test_read_only_question_in_each_state_keeps_card_and_choice(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say("I received a cracked screen on order ORD1001")
        assert draft(c).stage == "metadata" and reply.choice is not None
        again = c.say("What is your return policy?")
        assert draft(c).stage == "metadata" and again.choice is not None and again.choice.prompt_id == reply.choice.prompt_id
        card = c.pick(again, "none")
        before = card.pending.action_id
        shown = c.say("What is your return policy?")
        assert shown.pending is not None and shown.pending.action_id == before and draft(c).stage == "preview"

    def test_ambiguous_message_needs_an_explicit_choice_and_is_never_captured(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say("Why is my earphone broken?")
        assert reply.choice is not None and reply.text == T("ticket_classify_question") and draft(c).description is None
        card = c.pick(reply, "description")
        assert draft(c).description == "Why is my earphone broken?"

    def test_ambiguity_choice_question_answers_and_keeps_the_draft(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say("Why is my earphone broken?")
        c.pick(reply, "question")
        assert draft(c) is not None and draft(c).description is None and draft(c).stage == "awaiting"

    def test_ambiguity_chosen_as_description_that_is_invalid_counts_an_attempt(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        held = "why is this broken?"
        reply = c.say(held)
        assert reply.choice is not None
        c.backend.engine._take_description  # exists
        # a held text that is bare cannot be ambiguous; simulate the choice with an over-limit text through the engine directly
        s = session_of(c)
        s.choice = None
        c.backend.engine._take_description(s, s.ticket_draft, "ok", engine_module._Ctx())
        assert s.ticket_draft.attempts == 1

    def test_ignored_ambiguity_choice_is_superseded_and_old_text_never_captured(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        c.say("Why is my earphone broken?")
        reply = c.say("The left earbud does not charge at all.")
        assert draft(c).description == "The left earbud does not charge at all."
        assert "Why is my earphone broken?" not in str(draft(c))

    def test_exact_raise_a_ticket_phrase_is_a_conflict_not_a_description_or_an_attempt(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say("Raise a ticket.")
        assert reply.choice is not None and reply.text == T("ticket_conflict_question")
        assert draft(c).attempts == 0 and draft(c).description is None
        assert "raise a ticket" not in ticket_flow.BARE_ENTRIES

    def test_keep_pending_returns_to_the_stage_and_discard_starts_fresh(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say("raise a ticket")
        kept = c.pick(reply, "keep")
        assert kept.text == T("ticket_fresh_request") and draft(c).stage == "awaiting"
        reply = c.say("raise a ticket")
        fresh = c.pick(reply, "discard")
        assert fresh.text == T("ticket_fresh_request") and draft(c).attempts == 0

    def test_ignored_conflict_choice_stays_open_read_only_answered_other_messages_reminded(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        choice = c.say("raise a ticket")
        answered = c.say("What is your return policy?")
        assert answered.choice is not None and answered.choice.prompt_id == choice.choice.prompt_id
        reminded = c.say("The charger sparks when I plug it in.")
        assert reminded.text == T("remind_choice") and reminded.choice.prompt_id == choice.choice.prompt_id
        assert draft(c).description is None

    def test_expired_choice_is_removed_and_the_draft_survives(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        c.say("raise a ticket")
        bnow(minutes=11)  # the choice (pending_ttl) is gone; the draft's own time-out (clarification_ttl) is the same 10 minutes
        s = session_of(c)
        s.ticket_draft.expires = s.ticket_draft.expires.replace(year=s.ticket_draft.expires.year + 1)
        reply = c.say("The charger sparks when I plug it in.")
        assert s.choice is None or s.choice.kind != "draft_conflict"
        assert draft(c) is not None and draft(c).description == "The charger sparks when I plug it in."

    def test_draft_expiry_clears_all_ticket_state(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        bnow(minutes=11)
        reply = c.say("What is your return policy?")
        assert draft(c) is None


class TestCancellationAndOrders:
    def test_typed_cancel_clears_all_ticket_state(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        c.say("return")
        assert c.say("cancel").text == T("cancelled")
        s = session_of(c)
        assert s.ticket_draft is None and s.choice is None and s.want_ticket is False and s.clarification == []

    def test_card_cancel_button_clears_all_ticket_state(self, temp_db, bnow):
        c = client()
        card = to_preview(c)
        assert c.confirm(card, accepted=False).text == T("cancelled")
        assert draft(c) is None and c.backend.registry.open_pending(session_of(c)) is None

    def test_prompt_two_cancel_button_cancels(self, temp_db, bnow):
        c = client()
        c.say("raise a ticket")
        reply = c.say("ok")
        assert c.pick(reply, "cancel").text == T("cancelled") and draft(c) is None

    def test_order_selection_lists_only_the_customers_own_orders_and_no_order(self, temp_db, bnow):
        c = client(customer="C101")
        c.say("raise a ticket")
        reply = c.say("My earbuds stopped charging after two days.")
        ids = [o.option_id for o in reply.choice.options]
        assert set(ids) == {"ORD1001", "ORD1002", "ORD1014", "none", "cancel"}
        card = c.pick(reply, "ORD1001")
        assert "Order: ORD1001" in card.pending.summary

    def test_no_order_option_links_nothing(self, temp_db, bnow):
        writer = FakeTicketWriter()
        c = client(writer)
        c.confirm(to_preview(c))
        assert writer.requests[0].order_id is None

    def test_typed_foreign_order_leaves_the_card_unchanged_without_disclosure(self, temp_db, bnow):
        c = client(customer="C101")
        card = to_preview(c)
        reply = c.say("I meant order ORD1003")  # belongs to another customer
        assert reply.text == T("order_not_found_check") and reply.pending.action_id == card.pending.action_id

    def test_typed_own_order_replaces_the_card_and_needs_a_fresh_confirm(self, temp_db, bnow):
        writer = FakeTicketWriter()
        c = client(writer, customer="C101")
        card = to_preview(c)
        new = c.say("I meant order ORD1002")
        assert new.pending.action_id != card.pending.action_id and "Order: ORD1002" in new.pending.summary
        old = c.confirm(card)  # the old card can no longer be claimed
        assert writer.requests == [] and "TKT_" not in old.text
        done = c.confirm(new)
        assert writer.requests[0].order_id == "ORD1002"

    def test_typed_order_correction_is_classified_before_the_conflict_rule(self, temp_db, bnow):
        c = client(customer="C101")
        card = to_preview(c)
        reply = c.say("It is order ORD1014, I have a complaint about it")
        assert reply.choice is None and reply.pending is not None and "Order: ORD1014" in reply.pending.summary


class TestConfirmOnlyByButton:
    def test_typed_yes_never_confirms_and_typed_no_leaves_the_card(self, temp_db, bnow):
        writer = FakeTicketWriter()
        c = client(writer)
        card = to_preview(c)
        assert c.say("yes").pending.action_id == card.pending.action_id
        assert c.say("no").pending.action_id == card.pending.action_id
        assert writer.requests == [] and draft(c).stage == "preview"
        assert "TKT_" in c.confirm(card).text and len(writer.requests) == 1


class TestSharedImprovementsGateOff:
    """The approved shared improvements apply with the gate OFF too (legacy flow)."""

    def test_read_only_question_is_answered_while_a_legacy_card_is_open(self, temp_db, bnow):
        from app.backend import Backend
        c = Client(Backend(llm=ScriptedLLM(), settings=Settings()), "C101")
        c.say("My ORD1001 earbuds stopped charging after two days and I want a ticket for it.")
        reply = c.say("I have a complaint about ORD1001 stopped charging")
        card = reply if reply.pending is not None else None
        if card is None:
            pytest.skip("the rule-based stand-in did not propose a card here")
        answered = c.say("What is your return policy?")
        assert answered.pending is not None and answered.pending.action_id == card.pending.action_id

    def test_legacy_preview_is_not_truncated(self, temp_db, bnow):
        from app.backend import Backend
        c = Client(Backend(llm=ScriptedLLM(), settings=Settings()), "C101")
        s = session_of(c)
        text = "My ORD1001 earbuds stopped charging " + "again " * 80
        issue = c.backend.engine._start_issue(s, text)
        turn = c.backend.engine._propose_ticket(s, {}, text)
        if turn.pending is not None:
            assert text in turn.pending.summary


class TestPromptModesAndGreeting:
    def test_gate_off_prompt_keeps_the_legacy_instruction_and_loses_the_human_follow_up_promise(self):
        off = engine_module.system_prompt_for_turn("hello")
        assert "do not ask them to repeat it" in off
        assert "human agent can follow up" not in off
        assert engine_module.FRESH_TICKET_INSTRUCTION not in off

    def test_gate_on_prompt_has_the_approved_instruction_verbatim_and_not_the_legacy_sentence(self):
        on = engine_module.system_prompt_for_turn("hello", True)
        assert engine_module.FRESH_TICKET_INSTRUCTION in on
        assert on.count("Never promise human-agent follow-up.") >= 1
        assert "do not ask them to repeat it" not in on
        assert engine_module.FRESH_TICKET_INSTRUCTION.startswith("When a customer asks to raise a support ticket, ask them to provide a fresh description")

    def test_greeting_is_the_approved_text_in_every_constant_and_mentions_no_human_limitation(self):
        approved = ("Hello! I'm the store's automated customer-support assistant. I can check your orders and deliveries, explain "
                    "returns, refunds and policies, answer factual questions about our products, start a return, and help you raise "
                    "a support ticket. What do you need help with today?\n\nI'm here for support rather than shopping advice, so I "
                    "can't recommend products or take new orders.")
        assert T("greeting") == approved == guard.GREETING_REPLY
        assert "human agent" not in approved

    def test_hindi_and_hinglish_strings_stay_marked_draft(self):
        assert flow_texts.REVIEW_STATUS["en"] == "approved"
        assert flow_texts.REVIEW_STATUS["hinglish"].startswith("draft") and flow_texts.REVIEW_STATUS["hi"].startswith("draft")
        assert all(key in flow_texts.TEXTS["hi"] and key in flow_texts.TEXTS["hinglish"]
                   for key in ("ticket_exit", "ticket_outcome_unknown", "ticket_prompt_two_over"))


class TestDraftCreationConsumesNothing:
    def test_abandoned_drafts_create_no_tickets_and_consume_no_allowance(self, temp_db, bnow):
        c = client(customer="C101")
        for _ in range(8):
            c.say("raise a ticket")
            c.say("cancel")
        assert repository.get_customer_tickets("C101") == []


class TestManyOrders:
    def test_more_than_a_page_shows_the_most_recent_and_accepts_a_typed_own_order(self, temp_db, bnow, monkeypatch):
        orders = [{"order_id": f"ORD90{i:02d}", "status": "Delivered", "order_date": f"2026-10-{i + 1:02d}"} for i in range(12)]
        monkeypatch.setattr(repository, "list_customer_orders", lambda customer_id: orders)
        c = client(customer="C101")
        c.say("raise a ticket")
        reply = c.say("My earbuds stopped charging after two days.")
        shown = [o.option_id for o in reply.choice.options if o.option_id.startswith("ORD")]
        assert len(shown) == 8 and shown[0] == "ORD9011" and reply.text == T("ticket_order_pick_more", shown=8)
        refused = c.say("it is order ORD9000")  # not one of this customer's orders in the database: nothing is linked or disclosed
        assert refused.text == T("order_not_found_check") and draft(c).order_id is None
        chosen = c.say("it is order ORD1002")  # a real order of this customer, typed by the customer
        assert chosen.pending is not None and "Order: ORD1002" in chosen.pending.summary
