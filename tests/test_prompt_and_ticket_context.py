"""Phase 2: the language rule, the order-summary and unsupported-request rules, and tickets that use what the
customer already said.

Baseline v2 (3 live runs): English messages were answered in Hinglish (12 of 17 in v1, 15 of 30 language failures
in v2); with several undelivered orders only one was described; "Just cancel ORD1014" was answered with "What is
the issue?" although the customer had just said it. The prompt rules need a live model to be measured; what is
testable offline is tested here: the prompt's content, and the ticket proposal code.

Scripted model output only; no network.
"""

from __future__ import annotations

import pytest

from app.backend import Backend, Settings
from app.llm.base import LLMResult, ToolCall
from app.orchestrator import engine as engine_module
from backend_helpers import Client, ScriptedLLM

PROMPT = engine_module.SYSTEM_PROMPT


def rows(raw, table):
    return [dict(r) for r in raw.execute(f"SELECT * FROM {table}").fetchall()]


class TicketModel(ScriptedLLM):
    """A real-model stand-in that answers every message with a start_support_ticket proposal."""

    interprets_language = True

    def __init__(self, **arguments):
        super().__init__(chat=self._chat)
        self.arguments = arguments or {"issue_type": "general"}

    def _chat(self, system, messages, tools):
        return LLMResult(tool_calls=(ToolCall("start_support_ticket", dict(self.arguments), "t1"),))


class TestSystemPromptRules:
    def test_the_language_rule_is_explicit_and_not_the_old_ambiguous_line(self):
        assert "Reply in the customer's language" not in PROMPT
        lowered = PROMPT.lower()
        assert "latest message" in lowered and "english" in lowered and "do not use hindi or hinglish" in lowered
        assert "devanagari" in lowered

    def test_hinglish_is_still_supported_when_the_customer_uses_it(self):
        assert "Hinglish" in PROMPT and "only if the customer writes" in PROMPT

    def test_every_undelivered_order_is_to_be_mentioned(self):
        assert "every order that has not been delivered" in PROMPT

    def test_unsupported_requests_state_the_limit_then_use_what_the_customer_said(self):
        lowered = PROMPT.lower()
        assert "say plainly that you cannot" in lowered and "do not ask them to repeat" in lowered
        assert "cancel an order" in lowered and "replacement" in lowered

    def test_eligibility_is_not_inferred_from_a_refund_status(self):
        assert "not a statement about return eligibility" in PROMPT

    @pytest.mark.parametrize("phrase", ["never guess", "not instructions", "start_support_ticket",
                                        "start_return_request", "Never say a return", "Never reveal"])
    def test_the_original_guardrails_are_kept(self, phrase):
        assert phrase.lower() in PROMPT.lower()

    def test_the_prompt_stays_short_enough_to_follow(self):
        assert len(PROMPT) < 4500


class TestTicketUsesWhatTheCustomerSaid:
    def start(self, temp_db, customer="C101", **arguments):
        return Client(Backend(llm=TicketModel(**arguments), settings=Settings()), customer)

    def test_a_stated_problem_with_an_order_goes_straight_to_the_card(self, temp_db, raw):
        c = self.start(temp_db, issue_type="order", order_id="ORD1014")
        message = "Just cancel ORD1014, I don't want it anymore."
        reply = c.say(message)
        assert reply.status == "needs_confirmation" and reply.pending.kind == "support_ticket"
        assert message in reply.pending.summary and "ORD1014" in reply.pending.summary
        assert "What is the issue" not in reply.text and rows(raw, "support_tickets") == []

    def test_confirming_that_card_creates_one_ticket_with_the_customers_words_and_closes_the_issue(self, temp_db, raw):
        c = self.start(temp_db, issue_type="order", order_id="ORD1014")
        message = "Just cancel ORD1014, I don't want it anymore."
        card = c.say(message)
        assert "TKT_" in c.confirm(card).text
        (ticket,) = rows(raw, "support_tickets")
        assert ticket["description"] == message and ticket["order_id"] == "ORD1014" and ticket["customer_id"] == "C101"
        assert c.backend.registry.get(c.state._handle).issues.active is None

    def test_a_problem_described_without_an_order_id_also_goes_to_the_card(self, temp_db, raw):
        c = self.start(temp_db)
        message = "My earbuds are broken and nobody helps me, connect me to a person"
        reply = c.say(message)
        assert reply.status == "needs_confirmation" and message in reply.pending.summary

    @pytest.mark.parametrize("message", ["I'm done talking to a bot. Get me your manager.", "kisi insaan se baat karao",
                                         "I want to speak to a human"])
    def test_a_bare_request_for_a_person_still_asks_what_it_is_about(self, temp_db, raw, message):
        reply = self.start(temp_db).say(message)
        assert reply.status == "ok" and "What is the issue" in reply.text and reply.pending is None

    def test_after_the_bare_request_the_next_message_becomes_the_description(self, temp_db, raw):
        c = self.start(temp_db)
        c.say("get me a human")
        card = c.say("The left earbud stopped working after two days")
        assert card.status == "needs_confirmation" and "left earbud" in card.pending.summary

    def test_another_customers_order_is_not_ticketed(self, temp_db, raw):
        c = self.start(temp_db, "C102", issue_type="order", order_id="ORD1014")  # ORD1014 belongs to C101
        reply = c.say("Just cancel ORD1014, I don't want it anymore.")
        assert reply.pending is None and "couldn't find that order" in reply.text
        assert rows(raw, "support_tickets") == []

    def test_nothing_is_written_until_the_confirm_click(self, temp_db, raw):
        c = self.start(temp_db, issue_type="order", order_id="ORD1014")
        card = c.say("Just cancel ORD1014, I don't want it anymore.")
        assert c.say("yes").status == "needs_confirmation" and rows(raw, "support_tickets") == []
        assert "TKT_" in c.confirm(card).text and len(rows(raw, "support_tickets")) == 1

    def test_the_session_ticket_cap_still_applies(self, temp_db, raw):
        c = self.start(temp_db, issue_type="order", order_id="ORD1014")
        session = c.backend.registry.get(c.state._handle)
        session.committed["support_ticket"] = Settings().cap_for("support_ticket")
        reply = c.say("Just cancel ORD1014, I don't want it anymore.")
        assert reply.pending is None and "limit of support tickets" in reply.text


class TestBareComplaintsStillAsk:
    @pytest.mark.parametrize("message", ["I have a complaint.", "I want to complain.", "I want to report an issue."])
    def test_a_bare_complaint_asks_what_it_is_about(self, temp_db, message):
        reply = Client(Backend(llm=TicketModel(), settings=Settings()), "C101").say(message)
        assert reply.pending is None and "What is the issue" in reply.text

    def test_a_complaint_that_names_an_order_goes_to_the_card(self, temp_db):
        c = Client(Backend(llm=TicketModel(issue_type="order", order_id="ORD1002"), settings=Settings()), "C101")
        reply = c.say("I have a complaint about ORD1002, it never arrived")
        assert reply.status == "needs_confirmation" and "ORD1002" in reply.pending.summary


# ---------------------------------------------------------------------------
# Live v3 follow-ups: unfound orders, Hinglish detection, the unsupported-request sentence
# ---------------------------------------------------------------------------

class TestUnfoundOrderRule:
    def test_the_prompt_says_what_to_do_when_the_named_order_is_not_the_customers(self):
        assert "not on their account" in PROMPT and "never describe their own orders as anyone else's" in PROMPT
        assert "offer to list their own orders" in PROMPT

    def test_listing_instead_of_asking_still_applies_when_no_order_id_was_given(self):
        assert "If they give no order ID, call list_my_orders" in PROMPT

    @pytest.mark.parametrize("tool", ["get_order_status"])
    def test_the_tool_result_for_an_order_that_is_not_theirs_carries_the_same_instruction(self, temp_db, tool):
        from app.tools.context import CustomerContext
        from app.tools.registry import execute_tool

        result = execute_tool(CustomerContext("C102", "s"), tool, {"order_id": "ORD1001"})  # belongs to C101
        assert result.status == "not_found" and "not on this customer's account" in (result.note or "")
        assert "belongs to" not in (result.note or "").lower() and "C101" not in result.to_json()


class TestHinglishRule:
    def test_the_prompt_names_latin_letter_hindi_as_hinglish_with_examples(self):
        assert "mera order kab aayega" in PROMPT and "is Hinglish: reply in Hinglish" in PROMPT

    def test_the_english_rule_is_still_there(self):
        assert "do not use Hindi or Hinglish words" in PROMPT


class TestUnsupportedRequestSentenceBeforeTheCard:
    def start(self, temp_db, customer="C101", **arguments):
        return Client(Backend(llm=TicketModel(**arguments), settings=Settings()), customer)

    @pytest.mark.parametrize("message, limit", [
        ("The charger isn't working, and I need a replacement.", "arrange a replacement or exchange"),
        ("Please cancel my order ORD1014", "cancel an order"),
        ("Change my delivery address to somewhere else for ORD1014", "change account or delivery details"),
    ])
    def test_the_limit_is_stated_before_the_card(self, temp_db, raw, message, limit):
        reply = self.start(temp_db, issue_type="order", order_id="ORD1014").say(message)
        assert reply.status == "needs_confirmation" and reply.pending.kind == "support_ticket"
        assert reply.text.startswith(f"I'm not able to {limit} myself.") and "Please confirm to submit it" in reply.text
        assert message in reply.pending.summary and rows(raw, "support_tickets") == []

    @pytest.mark.parametrize("message", ["Can I cancel my order ORD1014?", "My earbuds are broken, connect me to a person",
                                         "I have a complaint about ORD1014, it never arrived"])
    def test_no_limit_is_claimed_for_a_question_or_an_ordinary_problem(self, temp_db, message):
        reply = self.start(temp_db, issue_type="order", order_id="ORD1014").say(message)
        assert "I'm not able to" not in reply.text

    def test_the_bare_request_for_a_person_is_unchanged(self, temp_db):
        reply = self.start(temp_db).say("I want to speak to a human")
        assert "What is the issue" in reply.text and "I'm not able to" not in reply.text

    def test_the_sentence_never_replaces_the_confirm_click(self, temp_db, raw):
        c = self.start(temp_db, issue_type="order", order_id="ORD1014")
        card = c.say("Please cancel my order ORD1014")
        assert c.say("yes").status == "needs_confirmation" and rows(raw, "support_tickets") == []
        assert "TKT_" in c.confirm(card).text and len(rows(raw, "support_tickets")) == 1

    def test_the_helper_only_fires_for_requests_not_questions(self):
        from app.orchestrator import guard

        assert guard.unsupported_request("Please cancel my order ORD1014") == "cancel an order"
        assert guard.unsupported_request("Can I cancel my order?") is None
        assert guard.unsupported_request("how do I cancel my order") is None
        assert guard.unsupported_request("Where is my order?") is None


class TestLiveV4PromptFixes:
    def test_a_named_order_is_always_looked_up_before_it_is_called_not_found(self):
        assert "always call get_order_status for it" in PROMPT
        assert "only if the tool says it is not on their account" in PROMPT
        assert "If the customer names an order ID that is not on their account" not in PROMPT  # the unconditional wording

    def test_the_other_unfound_order_rules_survive(self):
        assert "offer to list their own orders" in PROMPT and "never describe their own orders as anyone else's" in PROMPT

    def test_tool_and_policy_text_do_not_decide_the_reply_language(self):
        assert "Tool results and policy text are written in English" in PROMPT
        assert "answer in the customer's language anyway and paraphrase them" in PROMPT

    def test_the_earlier_language_and_length_rules_remain(self):
        assert "do not use Hindi or Hinglish words" in PROMPT and "is Hinglish: reply in Hinglish" in PROMPT
        assert len(PROMPT) < 4500
