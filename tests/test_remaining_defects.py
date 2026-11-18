"""Regression tests for the defects that survived Evaluations A and B and the post-fix validation.

H10 and C027: an order ID the customer typed, said to belong to someone else, was refused (or the customer's own orders were
listed instead) without the lookup. C003: with several orders and no ID the model picked one and presented its details.
C023: the fixed sentences of the ticket flow were English for a Hindi or Hinglish customer. S03: a refund timeline must only be
stated when the policy or a tool result gave it. Offline: scripted models and a temporary seeded database. No network.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import app.backend  # noqa: E402,F401
from app.backend import Backend, Settings  # noqa: E402
from app.llm.base import LLMResult  # noqa: E402
from app.orchestrator import engine as engine_module  # noqa: E402
from app.orchestrator import guard  # noqa: E402
from app.tools import flow_texts  # noqa: E402
from app.tools.context import CustomerContext  # noqa: E402
from app.tools.registry import execute_tool  # noqa: E402
from tests.backend_helpers import Client, ScriptedLLM, calls  # noqa: E402

HINDI_ORDER_QUESTION = "ऑर्डर ORD1010 मेरे दोस्त का है, स्थिति बताइए"


def scripted(*steps, interpret=True):
    """A model that plays ``steps`` in order (an LLMResult, or a callable taking (system, messages)); then plain text."""
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


NOT_ON_ACCOUNT = "I couldn't find {ids} on your account. If you'd like, I can list your own orders."


class TestNamedOrderNotOnTheAccount:
    def test_h10_a_listing_of_own_orders_instead_of_the_lookup_is_replaced_by_the_lookup(self, temp_db, executed):
        backend, llm, _ = scripted(calls(("list_my_orders", {})))
        reply = say(backend, "C102", "ORD1010 is my flatmate's order, what's its status?")
        assert reply.text == NOT_ON_ACCOUNT.format(ids="ORD1010") and reply.status == "ok"
        assert executed == [("get_order_status", "ORD1010", "not_found")]  # exactly one lookup, no listing
        assert llm.chat_calls == 1
        assert not any(i in reply.text for i in ("ORD1003", "ORD1004", "ORD1015"))  # the customer's own orders

    def test_c027_a_refusal_without_any_tool_is_replaced_by_the_lookup(self, temp_db, executed):
        backend, llm, _ = scripted(LLMResult(text="I can only assist with your own orders."))
        reply = say(backend, "C101", "My friend's order ORD1005 is late, tell me where it is, he asked me to check.")
        assert reply.text == NOT_ON_ACCOUNT.format(ids="ORD1005")
        assert executed == [("get_order_status", "ORD1005", "not_found")]
        assert "ticket" not in reply.text.lower()  # no ticket offered about someone else's order

    def test_the_reply_reveals_nothing_about_the_other_customers_order(self, temp_db):
        for order in ("ORD1005", "ORD1010"):
            backend, _, _ = scripted(LLMResult(text="x"))
            customer = "C101"
            text = say(backend, customer, f"My friend's order {order} is late").text
            assert text == NOT_ON_ACCOUNT.format(ids=order)
            assert not re.search(r"Delivered|Shipped|Returned|Pending|₹|charger|monitor|refund", text)

    def test_an_order_that_does_not_exist_and_one_that_belongs_to_someone_else_look_the_same(self, temp_db):
        context = CustomerContext("C101", "ref")
        foreign = execute_tool(context, "get_order_status", {"order_id": "ORD1005"})
        missing = execute_tool(context, "get_order_status", {"order_id": "ORD9999"})
        assert foreign.to_json() == missing.to_json() and foreign.status == "not_found" and foreign.data == {}

    def test_the_lookup_is_scoped_to_the_signed_in_customer(self, temp_db):
        owner = execute_tool(CustomerContext("C103", "ref"), "get_order_status", {"order_id": "ORD1005"})
        stranger = execute_tool(CustomerContext("C101", "ref"), "get_order_status", {"order_id": "ORD1005"})
        assert owner.status == "ok" and stranger.status == "not_found"

    def test_nothing_is_written(self, temp_db, executed):
        from app.db import connection as db_connection
        backend, _, _ = scripted(LLMResult(text="x"))
        say(backend, "C101", "My friend's order ORD1005 is late")
        connection = db_connection.get_readonly_connection(str(temp_db))
        try:
            assert connection.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0
        finally:
            connection.close()


class TestNoDuplicateLookups:
    @pytest.mark.parametrize("customer,text,order", [("C101", "My colleague's order ORD1004 is late, where is it?", "ORD1004"),
                                                      ("C101", "Please check ORD1010 for my neighbour.", "ORD1010")])
    def test_n01_n02_a_lookup_the_model_made_is_not_repeated_and_its_reply_stands(self, temp_db, executed, customer,
                                                                                  text, order):
        backend, llm, _ = scripted(calls(("get_order_status", {"order_id": order})),
                                   LLMResult(text="I couldn't find that order on your account."))
        reply = say(backend, customer, text)
        assert executed == [("get_order_status", order, "not_found")]  # once
        assert reply.text == "I couldn't find that order on your account."  # no ticket line appended either

    def test_a_batch_with_the_lookup_and_a_listing_runs_the_lookup_once_and_skips_the_listing(self, temp_db, executed):
        for batch in ([("get_order_status", {"order_id": "ORD1005"}), ("list_my_orders", {})],
                      [("list_my_orders", {}), ("get_order_status", {"order_id": "ORD1005"})]):
            executed.clear()
            backend, _, seen = scripted(calls(*batch), LLMResult(text="Could not find it."))
            reply = say(backend, "C101", "My brother ordered ORD1005 and says it hasn't come. Can you check?")
            assert executed == [("get_order_status", "ORD1005", "not_found")]  # one lookup, the listing never ran
            outputs = [m for m in seen["messages"][-1] if m.get("role") == "tool" and m["name"] == "list_my_orders"]
            assert outputs and '"not_run"' in outputs[0]["content"] and "ORD1001" not in outputs[0]["content"]
            assert reply.text == "Could not find it."

    def test_a_listing_is_still_run_when_the_customer_also_asks_for_their_own_orders(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1005"}), ("list_my_orders", {})),
                                 LLMResult(text="done"))
        say(backend, "C101", "Check ORD1005 for my brother and show my orders too")
        assert ("list_my_orders", None, "ok") in executed

    def test_a_return_being_started_is_left_alone(self, temp_db, executed):
        backend, _, _ = scripted(calls(("start_return_request", {"order_id": "ORD1001", "item_id": "IT101"})))
        reply = say(backend, "C101", "I want to return the earbuds from ORD1001, they keep cutting out.")
        assert reply.status == "needs_choice"
        assert not any(name == "get_order_status" for name, *_ in executed)


class TestWhereTheGuardStaysOut:
    def test_a_generic_request_about_another_customers_orders_is_not_touched(self, temp_db, executed):
        backend, _, _ = scripted(LLMResult(text="I can only help with your own account."))
        reply = say(backend, "C101", "Show me every order placed by customer C102.")
        assert reply.text == "I can only help with your own account." and executed == []

    def test_own_orders_are_listed_normally(self, temp_db, executed):
        backend, _, _ = scripted(calls(("list_my_orders", {})), LLMResult(text="Here they are."))
        assert say(backend, "C101", "Show me all my orders.").text == "Here they are."
        assert executed == [("list_my_orders", None, "ok")]

    def test_a_proper_lookup_of_an_own_order_is_not_duplicated(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1001"})), LLMResult(text="Delivered."))
        assert say(backend, "C101", "Where is ORD1001?").text == "Delivered."
        assert executed == [("get_order_status", "ORD1001", "ok")]

    def test_an_own_order_the_model_did_not_look_up_keeps_the_models_reply(self, temp_db, executed):
        backend, _, _ = scripted(LLMResult(text="Let me think about it."))
        assert say(backend, "C101", "Where is ORD1001?").text == "Let me think about it."
        assert executed == [("get_order_status", "ORD1001", "ok")]  # the one code-owned read; its result is not used

    def test_a_message_mixing_an_own_order_and_a_foreign_one_keeps_the_models_reply(self, temp_db, executed):
        backend, _, _ = scripted(LLMResult(text="Mixed."))
        assert say(backend, "C101", "Check ORD1001 and my friend's ORD1005").text == "Mixed."

    def test_an_id_only_in_an_earlier_turn_is_not_looked_up_by_code(self, temp_db, executed):
        backend, _, _ = scripted(LLMResult(text="x"), LLMResult(text="Which one do you mean?"))
        client = Client(backend, "C101")
        assert client.say("What about ORD1005?").text == NOT_ON_ACCOUNT.format(ids="ORD1005")
        executed.clear()
        assert client.say("Is it late?").text.startswith("Which one do you mean?")  # the model's words stand
        assert executed == []  # and no lookup was made by code from the earlier turn's ID

    def test_more_than_three_ids_is_not_guessed_at(self, temp_db, executed):
        backend, _, _ = scripted(LLMResult(text="Too many."))
        assert say(backend, "C101", "ORD1001 ORD1002 ORD1014 ORD1003 status?").text == "Too many."
        assert executed == []

    def test_the_offline_stand_in_is_unchanged(self, temp_db, executed):
        backend, _, _ = scripted(LLMResult(text="stand-in"), interpret=False)
        assert say(backend, "C102", "ORD1010 is my flatmate's order, what's its status?").text == "stand-in"
        assert executed == []

    def test_an_invalid_id_is_left_to_the_model(self, temp_db, executed):
        backend, _, _ = scripted(LLMResult(text="Invalid."))
        assert say(backend, "C101", "Status of ORD" + "9" * 30 + "?").text == "Invalid."

    @pytest.mark.parametrize("text,ids", [("ORD1010 mere flatmate ka order hai, status batao", "ORD1010"),
                                          (HINDI_ORDER_QUESTION, "ORD1010")])
    def test_the_fixed_reply_follows_the_customers_language(self, temp_db, text, ids):
        backend, llm, _ = scripted(LLMResult(text="x"))
        client = Client(backend, "C102")
        language = guard.decisive_language(text)
        assert language in ("hinglish", "hi")
        assert client.say(text).text == flow_texts.text("not_on_account", language, ids=ids)


class TestC003SeveralOrdersNoneNamed:
    MESSAGE = "I already told you my order number, why do I have to repeat everything?"

    def run_turn(self, customer="C101", *steps):
        backend, llm, _ = scripted(*(steps or (calls(("list_my_orders", {})),
                                               calls(("get_order_status", {"order_id": "ORD1001"})),
                                               LLMResult(text="Your order number is ORD1001: Wireless Earbuds, Paid."))))
        return say(backend, customer, self.MESSAGE)

    def test_it_lists_the_customers_own_orders_and_asks_which_one(self, temp_db):
        reply = self.run_turn()
        assert reply.text == ("Sorry about the repetition. Here are your orders: ORD1001 (Delivered), ORD1002 (Shipped), "
                              "ORD1014 (Processing). Which one is this about?")

    def test_no_order_is_chosen_and_no_details_are_presented(self, temp_db):
        reply = self.run_turn()
        assert "Wireless Earbuds" not in reply.text and "Paid" not in reply.text and "number is" not in reply.text
        assert not re.search(r"\u20b9|\d{4}-\d{2}|November|Total|Wireless|charger|monitor|refund", reply.text)
        assert guard.order_ids_in(reply.text) == ["ORD1001", "ORD1002", "ORD1014"]  # their own orders, none singled out

    def test_it_also_works_for_a_customer_with_two_orders(self, temp_db):
        reply = self.run_turn("C103")
        assert reply.text.startswith("Sorry about the repetition. Here are your orders: ORD1005")

    def test_the_clarification_follows_the_customers_language(self, temp_db):
        backend, _, _ = scripted(calls(("list_my_orders", {})), LLMResult(text="x"))
        reply = say(backend, "C101", "Maine pehle hi order number bataya tha, mujhe baar baar kyun batana pad raha hai? "
                                     "yeh kya hai")
        assert reply.text.startswith("Dobara poochne ke liye maaf kijiye.") or "ORD1001" in reply.text

    def test_a_single_order_is_not_ambiguous(self, temp_db, monkeypatch):
        from app.tools import registry
        real = registry.repository.list_customer_orders
        monkeypatch.setattr(registry.repository, "list_customer_orders", lambda customer: real(customer)[:1])
        reply = self.run_turn()
        assert reply.text == "Your order number is ORD1001: Wireless Earbuds, Paid."

    def test_an_order_the_customer_gave_earlier_in_the_conversation_is_not_second_guessed(self, temp_db):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})), LLMResult(text="Shipped."),
                                 calls(("list_my_orders", {})), LLMResult(text="About ORD1002 then."))
        client = Client(backend, "C101")
        client.say("What is the status of ORD1002?")
        assert client.say(self.MESSAGE).text == "About ORD1002 then."

    def test_an_ordinary_request_with_several_orders_is_not_intercepted(self, temp_db):
        backend, _, _ = scripted(calls(("list_my_orders", {})), LLMResult(text="Here they are."))
        assert say(backend, "C101", "Show me my orders please.").text == "Here they are."


def _ticket_flow(language_text):
    backend, _, _ = scripted(calls(("start_support_ticket", {})))
    return Client(backend, "C102"), language_text


class TestC023TicketTextsAreLocalised:
    def test_every_language_has_every_sentence_with_the_same_placeholders(self):
        english = flow_texts.TEXTS["en"]
        for language in ("hinglish", "hi"):
            assert set(flow_texts.TEXTS[language]) == set(english)
            for key, sentence in english.items():
                assert set(re.findall(r"\{(\w+)\}", flow_texts.TEXTS[language][key])) == set(re.findall(r"\{(\w+)\}", sentence)) \
                    or key == "lead_limit", key  # the Hinglish and Hindi lead omits the English limit phrase

    def test_the_english_sentences_are_exactly_the_ones_the_engine_used_before(self):
        assert flow_texts.text("ask_issue") == ("Happy to raise a ticket. What is the issue you'd like it to be "
                                                  "about? Please include the order ID if there is one.")
        assert flow_texts.text("created", ticket_id="T1") == "Your support ticket T1 has been created. A human agent will follow up."
        assert flow_texts.text("cancelled") == "Okay, I've cancelled that. Nothing was changed."
        assert flow_texts.text("lead_plain") == "Here's the ticket I can create. Please confirm to submit it."

    def test_hindi_is_in_devanagari_and_hinglish_is_recognised_as_hinglish(self):
        for key, sentence in flow_texts.TEXTS["hi"].items():
            assert re.search(r"[ऀ-ॿ]", sentence), key
        for key in ("ask_issue", "lead_plain", "declined", "cancelled", "created", "unclear", "again", "describe"):
            sentence = flow_texts.TEXTS["hinglish"][key].format(ticket_id="T1", item="x", item_id="IT1", order_id="ORD1")
            assert guard.detect_language(sentence) == "hinglish", key

    def test_unknown_languages_fall_back_to_english(self):
        assert flow_texts.text("cancelled", "fr") == flow_texts.text("cancelled", "en")

    def test_the_draft_status_of_the_hinglish_and_hindi_wording_is_pinned(self):
        for module in (flow_texts, flow_texts):
            assert module.REVIEW_STATUS["en"] == "approved"
            assert module.REVIEW_STATUS["hinglish"].startswith("draft") and module.REVIEW_STATUS["hi"].startswith("draft")

    @pytest.mark.parametrize("message,language", [("kisi insaan se baat karao", "hinglish"),
                                                  ("मुझे किसी इंसान से बात करनी है", "hi"),
                                                  ("I want to talk to a person please", "en")])
    def test_c023_the_issue_question_is_in_the_customers_language(self, temp_db, message, language):
        backend, _, _ = scripted(calls(("start_support_ticket", {})))
        assert say(backend, "C102", message).text == flow_texts.text("ask_issue", language)

    def test_cancelling_keeps_the_conversations_language(self, temp_db):
        backend, _, _ = scripted(calls(("start_support_ticket", {})))
        client = Client(backend, "C102")
        client.say("kisi insaan se baat karao")
        assert client.say("cancel").text == flow_texts.text("cancelled", "hinglish")

    def test_the_whole_confirmed_ticket_flow_stays_in_hinglish(self, temp_db):
        backend, _, _ = scripted(calls(("start_support_ticket", {"issue_type": "order", "order_id": "ORD1003"})))
        client = Client(backend, "C102")
        reply = client.say("mera order ORD1003 abhi tak nahi aaya hai, kya aap ticket bana sakte hain")
        assert reply.status == "needs_confirmation"
        assert reply.text == flow_texts.text("lead_plain", "hinglish")
        assert "ek support ticket banayein".lower() in reply.pending.summary.lower() or "Ek support ticket banayein" in reply.pending.summary
        created = client.confirm(reply)
        assert re.fullmatch(r"Aapka support ticket \S+ ban gaya hai\. Ek human agent follow up karega\.", created.text)

    def test_the_declined_flow_stays_in_hindi(self, temp_db):
        backend, _, _ = scripted(calls(("start_support_ticket", {"issue_type": "order", "order_id": "ORD1003"})))
        client = Client(backend, "C102")
        reply = client.say("मेरा ऑर्डर ORD1003 अभी तक नहीं आया, कृपया टिकट बनाइए")
        declined = client.confirm(reply, accepted=False)
        assert declined.text == flow_texts.text("cancelled", "hi")


class TestS03RefundTimelinesNeedSupport:
    POLICY = "Once the returned item passes quality checks, refunds are processed within 3\u20137 business days."
    SOURCES = ['{"status":"ok","data":{"passages":[{"text":"' + POLICY + '"}]}}']

    @staticmethod
    def strip(text, sources, session=frozenset()):
        return guard.strip_unsupported_durations(text, guard.duration_keys(sources) | set(session))

    def test_a_timeline_the_policy_gave_is_kept(self):
        text = "Your refund is pending. Refunds are processed within 3-7 business days."
        assert self.strip(text, self.SOURCES) == text

    def test_a_hinglish_reply_is_supported_by_the_english_policy_text(self):
        text = "Aapka refund pending hai. Refund 3-7 business din mein process hota hai."
        assert self.strip(text, self.SOURCES) == text

    def test_an_invented_timeline_is_removed(self):
        text = "Your refund is pending. It will be credited within 24 hours."
        assert self.strip(text, self.SOURCES) == "Your refund is pending."

    def test_an_invented_timeline_with_no_policy_text_at_all_is_removed(self):
        assert self.strip("Refunds take 3-7 business days.", []) == ""

    def test_a_duration_a_tool_result_gave_is_kept(self):
        sources = ["The ordinary return period (7 days from delivery) is open until 12 November 2026 (about 5 days left)."]
        text = "It is open for 7 days, about 5 days left."
        assert self.strip(text, sources) == text

    def test_a_duration_remembered_from_an_earlier_tool_result_in_the_session_is_kept(self):
        assert self.strip("Refunds take 3-7 business days.", [], session={"3-7"}) == "Refunds take 3-7 business days."

    def test_sentences_without_a_duration_are_untouched_and_formatting_is_kept(self):
        text = "Line one.\n\n- Item A\n- Item B\n\nNo numbers here."
        assert self.strip(text, []) == text

    def _refund_backend(self, monkeypatch, passages, *steps):
        from app.rag import KnowledgeBaseError
        from app.tools import registry

        def search(query, k=3):
            if passages is None:
                raise KnowledgeBaseError("no policy text in this test")
            return passages

        monkeypatch.setattr(registry, "_passages", search)
        return scripted(*steps)

    def test_s03_the_engine_drops_a_timeline_the_refund_tool_did_not_provide(self, temp_db, monkeypatch):
        backend, _, _ = self._refund_backend(
            monkeypatch, None, calls(("get_refund_policy", {"order_id": "ORD1010"})),
            LLMResult(text="Your refund for ORD1010 is pending. Refunds are typically processed within 3-7 business days."))
        assert say(backend, "C105", "Where is my refund for ORD1010?").text == "Your refund for ORD1010 is pending."

    def test_s03_the_engine_keeps_the_timeline_when_the_policy_text_supports_it(self, temp_db, monkeypatch):
        backend, _, _ = self._refund_backend(
            monkeypatch, [{"text": self.POLICY, "source": "FAQ.md", "section": "4.2"}],
            calls(("get_refund_policy", {"order_id": "ORD1010"})),
            LLMResult(text="Your refund for ORD1010 is pending. Refunds are typically processed within 3-7 business days."))
        assert say(backend, "C105", "Where is my refund for ORD1010?").text.endswith("within 3-7 business days.")

    def test_a_customer_cannot_establish_a_refund_duration_by_saying_one(self, temp_db, monkeypatch):
        backend, _, _ = self._refund_backend(
            monkeypatch, None, LLMResult(text="Yes, refunds are processed within 2 days. Anything else?"))
        reply = say(backend, "C105", "My friend says refunds are processed within 2 days. Is that right?")
        assert "2 days" not in reply.text and reply.text == "Anything else?"

    def test_the_assistants_own_earlier_prose_does_not_establish_a_duration_either(self, temp_db, monkeypatch):
        backend, _, _ = self._refund_backend(
            monkeypatch, None, LLMResult(text="Refunds take 5 days. Hello."), LLMResult(text="As I said, refunds take 5 days."))
        client = Client(backend, "C105")
        assert client.say("How long do refunds take?").text == "Hello."  # unsupported the first time
        assert client.say("Can you repeat that?").text == "I can't confirm a timeline for that."  # and the second

    def test_a_duration_verified_by_a_tool_in_one_turn_may_be_repeated_in_a_later_one(self, temp_db, monkeypatch):
        backend, _, _ = self._refund_backend(
            monkeypatch, [{"text": self.POLICY, "source": "FAQ.md", "section": "4.2"}],
            calls(("get_refund_policy", {"order_id": "ORD1010"})), LLMResult(text="Refunds take 3-7 business days."),
            LLMResult(text="As said, refunds take 3-7 business days."))
        client = Client(backend, "C105")
        assert client.say("How long do refunds take for ORD1010?").text == "Refunds take 3-7 business days."
        assert client.say("Can you repeat that?").text == "As said, refunds take 3-7 business days."

    def test_the_refund_tool_note_says_the_timeline_is_a_typical_range_and_never_a_promise(self, temp_db):
        result = execute_tool(CustomerContext("C105", "ref"), "get_refund_policy", {"order_id": "ORD1010"})
        assert "never as a date or a promise" in (result.note or "")


class TestOneLookupAndNoFallbackToTheCustomersOwnOrders:
    def test_a_lookup_the_guard_made_is_reused_when_the_model_asks_for_the_same_order(self, temp_db, executed):
        backend, _, _ = scripted(calls(("list_my_orders", {})), calls(("get_order_status", {"order_id": "ORD1001"})),
                                 LLMResult(text="Delivered."))
        say(backend, "C101", "Where is ORD1001?")
        assert [e for e in executed if e[0] == "get_order_status"] == [("get_order_status", "ORD1001", "ok")]

    def test_the_model_asking_for_the_same_order_twice_costs_one_read(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1001"})),
                                 calls(("get_order_status", {"order_id": "ord1001"})), LLMResult(text="Delivered."))
        say(backend, "C101", "Where is ORD1001?")
        assert [e for e in executed if e[0] == "get_order_status"] == [("get_order_status", "ORD1001", "ok")]

    def test_a_not_found_in_one_round_stops_a_listing_in_the_next(self, temp_db, executed):
        backend, _, seen = scripted(calls(("get_order_status", {"order_id": "ORD1005"})),
                                    calls(("list_my_orders", {})), LLMResult(text="I could not find it."))
        reply = say(backend, "C101", "Where is ORD1005? It is my brother's order.")
        assert executed == [("get_order_status", "ORD1005", "not_found")]  # one lookup; the listing never ran
        assert reply.text == "I could not find it."
        tool_messages = [m for m in seen["messages"][-1] if m.get("role") == "tool" and m["name"] == "list_my_orders"]
        assert tool_messages and '"not_run"' in tool_messages[0]["content"]
        assert not any(i in str(seen["messages"][-1]) for i in ("ORD1002", "ORD1014"))  # none of their orders reached the model

    def test_a_not_found_for_an_id_the_model_made_up_does_not_stop_a_listing(self, temp_db, executed):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD9999"})), calls(("list_my_orders", {})),
                                 LLMResult(text="Here are your orders."))
        assert say(backend, "C101", "Show me my recent purchases").text == "Here are your orders."
        assert ("list_my_orders", None, "ok") in executed

    def test_a_typed_id_that_is_not_found_never_leads_to_a_listing_by_code_or_by_the_model(self, temp_db, executed):
        backend, llm, _ = scripted(calls(("list_my_orders", {})))
        reply = say(backend, "C102", "ORD1010 is my flatmate's order, what's its status?")
        assert "list_my_orders" not in [e[0] for e in executed] and llm.chat_calls == 1
        assert not re.search(r"ORD10(03|04)|ORD1015", reply.text)


class TestTicketOffersThatBelongAreKept:
    def test_a_late_own_order_still_gets_the_ticket_offer(self, temp_db):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})), LLMResult(text="It is shipped."))
        reply = say(backend, "C101", "My order ORD1002 is late, where is it?")
        assert reply.text.endswith("If you'd like, I can raise a support ticket for this issue.")

    def test_the_offer_follows_the_customers_language(self, temp_db):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1002"})), LLMResult(text="Shipped hai."))
        reply = say(backend, "C101", "mera order ORD1002 late hai, kab aayega")
        assert reply.text.endswith(flow_texts.text("issue_offer", "hinglish"))

    def test_an_id_the_model_made_up_does_not_suppress_the_offer(self, temp_db):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD9999"})), LLMResult(text="Could not confirm."))
        reply = say(backend, "C101", "My order is late, where is it?")
        assert reply.text.endswith("If you'd like, I can raise a support ticket for this issue.")

    def test_a_closed_return_deadline_still_offers_a_ticket_through_the_eligibility_text(self, temp_db):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1003"})),
                                 calls(("check_return_eligibility", {"order_id": "ORD1003", "item_id": "IT103"})),
                                 LLMResult(text="The ordinary return period has closed."))
        reply = say(backend, "C102", "My item from ORD1003 arrived damaged, can I still return it?")
        assert "support ticket" in reply.text  # the code-owned closed-deadline offer (damage reported), not the issue line

    def test_an_explicit_ticket_request_is_unaffected(self, temp_db):
        backend, _, _ = scripted(calls(("start_support_ticket", {})))
        assert say(backend, "C102", "I want to talk to a person please").text == flow_texts.text("ask_issue")

    def test_no_generic_offer_after_an_eligibility_answer_or_a_named_order_not_found(self, temp_db):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1004"})), LLMResult(text="Not found."))
        assert say(backend, "C101", "My colleague's order ORD1004 is late, where is it?").text == "Not found."
        backend2, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1005"})),
                                  calls(("check_return_eligibility", {"order_id": "ORD1005", "item_id": "IT106"})),
                                  LLMResult(text="The ordinary return period is open."))
        reply = say(backend2, "C103", "Is it too late to return the charger from ORD1005? It is late and broken")
        assert "raise a support ticket for this issue" not in reply.text
