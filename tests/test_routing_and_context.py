"""Conversation routing and multi-turn context for the offline RuleBasedLLM stand-in.

Regression tests for four gaps found in manual testing (all with ``RuleBasedLLM``, a temporary database and
no network):

A. "I want to return it" after an order was discussed reuses that order instead of asking for the ID again.
B. Order-count questions use the existing ``list_my_orders`` tool and state the count, while policy questions
   that merely contain "how many" still go to FAQ/policy retrieval.
C. Concern and complaint phrasing starts the existing support-ticket workflow, which still needs the explicit
   confirmation button.
D. When FAQ retrieval is unavailable the failure is logged with its exception *types* only, and orders,
   returns and tickets keep working without an OpenAI key.

The ownership, item, window and confirmation rules are not re-tested here: they are enforced by the engine and
the repository, and these tests check that the new routing cannot bypass them.
"""

from __future__ import annotations

import logging

import pytest

from app import rag
from app.backend import Backend, Settings
from app.db import repository
from app.llm.rule_based import RuleBasedLLM
from app.orchestrator import guard
from app.rag.retriever import KnowledgeBaseError
from app.tools import CustomerContext, execute_tool
from backend_helpers import Client


class Spy(RuleBasedLLM):
    """The stand-in, recording every tool call it proposes."""

    def __init__(self) -> None:
        self.proposed: list[tuple[str, dict]] = []

    def chat(self, system, messages, tools, *, max_output_tokens):
        result = super().chat(system, messages, tools, max_output_tokens=max_output_tokens)
        for call in result.tool_calls or ():
            self.proposed.append((call.name, dict(call.arguments)))
        return result


@pytest.fixture
def spy():
    return Spy()


@pytest.fixture
def make_client(temp_db, spy):
    def build(customer_id: str) -> Client:
        return Client(Backend(llm=spy, settings=Settings()), customer_id)
    return build


def table(raw, name):
    return [dict(r) for r in raw.execute(f"SELECT * FROM {name}").fetchall()]


def names(spy):
    return [name for name, _ in spy.proposed]


# ---------------------------------------------------------------------------
# A. Return context from the conversation
# ---------------------------------------------------------------------------

class TestReturnContext:
    def test_the_order_from_the_conversation_is_reused(self, make_client, spy, raw):
        c = make_client("C101")
        assert "order ID" in c.say("Where is my order?").text
        assert "Order ORD1001: Delivered" in c.say("ORD1001").text
        spy.proposed.clear()

        reply = c.say("I want to return it.")

        assert names(spy)[0] == "get_order_status" and spy.proposed[0][1] == {"order_id": "ORD1001"}
        assert "Which order" not in reply.text
        # The existing return workflow takes over: the bare request still needs the customer's own reason.
        assert reply.status == "ok" and "why you want to return" in reply.text
        assert table(raw, "return_requests") == []

        choice = c.say("The colour is too bright for me")
        assert choice.status == "needs_choice"
        pending = c.pick(choice, "standard")
        assert pending.status == "needs_confirmation" and pending.pending.kind == "return_request"
        assert table(raw, "return_requests") == []  # still nothing saved before the button
        done = c.confirm(pending)
        assert "RET_" in done.text
        saved = table(raw, "return_requests")
        assert len(saved) == 1
        assert (saved[0]["customer_id"], saved[0]["order_id"], saved[0]["item_id"]) == ("C101", "ORD1001", "IT101")
        assert saved[0]["reason"] == "The colour is too bright for me"

    def test_no_previous_order_means_the_customer_is_asked(self, make_client, spy, raw):
        c = make_client("C101")
        reply = c.say("I want to return it")
        assert reply.text == "Which order would you like to return? Please share the order ID."
        assert spy.proposed == [] and table(raw, "return_requests") == []

    def test_the_most_recent_order_is_used(self, make_client, spy):
        c = make_client("C101")
        c.say("ORD1001")
        c.say("ORD1002")
        spy.proposed.clear()
        c.say("I want to return it")
        assert spy.proposed[0] == ("get_order_status", {"order_id": "ORD1002"})

    def test_an_explicit_order_id_wins_over_the_conversation(self, make_client, spy):
        c = make_client("C101")
        c.say("ORD1002")
        spy.proposed.clear()
        reply = c.say("I want to return item IT101 from order ORD1001 because I changed my mind about it")
        assert spy.proposed == [("start_return_request", {"order_id": "ORD1001", "item_id": "IT101"})]
        assert reply.status == "needs_choice"

    def test_several_orders_in_the_latest_mention_are_not_guessed(self, make_client, spy, raw):
        c = make_client("C101")
        c.say("compare ORD1001 and ORD1002 for me")  # one customer message naming two orders
        spy.proposed.clear()
        reply = c.say("I want to return it")
        assert reply.text == "Which order would you like to return? Please share the order ID."
        assert spy.proposed == [] and table(raw, "return_requests") == []

    def test_a_list_the_assistant_showed_is_not_a_choice_the_customer_made(self, make_client, spy, raw):
        c = make_client("C101")
        listing = c.say("my orders")  # the assistant's own reply names several orders
        assert all(order in listing.text for order in ("ORD1001", "ORD1002", "ORD1014"))
        spy.proposed.clear()
        reply = c.say("I want to return it")
        assert reply.text == "Which order would you like to return? Please share the order ID."
        assert spy.proposed == [] and table(raw, "return_requests") == []

    def test_the_assistants_example_order_id_is_never_used(self, make_client, spy, raw):
        c = make_client("C101")
        asked = c.say("Where is my order?")
        assert "for example ORD1001" in asked.text  # the assistant's own question contains an example ID
        spy.proposed.clear()
        reply = c.say("I want to return it")
        assert reply.text == "Which order would you like to return? Please share the order ID."
        assert spy.proposed == [] and table(raw, "return_requests") == []

    def test_a_multi_item_order_asks_which_item(self, make_client, raw):
        c = make_client("C103")
        c.say("ORD1005")
        reply = c.say("I want to return it")
        assert "Which item would you like to return" in reply.text
        assert "IT105" in reply.text and "IT106" in reply.text
        assert table(raw, "return_requests") == []

    def test_another_customers_order_is_still_rejected(self, make_client, spy, raw):
        c = make_client("C101")
        assert "couldn't find" in c.say("status of ORD1003").text  # ORD1003 belongs to C102
        spy.proposed.clear()
        reply = c.say("I want to return it")
        assert spy.proposed[0] == ("get_order_status", {"order_id": "ORD1003"})  # the lookup is attempted...
        assert reply.text == "I couldn't find that on your account."  # ...and the ownership check refuses it
        assert reply.choice is None and reply.pending is None
        assert table(raw, "return_requests") == []

    def test_the_conversation_is_not_searched_for_other_things(self, make_client, spy):
        c = make_client("C101")
        c.say("ORD1001")
        spy.proposed.clear()
        # No return wording: the earlier order must not be attached to an unrelated question.
        c.say("Do you sell refrigerators in Antarctica?")
        assert names(spy) == ["search_policies"]


# ---------------------------------------------------------------------------
# B. Order-count intent
# ---------------------------------------------------------------------------

COUNT_PHRASES = ["How many orders do I have?", "What is the number of orders I have?", "Count my orders.",
                 "Do I have any orders?"]


class TestOrderCount:
    @pytest.mark.parametrize("message", COUNT_PHRASES)
    def test_the_count_comes_from_the_existing_tool(self, make_client, spy, message):
        reply = make_client("C101").say(message)
        assert names(spy)[0] == "list_my_orders" and "search_policies" not in names(spy)
        assert reply.status == "ok" and reply.citations == ()
        assert "You have 3 orders." in reply.text
        assert "ORD1001" in reply.text  # the list may follow the count

    def test_only_the_customers_own_orders_are_counted(self, make_client, raw):
        for customer_id in ("C102", "C103", "C106"):
            expected = raw.execute("SELECT COUNT(*) FROM orders WHERE customer_id = ?", (customer_id,)).fetchone()[0]
            reply = make_client(customer_id).say("How many orders do I have?")
            word = "order" if expected == 1 else "orders"
            assert f"You have {expected} {word}." in reply.text, customer_id

    def test_an_empty_order_list(self, make_client, monkeypatch):
        monkeypatch.setattr(repository, "list_customer_orders", lambda customer_id: [])
        assert "You have 0 orders." in make_client("C101").say("How many orders do I have?").text
        assert make_client("C101").say("show me my orders").text == "You have no orders yet."

    def test_the_count_comes_from_the_tool_total_not_from_the_visible_rows(self, make_client, monkeypatch):
        many = [{"order_id": f"ORD{2000 + i}", "status": "Delivered", "order_date": "2026-11-01T00:00:00Z",
                 "total_amount": 100} for i in range(12)]
        monkeypatch.setattr(repository, "list_customer_orders", lambda customer_id: many)
        reply = make_client("C101").say("Count my orders.")
        assert "You have 12 orders." in reply.text  # the tool shows 10 rows but reports the real total

    def test_show_me_my_orders_still_lists_them(self, make_client, spy):
        reply = make_client("C101").say("show me my orders")
        assert names(spy)[0] == "list_my_orders"
        assert reply.text.startswith("Your orders:") and "You have 3 orders" not in reply.text

    @pytest.mark.parametrize("question", ["How many days do I have to return an item?",
                                          "How many days does delivery take?",
                                          "How many orders can I return at once?"])
    def test_policy_questions_still_use_retrieval(self, make_client, spy, question):
        make_client("C101").say(question)
        assert names(spy)[0] == "search_policies" and "list_my_orders" not in names(spy)

    def test_the_return_window_question_is_still_answered_from_policy(self, make_client):
        reply = make_client("C101").say("How many days do I have to return an item?")
        assert "7 days" in reply.text and reply.citations  # the existing retrieval answer, unchanged


# ---------------------------------------------------------------------------
# C. Concern and complaint tickets
# ---------------------------------------------------------------------------

TICKET_PHRASES = ["I want to raise my concern.", "I have a complaint.", "I want to complain.",
                  "I want to report an issue."]
ASK_FOR_THE_ISSUE = "Happy to raise a ticket. What is the issue you'd like it to be about?"


class TestConcernAndComplaintTickets:
    @pytest.mark.parametrize("message", TICKET_PHRASES)
    def test_the_ticket_workflow_starts(self, make_client, spy, raw, message):
        reply = make_client("C101").say(message)
        assert reply.status == "ok" and reply.text.startswith(ASK_FOR_THE_ISSUE)
        assert names(spy) == ["start_support_ticket"]
        assert reply.pending is None and table(raw, "support_tickets") == []

    def test_details_then_pending_then_explicit_confirmation(self, make_client, raw):
        c = make_client("C101")
        c.say("I want to raise my concern.")
        pending = c.say("My earbuds from ORD1001 stopped charging after two days and I need help.")
        assert pending.status == "needs_confirmation" and pending.pending.kind == "support_ticket"
        assert "stopped charging" in pending.pending.summary
        assert table(raw, "support_tickets") == []

        # A typed "yes" is not a confirmation: the same pending action is shown again.
        typed = c.say("yes")
        assert typed.status == "needs_confirmation" and typed.pending.action_id == pending.pending.action_id
        assert table(raw, "support_tickets") == []

        done = c.confirm(pending)  # the explicit UI confirmation
        assert "TKT_" in done.text
        tickets = table(raw, "support_tickets")
        assert len(tickets) == 1 and tickets[0]["customer_id"] == "C101"
        assert "stopped charging" in tickets[0]["description"]

    def test_declining_creates_no_ticket(self, make_client, raw):
        c = make_client("C101")
        c.say("I have a complaint.")
        pending = c.say("The courier left my parcel outside and it was damaged, please look into it.")
        assert pending.status == "needs_confirmation"
        c.confirm(pending, accepted=False)
        assert table(raw, "support_tickets") == []

    def test_typed_cancel_creates_no_ticket(self, make_client, raw):
        c = make_client("C101")
        c.say("I want to complain.")
        pending = c.say("The courier left my parcel outside and it was damaged, please look into it.")
        assert pending.status == "needs_confirmation"
        c.say("cancel")
        assert table(raw, "support_tickets") == []
        assert c.confirm(pending).status == "ok" and table(raw, "support_tickets") == []  # nothing left to confirm

    def test_a_complaint_with_details_goes_straight_to_the_proposal(self, make_client, raw):
        reply = make_client("C101").say("I want to complain, my earbuds from ORD1001 stopped working yesterday")
        assert reply.status == "needs_confirmation" and reply.pending.kind == "support_ticket"
        assert table(raw, "support_tickets") == []

    @pytest.mark.parametrize("message", ["Where is my order ORD1001?", "How many orders do I have?",
                                         "What is your complaint policy?", "How do I report an issue with a product?",
                                         "Do you have a complaints process?", "I am concerned about delivery times"])
    def test_ordinary_messages_do_not_start_a_ticket(self, make_client, spy, raw, message):
        reply = make_client("C101").say(message)
        assert "start_support_ticket" not in names(spy)
        assert not reply.text.startswith(ASK_FOR_THE_ISSUE) and reply.pending is None
        assert table(raw, "support_tickets") == []

    def test_a_vague_problem_report_is_still_clarified_first(self, make_client, spy):
        reply = make_client("C101").say("something is broken")
        assert "which order or product" in reply.text and spy.proposed == []  # the guard's existing behaviour

    def test_the_guard_and_the_stand_in_share_one_phrase_list(self):
        from app.llm import rule_based

        assert set(rule_based.TICKET_REQUEST_PHRASES) == set(guard.TICKET_REQUEST_PHRASES)
        assert all(phrase == phrase.lower() for phrase in guard.TICKET_REQUEST_PHRASES)


# ---------------------------------------------------------------------------
# D. Safe diagnostics when FAQ retrieval is unavailable
# ---------------------------------------------------------------------------

class TestRetrievalDiagnostics:
    ctx = CustomerContext("C101", "ref")

    @staticmethod
    def logged(caplog) -> str:
        return "\n".join(f"{r.name}|{r.levelname}|{r.getMessage()}" for r in caplog.records)

    @staticmethod
    def break_retrieval(monkeypatch, error):
        def broken():
            raise error
        monkeypatch.setattr(rag, "default_knowledge_base", broken)

    def test_a_missing_key_is_unavailable_and_logged_as_a_configuration_failure(self, production_embeddings, caplog):
        with caplog.at_level(logging.WARNING, logger="app.tools"):
            result = execute_tool(self.ctx, "search_policies", {"query": "how long do refunds take"})
        assert (result.status, result.code) == ("unavailable", "knowledge_unavailable") and result.data == {}
        log = self.logged(caplog)
        assert "FAQ and policy retrieval unavailable" in log
        assert "error=VectorIndexError" in log and "cause=none" in log  # no underlying failure: configuration
        assert "OPENAI_API_KEY is required" not in log  # the exception message is never logged

    def test_an_underlying_failure_is_distinguished_by_its_cause_type(self, monkeypatch, caplog):
        try:
            raise OSError("disk path C:/secret/store is unreadable")
        except OSError as cause:
            error = KnowledgeBaseError("could not open the vector store")
            error.__cause__ = cause
        self.break_retrieval(monkeypatch, error)
        with caplog.at_level(logging.WARNING, logger="app.tools"):
            execute_tool(self.ctx, "search_policies", {"query": "q"})
        log = self.logged(caplog)
        assert "error=KnowledgeBaseError" in log and "cause=OSError" in log
        assert "C:/secret" not in log and "could not open" not in log

    @pytest.mark.parametrize("tool, arguments", [
        ("search_policies", {"query": "my order ORD1001 refund for customer C101"}),
        ("get_refund_policy", {}),
    ])
    def test_nothing_sensitive_reaches_the_log(self, temp_db, monkeypatch, caplog, tool, arguments):
        secret = "sk-SECRET-KEY-123 customer C101 order ORD1001 policy text: refunds take 5 business days"
        self.break_retrieval(monkeypatch, KnowledgeBaseError(secret))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRET-KEY-123")
        with caplog.at_level(logging.DEBUG):
            result = execute_tool(self.ctx, tool, arguments)
        assert (result.status, result.code) == ("unavailable", "knowledge_unavailable")
        log = self.logged(caplog)
        assert "error=KnowledgeBaseError" in log
        for forbidden in ("sk-SECRET", "C101", "ORD1001", "business days", "policy text", "refund for customer"):
            assert forbidden not in log, forbidden

    def test_the_refund_tool_logs_the_same_way_when_an_order_is_given(self, temp_db, production_embeddings, caplog):
        with caplog.at_level(logging.WARNING, logger="app.tools"):
            result = execute_tool(self.ctx, "get_refund_policy", {"order_id": "ORD1001"})
        assert result.status == "ok" and result.data["policy_unavailable"] is True
        log = self.logged(caplog)
        assert "error=VectorIndexError" in log and "ORD1001" not in log

    def test_orders_returns_and_tickets_work_without_a_key(self, make_client, production_embeddings, raw):
        c = make_client("C101")
        assert "You have 3 orders." in c.say("How many orders do I have?").text
        assert "Order ORD1001: Delivered" in c.say("ORD1001").text
        assert c.say("I want to return it").status == "ok"  # the return workflow starts (asks for the reason)
        pending = c.say("The colour is too bright for me")
        assert pending.status == "needs_choice"
        c.say("cancel")
        c.say("I want to raise my concern.")
        ticket = c.say("My earbuds from ORD1001 stopped charging after two days and I need help.")
        assert ticket.status == "needs_confirmation"
        assert "TKT_" in c.confirm(ticket).text
        assert len(table(raw, "support_tickets")) == 1

    def test_the_user_facing_message_is_unchanged(self, make_client, production_embeddings):
        reply = make_client("C101").say("How long do refunds take?")
        assert reply.text == ("I can't look up our FAQ and policies right now, so I can't confirm that. "
                              "I can raise a support ticket if you like.")
        assert reply.citations == ()

    def test_production_never_falls_back_to_the_local_embedding(self, production_embeddings):
        assert "injected" not in rag.embedding_configuration()
        with pytest.raises(KnowledgeBaseError):
            rag.default_knowledge_base()
