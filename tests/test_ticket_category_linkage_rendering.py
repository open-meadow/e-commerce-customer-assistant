"""Phase 1, step 1d: category validation, order linkage and code-rendered ticket answers
(design NT-22 to NT-28, NT-51, NT-52, NT-60 to NT-65, NT-97).

Deliberately NOT covered, because it is not implemented (open owner decisions, see the Phase 1 report): asking when a proposed
category contradicts the customer's words, asking when the engine itself starts a ticket with no category, draft corrections,
Hindi and Hinglish category inference (inactive by owner decision OD-07).
"""

from __future__ import annotations

import pytest

from app.backend import Backend, Settings
from app.llm.base import LLMResult, ToolCall
from app.orchestrator import guard
from app.tools import flow_texts
from backend_helpers import Client, ScriptedLLM, calls

STORE = "My earbuds from ORD1001 stopped working after two days"


def proposing(arguments):
    """A model that proposes start_support_ticket with exactly ``arguments`` (and is the stand-in otherwise)."""
    llm = ScriptedLLM()

    def chat(system, messages, tools):
        last = next((m for m in reversed(messages) if m.get("role") == "user"), {}).get("content", "")
        if last.startswith("PROPOSE"):
            return calls(("start_support_ticket", arguments))
        return llm.base.chat(system, messages, tools, max_output_tokens=512)

    llm.chat_fn = chat
    return llm


def card_of(backend, text=f"PROPOSE {STORE}"):
    c = Client(backend, "C101")
    return c, c.say(text)


def rows(raw):
    return [dict(r) for r in raw.execute("SELECT * FROM support_tickets").fetchall()]


# ---------------------------------------------------------------- guard functions

class TestCategoryFunctions:
    @pytest.mark.parametrize("value,expected", [
        ("product", "product"), ("Product", "product"), ("PRODUCT", "product"), (" general ", "general"),
        ("damaged", "product"), ("Defective", "product"), ("delivery", "order"), ("parcel", "order"),
        ("charged", "payment"), ("exchange", "return"), ("replacement", "return"), ("other", "general")])
    def test_approved_english_aliases_and_case_are_normalised(self, value, expected):
        assert guard.normalize_category(value) == expected

    @pytest.mark.parametrize("value", ["refund", "Refund", "", None, 123, 1.5, [], "products", "return policy", "सामान", "paisa"])
    def test_everything_else_including_refund_is_not_a_category(self, value):
        assert guard.normalize_category(value) is None  # refund stays ambiguous (OD-07)

    def test_signals_are_english_only(self):
        assert guard.category_signals("my earbuds are damaged") == {"product"}
        assert guard.category_signals("I was charged twice") == {"payment"}
        assert guard.category_signals("my parcel arrived broken and I was charged twice") == {"order", "product", "payment"}
        for language in ("hinglish", "hi"):
            assert guard.category_signals("my earbuds are damaged", language) == set()  # inactive pending fluent review
        assert guard.category_signals("मेरा ऑर्डर नहीं आया") == set()
        assert guard.category_signals("paisa kat gaya") == set()


# ---------------------------------------------------------------- the model's proposal is validated, never repaired silently

class TestProposedCategory:
    def test_a_valid_category_goes_straight_to_the_card(self, temp_db, raw):
        _, card = card_of(Backend(llm=proposing({"issue_type": "product"})))
        assert card.status == "needs_confirmation" and "(product, order ORD1001)" in card.pending.summary

    @pytest.mark.parametrize("value,expected", [("Product", "product"), ("PRODUCT", "product"), ("damaged", "product"),
                                                ("general ", "general")])
    def test_aliases_and_case_are_normalised_and_shown_on_the_card(self, temp_db, value, expected):
        _, card = card_of(Backend(llm=proposing({"issue_type": value})))
        assert card.status == "needs_confirmation" and f"({expected}" in card.pending.summary

    @pytest.mark.parametrize("value", ["refund", "", None, 123, "nonsense"])
    def test_an_invalid_value_with_no_clear_signal_is_asked_about_and_nothing_is_stored(self, temp_db, raw, value):
        backend = Backend(llm=proposing({"issue_type": value}))
        c = Client(backend, "C101")
        out = c.say("PROPOSE my thing from ORD1001 stopped working after two days again")
        # 'stopped working' alone is no category word, so the customer is asked
        assert out.status == "needs_choice" and out.text == flow_texts.text("ticket_cat_question", "en")
        assert {o.option_id for o in out.choice.options} == {"order", "return", "payment", "product", "general", "cancel"}
        assert rows(raw) == []

    def test_a_single_clear_english_signal_is_only_a_suggestion_shown_on_the_card(self, temp_db, raw):
        _, card = card_of(Backend(llm=proposing({"issue_type": "refund"})), "PROPOSE my earbuds from ORD1001 are damaged")
        assert card.status == "needs_confirmation" and "(product" in card.pending.summary
        assert rows(raw) == []  # nothing saved until the customer confirms

    def test_refund_is_ambiguous_and_clarified_when_the_words_do_not_decide(self, temp_db):
        backend = Backend(llm=proposing({"issue_type": "refund"}))
        c = Client(backend, "C101")
        out = c.say("PROPOSE my refund for the return has not come from ORD1001")  # return and payment both fit
        assert out.status == "needs_choice"

    def test_choosing_a_category_gives_the_card_with_that_category(self, temp_db, raw):
        backend = Backend(llm=proposing({"issue_type": "nonsense"}))
        c = Client(backend, "C101")
        out = c.say("PROPOSE my thing from ORD1001 stopped working after two days again")
        card = c.pick(out, "payment")
        assert card.status == "needs_confirmation" and "(payment, order ORD1001)" in card.pending.summary
        done = c.confirm(card)
        assert "has been created" in done.text and rows(raw)[0]["issue_type"] == "payment"

    def test_cancelling_the_category_question_creates_nothing(self, temp_db, raw):
        backend = Backend(llm=proposing({"issue_type": "nonsense"}))
        c = Client(backend, "C101")
        out = c.say("PROPOSE my thing from ORD1001 stopped working after two days again")
        assert "cancelled" in c.pick(out, "cancel").text and rows(raw) == []

    def test_an_invented_category_never_reaches_the_database(self, temp_db, raw):
        backend = Backend(llm=proposing({"issue_type": "x'; DROP TABLE support_tickets;--"}))
        c = Client(backend, "C101")
        c.say("PROPOSE my thing from ORD1001 stopped working after two days again")
        assert rows(raw) == []

    def test_hindi_and_hinglish_have_no_active_inference_so_the_customer_is_asked(self, temp_db):
        backend = Backend(llm=proposing({"issue_type": "nonsense"}))
        c = Client(backend, "C101")
        out = c.say("PROPOSE mera order ORD1001 abhi tak nahi aaya, earbuds band ho gaya")
        assert out.status == "needs_choice"  # Hinglish text: no category word is read, and no silent 'general'

    def test_when_the_engine_starts_the_ticket_itself_the_existing_behaviour_is_unchanged(self, temp_db):
        c = Client(Backend(llm=ScriptedLLM()), "C101")
        c.say("I want to speak to a human agent")
        card = c.say(STORE)
        assert card.status == "needs_confirmation" and "(product" in card.pending.summary


# ---------------------------------------------------------------- order linkage

class TestOrderLinkage:
    def test_an_order_typed_before_a_clarification_is_linked(self, temp_db, raw):
        c = Client(Backend(llm=ScriptedLLM()), "C101")
        c.say("I want to speak to a human agent")  # the engine asks what it is about; the model proposes nothing
        card = c.say("order ORD1001 it stopped working after two days and the earbuds are broken")
        assert card.status == "needs_confirmation" and "order ORD1001" in card.pending.summary
        c.confirm(card)
        assert rows(raw)[0]["order_id"] == "ORD1001"

    def test_a_foreign_order_is_never_linked_and_nothing_is_disclosed(self, temp_db, raw):
        c = Client(Backend(llm=ScriptedLLM()), "C101")
        c.say("I want to speak to a human agent")
        card = c.say("order ORD1003 earbuds stopped working after two days")  # ORD1003 belongs to another customer
        assert card.status == "needs_confirmation" and "ORD1003" not in card.pending.summary.split("description")[0]
        c.confirm(card)
        assert rows(raw)[0]["order_id"] is None

    def test_several_of_the_customers_own_orders_are_asked_about_never_guessed(self, temp_db, raw):
        owned = [r[0] for r in raw.execute("SELECT order_id FROM orders WHERE customer_id = 'C101' ORDER BY order_id")][:2]
        assert len(owned) == 2
        c = Client(Backend(llm=ScriptedLLM()), "C101")
        c.say("I want to speak to a human agent")
        out = c.say(f"{owned[0]} and {owned[1]}: the earbuds stopped working after two days")
        assert out.status == "needs_choice" and {o.option_id for o in out.choice.options} == {*owned, "none", "cancel"}
        card = c.pick(out, owned[1])
        assert card.status == "needs_confirmation" and f"order {owned[1]}" in card.pending.summary
        c.confirm(card)
        assert rows(raw)[0]["order_id"] == owned[1]

    def test_choosing_none_creates_an_orderless_ticket(self, temp_db, raw):
        owned = [r[0] for r in raw.execute("SELECT order_id FROM orders WHERE customer_id = 'C101' ORDER BY order_id")][:2]
        c = Client(Backend(llm=ScriptedLLM()), "C101")
        c.say("I want to speak to a human agent")
        out = c.say(f"{owned[0]} and {owned[1]}: the earbuds stopped working after two days")
        card = c.pick(out, "none")
        assert card.status == "needs_confirmation"
        c.confirm(card)
        assert rows(raw)[0]["order_id"] is None

    def test_the_models_own_owned_order_is_still_used(self, temp_db, raw):
        _, card = card_of(Backend(llm=proposing({"issue_type": "product", "order_id": "ORD1001"})), "PROPOSE my earbuds are damaged")
        assert "order ORD1001" in card.pending.summary


# ---------------------------------------------------------------- ticket answers rendered from the database

def ticket_listing_model(prose):
    """A model that looks the tickets up and then says ``prose`` (which may be wrong or invented)."""
    llm = ScriptedLLM()

    def chat(system, messages, tools):
        last = next((m for m in reversed(messages) if m.get("role") == "user"), {}).get("content", "")
        if "ZZLIST" in last:
            if not any(m.get("role") == "tool" for m in messages):
                return LLMResult(tool_calls=(ToolCall("get_my_tickets", {}, "c1"),))
            return LLMResult(text=prose)
        return llm.base.chat(system, messages, tools, max_output_tokens=512)

    llm.chat_fn = chat
    return llm


def make_tickets(backend, n):
    c = Client(backend, "C101")
    ids = []
    for i in range(n):
        c.say("I want to speak to a human agent")
        card = c.say(f"{STORE} ({i})")
        done = c.confirm(card)
        ids.append(done.text.split("support ticket ")[1].split(" ")[0])
    return c, ids


class TestCodeRenderedTicketAnswers:
    def test_a_wrong_count_in_the_models_prose_cannot_reach_the_customer(self, temp_db):
        backend = Backend(llm=ticket_listing_model("You have 2 tickets."))
        c, ids = make_tickets(backend, 3)
        out = c.say("ZZLIST show my tickets please")
        assert "You have 3 support ticket(s)" in out.text and "2 tickets" not in out.text
        assert all(i in out.text for i in ids)

    def test_a_false_no_tickets_claim_is_replaced_by_the_data(self, temp_db):
        backend = Backend(llm=ticket_listing_model("You don't have any tickets on file right now."))
        c, ids = make_tickets(backend, 1)
        out = c.say("ZZLIST show my tickets please")
        assert ids[0] in out.text and "don't have any tickets" not in out.text

    def test_an_invented_id_in_the_prose_is_not_in_the_rendered_answer(self, temp_db):
        backend = Backend(llm=ticket_listing_model("Your ticket TKT_" + "0" * 32 + " is being handled."))
        c, ids = make_tickets(backend, 1)
        out = c.say("ZZLIST show my tickets please")
        assert "TKT_" + "0" * 32 not in out.text and ids[0] in out.text

    def test_the_status_category_and_order_come_from_the_row(self, temp_db, raw):
        backend = Backend(llm=ticket_listing_model("It is resolved and about a payment."))
        c, ids = make_tickets(backend, 1)
        out = c.say("ZZLIST show my tickets please")
        assert f"- {ids[0]}: product, order ORD1001, status Open, created" in out.text and "resolved" not in out.text.lower()

    def test_an_empty_history_is_stated_by_code(self, temp_db):
        backend = Backend(llm=ticket_listing_model("You have 4 open tickets."))
        c = Client(backend, "C101")
        out = c.say("ZZLIST show my tickets please")
        assert out.text == flow_texts.text("tickets_none", "en")

    def test_more_than_one_page_states_how_many_are_shown(self, temp_db):
        backend = Backend(llm=ticket_listing_model("whatever"), settings=Settings(ticket_rate_policy="off", session_requests_per_minute=600))
        c, ids = make_tickets(backend, 11)
        out = c.say("ZZLIST show my tickets please")
        assert "You have 11 support ticket(s)" in out.text and "Showing the 10 most recent of 11." in out.text
        assert out.text.count("TKT_") == 10

    def test_another_customers_tickets_are_never_listed(self, temp_db):
        backend = Backend(llm=ticket_listing_model("x"))
        other = Client(backend, "C102")
        other.say("I want to speak to a human agent")
        done = other.confirm(other.say("my charger from ORD1003 stopped working after two days"))
        foreign = done.text.split("support ticket ")[1].split(" ")[0]
        mine = Client(backend, "C101")
        assert foreign not in mine.say("ZZLIST show my tickets please").text

    @pytest.mark.parametrize("language", ["en", "hinglish", "hi"])
    def test_every_template_exists_in_all_three_languages(self, language):
        for key in ("tickets_none", "tickets_header", "tickets_line", "tickets_line_order", "tickets_more",
                    "ticket_cat_question", "ticket_order_question", "ticket_order_none"):
            assert flow_texts.TEXTS[language][key]

    def test_other_read_tools_in_the_same_turn_keep_the_models_wording(self, temp_db):
        """Only a turn that looked up nothing but tickets is rendered by code; a mixed turn is left to the model (the ID
        guard still applies)."""
        llm = ScriptedLLM()

        def chat(system, messages, tools):
            last = next((m for m in reversed(messages) if m.get("role") == "user"), {}).get("content", "")
            if "ZZLIST" in last:
                if not any(m.get("role") == "tool" for m in messages):
                    return LLMResult(tool_calls=(ToolCall("get_my_tickets", {}, "c1"), ToolCall("list_my_orders", {}, "c2")))
                return LLMResult(text="Here is the overview you asked for.")
            return llm.base.chat(system, messages, tools, max_output_tokens=512)

        llm.chat_fn = chat
        out = Client(Backend(llm=llm), "C101").say("ZZLIST show my tickets and orders please")
        assert "overview you asked for" in out.text
