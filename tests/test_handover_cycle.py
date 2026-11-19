"""Handover cycle: read-only order ambiguity (status and refund answers) and the remaining localisation (issue-management
prompts and notices, greeting, thanks, scope redirect, fallback item name). Offline: scripted models, a temporary database."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import app.backend  # noqa: E402,F401
from app import catalog as catalog_module  # noqa: E402
from app.backend import Backend, Settings  # noqa: E402
from app.llm.base import LLMResult  # noqa: E402
from app.orchestrator import engine as engine_module  # noqa: E402
from app.orchestrator import guard  # noqa: E402
from app.tools import flow_texts  # noqa: E402
from tests.backend_helpers import Client, ScriptedLLM, calls  # noqa: E402

LANGUAGES = ("en", "hinglish", "hi")
CHARGER_ORDERS = "ORD1002 (Shipped), ORD1014 (Processing)"  # C101's two orders that hold the 65W GaN Charger
CHARGER = "65W GaN Charger"


def scripted(*steps, interpret=True):
    queue = list(steps)
    seen = {"messages": []}

    def chat(system, messages, tools):
        seen["messages"].append(messages)
        step = queue.pop(0) if queue else LLMResult(text="MODEL TEXT")
        return step(system, messages) if callable(step) else step

    llm = ScriptedLLM(chat=chat)
    llm.interprets_language = interpret
    return Backend(llm=llm, settings=Settings()), llm, seen


@pytest.fixture
def executed(monkeypatch):
    log = []
    real = engine_module.execute_tool

    def spy(ctx, name, arguments):
        result = real(ctx, name, arguments)
        log.append((name, (arguments or {}).get("order_id") if isinstance(arguments, dict) else None, result.status))
        return result

    monkeypatch.setattr(engine_module, "execute_tool", spy)
    return log


def say(backend, customer, text):
    return Client(backend, customer).say(text)


# ======================================================================================================================
# 1. Read-only order ambiguity
# ======================================================================================================================

class TestStatusAndRefundAnswersDoNotPickAmongLookAlikeOrders:
    def test_a_status_lookup_of_one_of_two_chargers_becomes_a_question(self, temp_db, executed):
        backend, llm, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})))
        reply = say(backend, "C101", "Where is my charger?")
        assert reply.status == "ok" and llm.chat_calls == 1
        assert reply.text == flow_texts.text("which_order_status", "en", product=CHARGER, orders=CHARGER_ORDERS)
        assert executed == []  # nothing about either order was read, so nothing about either can be shown
        assert not any(w in reply.text for w in ("₹", "November", "Total", "refund"))  # IDs and statuses only

    def test_a_refund_lookup_of_one_of_two_chargers_becomes_a_question(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_refund_policy", {"order_id": "ORD1014"})))
        reply = say(backend, "C101", "What about the refund for my charger?")
        assert reply.text == flow_texts.text("which_order_status", "en", product=CHARGER, orders=CHARGER_ORDERS)
        assert executed == []

    @pytest.mark.parametrize("language,message", [
        ("hinglish", "mera charger kahan hai, kab aayega yaar"),
        ("hi", "मेरा चार्जर कहाँ है")])
    def test_the_question_is_in_the_customers_language(self, temp_db, language, message):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})))
        # the Devanagari message names the product in Devanagari; add the catalogue word so the product is recognised
        text = message if language == "hinglish" else message + " charger"
        reply = say(backend, "C101", text)
        assert reply.text == flow_texts.text("which_order_status", language, product=CHARGER, orders=CHARGER_ORDERS)

    def test_looking_up_every_candidate_in_one_turn_still_asks_and_reads_nothing(self, temp_db, executed):
        backend, llm, seen = scripted(calls(("get_order_status", {"order_id": "ORD1002"}),
                                            ("get_order_status", {"order_id": "ORD1014"})),
                                      LLMResult(text="SHOULD NEVER BE USED"))
        reply = say(backend, "C101", "Where is my charger?")
        assert reply.text == flow_texts.text("which_order_status", "en", product=CHARGER, orders=CHARGER_ORDERS)
        assert executed == [] and llm.chat_calls == 1  # no read ran, and the model never saw any order's details
        assert not any(m.get("role") == "tool" for m in seen["messages"][-1])

    def test_a_typed_order_id_settles_it(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})), LLMResult(text="Shipped."))
        assert say(backend, "C101", "Where is my charger from ORD1002?").text == "Shipped."
        assert executed == [("get_order_status", "ORD1002", "ok")]

    def test_an_id_typed_earlier_in_the_conversation_settles_it(self, temp_db, executed):
        backend, _, _ = scripted(LLMResult(text="Which?"), calls(("get_order_status", {"order_id": "ORD1002"})),
                                 LLMResult(text="Shipped."))
        client = Client(backend, "C101")
        client.say("I am asking about ORD1002")
        assert client.say("Where is my charger?").text == "Shipped."

    def test_one_matching_order_needs_no_question(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1001"})), LLMResult(text="Delivered."))
        assert say(backend, "C101", "Where are my earbuds?").text == "Delivered."  # only ORD1001 holds earbuds

    def test_a_message_naming_no_product_is_left_to_the_model(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})), LLMResult(text="Shipped."))
        assert say(backend, "C101", "Where is my order?").text == "Shipped."  # the prompt tells it to list, not pick

    def test_an_order_the_message_does_not_describe_is_not_second_guessed(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1001"})), LLMResult(text="Delivered."))
        assert say(backend, "C101", "Where is my charger?").text == "Delivered."  # ORD1001 holds no charger: not ambiguity

    def test_the_offline_stand_in_is_unchanged(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})), LLMResult(text="Shipped."),
                                 interpret=False)
        assert say(backend, "C101", "Where is my charger?").text == "Shipped."

    def test_an_unavailable_catalogue_means_no_question_rather_than_a_wrong_one(self, temp_db, monkeypatch):
        def broken():
            raise catalog_module.CatalogError("down")

        monkeypatch.setattr(catalog_module, "default_catalog", broken)
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})), LLMResult(text="Shipped."))
        assert say(backend, "C101", "Where is my charger?").text == "Shipped."

    def test_other_customers_orders_are_never_part_of_the_question(self, temp_db):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})))
        reply = say(backend, "C101", "Where is my charger?")  # C105's ORD1010 also holds a charger product
        assert "ORD1010" not in reply.text and "ORD1002" in reply.text  # candidates come from C101's own orders only


class TestProductMatching:
    @pytest.mark.parametrize("message,name,expected", [
        ("where is my charger", CHARGER, True), ("where are my chargers", CHARGER, True),
        ("where are my earbuds", CHARGER, False), ("Where is my order?", CHARGER, False),
        ("charger from ORD1002", CHARGER, True), ("", CHARGER, False), ("charger", "", False)])
    def test_names_product(self, message, name, expected):
        assert guard.names_product(message, name) is expected


# ======================================================================================================================
# 2. The remaining localisation
# ======================================================================================================================

class TestGreetingThanksAndScope:
    @pytest.mark.parametrize("message,language", [("hello", "en"), ("namaste", "hinglish"), ("Namaste!", "hinglish"),
                                                  ("नमस्ते", "hi")])
    def test_a_greeting_is_answered_in_the_customers_language(self, temp_db, message, language):
        backend, _, _ = scripted()
        assert say(backend, "C101", message).text == flow_texts.text("greeting", language)

    @pytest.mark.parametrize("message,language", [("thanks", "en"), ("thank you!", "en"), ("shukriya", "hinglish"),
                                                  ("dhanyavaad ji", "hinglish"),
                                                  ("धन्यवाद", "hi"),
                                                  ("शुक्रिया", "hi")])
    def test_thanks_is_answered_in_the_customers_language(self, temp_db, message, language):
        backend, _, _ = scripted()
        assert say(backend, "C101", message).text == flow_texts.text("thanks", language)

    @pytest.mark.parametrize("message,language", [
        ("what is the weather today", "en"), ("aaj mausam kaisa hai", "hinglish"),
        ("मौसम कैसा है", "hi")])
    def test_a_scope_redirect_is_given_in_the_customers_language(self, temp_db, message, language):
        backend, llm, _ = scripted()
        assert say(backend, "C101", message).text == flow_texts.text("scope_redirect", language)
        assert llm.chat_calls == 0  # answered by code; the model is never asked

    def test_the_english_sentences_are_the_ones_the_engine_always_said(self):
        assert flow_texts.text("greeting") == guard.GREETING_REPLY
        assert flow_texts.text("thanks") == guard.THANKS_REPLY
        assert flow_texts.text("scope_redirect") == guard.SCOPE_REDIRECT

    def test_an_expired_prompt_is_announced_in_the_customers_language(self, temp_db, bnow):
        llm = ScriptedLLM(labels=["related_but_unclear"])
        backend = Backend(llm=llm, settings=Settings())
        client = Client(backend, "C101")
        client.say(ISSUE_A)
        assert client.say(ISSUE_B).status == "needs_choice"
        bnow(minutes=11)
        assert client.say("shukriya").text == flow_texts.text("timed_out", "hinglish") + "\n\n" + flow_texts.text(
            "thanks", "hinglish")


ISSUE_A = "mere earbuds broken hain, kya karu yaar"
ISSUE_B = "mera charger damaged aaya hai, yeh kya baat hai bhai"
ISSUE_C = "mera monitor defect hai, screen cracked hai bhai"


def issue_client(labels):
    llm = ScriptedLLM(labels=list(labels))
    backend = Backend(llm=llm, settings=Settings())
    return backend, Client(backend, "C101")


class TestIssueManagementInTheCustomersLanguage:
    def test_the_relation_question_and_its_buttons(self, temp_db):
        _, client = issue_client(["related_but_unclear"])
        client.say(ISSUE_A)
        out = client.say(ISSUE_B)
        assert out.status == "needs_choice" and out.text == flow_texts.text("relation_question", "hinglish")
        assert {o.option_id: o.label for o in out.choice.options} == {
            "same_issue": flow_texts.text("relation_same", "hinglish"),
            "new_issue": flow_texts.text("relation_new", "hinglish"),
            "cancel": flow_texts.text("cat_cancel", "hinglish")}

    def test_leaving_things_as_they_are(self, temp_db):
        _, client = issue_client(["related_but_unclear"])
        client.say(ISSUE_A)
        assert client.pick(client.say(ISSUE_B), "cancel").text == flow_texts.text("relation_left", "hinglish")

    def test_putting_the_earlier_issue_on_hold_is_noted(self, temp_db):
        _, client = issue_client(["new_issue"])
        client.say(ISSUE_A)
        assert flow_texts.text("notice_on_hold", "hinglish") in client.say(ISSUE_B).text

    def test_the_two_issue_limit_question_buttons_and_outcomes(self, temp_db):
        _, client = issue_client(["new_issue", "new_issue"])
        client.say(ISSUE_A)
        client.say(ISSUE_B)
        out = client.say(ISSUE_C)
        assert out.text == flow_texts.text("third_question", "hinglish")
        assert {o.option_id: o.label for o in out.choice.options} == {
            "drop_active": flow_texts.text("third_drop_active", "hinglish"),
            "drop_suspended": flow_texts.text("third_drop_suspended", "hinglish"),
            "keep": flow_texts.text("third_keep", "hinglish")}
        assert client.pick(out, "keep").text == flow_texts.text("third_kept", "hinglish")

    @pytest.mark.parametrize("option,notice", [("drop_active", "notice_dropped_active"),
                                               ("drop_suspended", "notice_dropped_hold")])
    def test_dropping_an_issue_is_announced(self, temp_db, option, notice):
        _, client = issue_client(["new_issue", "new_issue"])
        client.say(ISSUE_A)
        client.say(ISSUE_B)
        assert flow_texts.text(notice, "hinglish") in client.pick(client.say(ISSUE_C), option).text

    def test_english_still_gets_the_original_english(self, temp_db):
        _, client = issue_client(["related_but_unclear"])
        client.say("My earbuds stopped working after two days")
        out = client.say("My charger arrived damaged and the box was broken")
        assert out.text == "Is this part of the issue we're already working on, or a separate issue?"
        assert [o.label for o in out.choice.options] == ["Same issue", "A separate issue", "Never mind"]


class TestTheFallbackItemName:
    @pytest.mark.parametrize("language,expected", [("en", "the item"), ("hinglish", "yeh item"),
                                                   ("hi", "यह आइटम")])
    def test_an_item_with_no_catalogue_name_is_described_in_the_customers_language(self, language, expected, monkeypatch):
        def broken():
            raise catalog_module.CatalogError("down")

        monkeypatch.setattr(catalog_module, "default_catalog", broken)
        items = [{"item_id": "IT1", "product_id": "P1"}]
        assert engine_module.Engine._item_name(items, "IT1", language) == expected
        assert engine_module.Engine._item_name(items, "IT-UNKNOWN", language) == expected

    def test_the_default_is_still_english(self):
        assert engine_module.Engine._item_name([], "IT1") == "the item"


class TestCourtesyWordsSettleTheLanguage:
    @pytest.mark.parametrize("text,expected", [("namaste", "hinglish"), ("shukriya", "hinglish"),
                                               ("dhanyavaad ji", "hinglish"), ("namaste bhai", "hinglish"),
                                               ("नमस्ते", "hi"), ("hello", None),
                                               ("namaste where is my order please", "en")])
    def test_decisive_language(self, text, expected):
        assert guard.decisive_language(text) == expected

    @pytest.mark.parametrize("text,kind", [("namaste", "greeting"), ("namaskar", "greeting"),
                                           ("नमस्कार", "greeting"),
                                           ("shukriya", "thanks"), ("dhanyawad", "thanks"),
                                           ("धन्यवाद", "thanks"),
                                           ("aaj mausam kaisa hai", "out_of_scope"),
                                           ("mera order kab aayega", "normal")])
    def test_classification(self, text, kind):
        assert guard.classify_scope(text, specific=False) == kind


class TestTheCatalogueIsComplete:
    def test_the_catalogue_now_holds_every_sentence_the_engine_speaks(self):
        assert len(flow_texts.TEXTS["en"]) >= 104
        engine_source = (Path(__file__).resolve().parents[1] / "app" / "orchestrator" / "engine.py").read_text(
            encoding="utf-8")
        assert "guard.GREETING_REPLY" not in engine_source and "guard.THANKS_REPLY" not in engine_source
        assert "guard.SCOPE_REDIRECT" not in engine_source

    def test_no_english_literal_is_left_in_a_customer_facing_turn(self):
        """Every customer-facing Turn(...) in the engine takes its text from the catalogue (or from a helper that does)."""
        import re
        source = (Path(__file__).resolve().parents[1] / "app" / "orchestrator" / "engine.py").read_text(encoding="utf-8")
        literal = re.findall(r'Turn\(\s*f?"[A-Z][a-z]', source) + re.findall(r'notices\.append\(\s*f?"', source)
        assert literal == [], literal


class TestNothingIsReadOrShownBeforeTheCustomerClarifies:
    QUESTION = flow_texts.text("which_order_status", "en", product=CHARGER, orders=CHARGER_ORDERS)

    @pytest.mark.parametrize("proposal", [
        calls(("get_order_status", {"order_id": "ORD1002"})), calls(("get_refund_policy", {"order_id": "ORD1014"})),
        calls(("get_order_status", {"order_id": "ORD1002"}), ("get_order_status", {"order_id": "ORD1014"})),
        calls(("list_my_orders", {})),
        calls(("list_my_orders", {}), ("get_order_status", {"order_id": "ORD1002"}))])
    def test_whatever_the_model_proposes_the_customer_is_asked_and_no_tool_runs(self, temp_db, executed, proposal):
        backend, llm, seen = scripted(proposal, LLMResult(text="SHOULD NEVER BE USED"))
        reply = say(backend, "C101", "Where is my charger?")
        assert reply.text == self.QUESTION
        assert executed == []  # no lookup, no listing, nothing read: account-scoped or not
        assert llm.chat_calls == 1 and not any(m.get("role") == "tool" for m in seen["messages"][-1])

    def test_what_the_customer_sees_is_ids_statuses_and_the_product_name_only(self, temp_db):
        backend, _, _ = scripted(calls(("list_my_orders", {})))
        reply = say(backend, "C101", "Where is my charger?")
        without_allowed = reply.text
        for allowed in (CHARGER, "ORD1002", "ORD1014", "Shipped", "Processing"):
            without_allowed = without_allowed.replace(allowed, "")
        assert not re.search(r"\d", without_allowed)  # no dates, totals, prices, quantities
        assert not any(w in reply.text for w in ("\u20b9", "November", "Total", "refund", "Paid", "item IT"))

    def test_a_customer_who_asks_for_their_own_orders_in_so_many_words_still_gets_the_listing(self, temp_db, executed):
        backend, _, _ = scripted(calls(("list_my_orders", {})), LLMResult(text="Here are all your orders."))
        assert say(backend, "C101", "Show me all my orders with a charger").text == "Here are all your orders."
        assert ("list_my_orders", None, "ok") in executed

    def test_after_the_customer_names_an_order_the_ordinary_account_scoped_lookup_runs(self, temp_db, executed):
        backend, _, _ = scripted(calls(("list_my_orders", {})), calls(("get_order_status", {"order_id": "ORD1002"})),
                                 LLMResult(text="It is on its way."))
        client = Client(backend, "C101")
        assert client.say("Where is my charger?").text == self.QUESTION
        assert client.say("ORD1002").text == "It is on its way."
        assert executed == [("get_order_status", "ORD1002", "ok")]  # only now, only the named order

    def test_the_customer_cannot_use_the_question_to_reach_another_customers_order(self, temp_db, executed):
        backend, _, _ = scripted(calls(("list_my_orders", {})), calls(("get_order_status", {"order_id": "ORD1010"})),
                                 LLMResult(text="I couldn't find it."))
        client = Client(backend, "C101")
        client.say("Where is my charger?")
        client.say("ORD1010")  # another customer's order, typed by the customer
        assert executed == [("get_order_status", "ORD1010", "not_found")]  # scoped: not found, nothing revealed

    def test_nothing_is_written_while_asking(self, temp_db, raw):
        backend, _, _ = scripted(calls(("list_my_orders", {})))
        say(backend, "C101", "Where is my charger?")
        assert raw.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0


PROPOSALS = {
    "status": calls(("get_order_status", {"order_id": "ORD1002"})),
    "refund": calls(("get_refund_policy", {"order_id": "ORD1014"})),
    "both candidates": calls(("get_order_status", {"order_id": "ORD1002"}), ("get_order_status", {"order_id": "ORD1014"})),
    "listing": calls(("list_my_orders", {})),
}


class TestEveryToolTypeAgainstTheWaysACustomerCanSettleIt:
    """The same four proposals, in each situation: nothing typed (ask), typed in the message, typed earlier, other account."""

    @pytest.mark.parametrize("kind", PROPOSALS)
    def test_nothing_typed_asks_and_runs_no_tool(self, temp_db, executed, kind):
        backend, llm, _ = scripted(PROPOSALS[kind], LLMResult(text="SHOULD NEVER BE USED"))
        reply = say(backend, "C101", "Where is my charger?")
        assert reply.text == flow_texts.text("which_order_status", "en", product=CHARGER, orders=CHARGER_ORDERS)
        assert executed == [] and llm.chat_calls == 1

    @pytest.mark.parametrize("kind", PROPOSALS)
    def test_an_id_typed_in_the_message_lets_the_tools_run(self, temp_db, executed, kind):
        backend, _, _ = scripted(PROPOSALS[kind], LLMResult(text="Answered."))
        assert say(backend, "C101", "Where is my charger from ORD1002?").text == "Answered."
        assert executed  # the ordinary account-scoped tools ran

    @pytest.mark.parametrize("kind", PROPOSALS)
    def test_an_id_typed_earlier_in_the_conversation_lets_the_tools_run(self, temp_db, executed, kind):
        backend, _, _ = scripted(LLMResult(text="Noted."), PROPOSALS[kind], LLMResult(text="Answered."))
        client = Client(backend, "C101")
        client.say("I am asking about ORD1002")
        executed.clear()
        assert client.say("Where is my charger?").text == "Answered."
        assert executed

    @pytest.mark.parametrize("kind", PROPOSALS)
    def test_another_customers_look_alike_orders_do_not_create_a_question_or_appear_in_one(self, temp_db, executed, kind):
        """C101 holds two charger orders. C103 holds one (ORD1005), so for C103 there is nothing to ask, and no reply to
        C103 may mention C101's orders; and C101's question never lists C103's."""
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1005"})), LLMResult(text="Delivered."))
        assert say(backend, "C103", "Where is my charger?").text == "Delivered."
        asked = say(scripted(PROPOSALS[kind])[0], "C101", "Where is my charger?").text
        assert "ORD1005" not in asked and "ORD1010" not in asked

    def test_account_scoping_holds_when_the_customer_types_someone_elses_id_after_the_question(self, temp_db, executed):
        backend, _, _ = scripted(PROPOSALS["listing"], calls(("get_order_status", {"order_id": "ORD1005"})),
                                 LLMResult(text="I couldn't find that order."))
        client = Client(backend, "C101")
        assert client.say("Where is my charger?").text.startswith("You have more than one order")
        client.say("ORD1005")  # C103's order
        assert executed == [("get_order_status", "ORD1005", "not_found")]
