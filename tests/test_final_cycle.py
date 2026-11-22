"""Final correction cycle: refund-status wording (S02), the localised return journey, the review of order-ID format,
ambiguous multi-order selection and the refund-duration guard. Offline: scripted models and a temporary seeded database."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import app.backend  # noqa: E402,F401
from app.backend import Backend, Settings  # noqa: E402
from app.backend.types import ConfirmationDecision  # noqa: E402
from app.db import repository  # noqa: E402
from app.llm.base import LLMResult  # noqa: E402
from app.orchestrator import engine as engine_module  # noqa: E402
from app.orchestrator import guard  # noqa: E402
from app.tools import flow_texts  # noqa: E402
from app.tools.context import CustomerContext  # noqa: E402
from app.tools.registry import execute_tool  # noqa: E402
from tests.backend_helpers import Client, ScriptedLLM, calls  # noqa: E402

LANGUAGES = ("en", "hinglish", "hi")


def scripted(*steps):
    queue = list(steps)
    seen = {"messages": []}

    def chat(system, messages, tools):
        seen["messages"].append(messages)
        step = queue.pop(0) if queue else LLMResult(text="MODEL TEXT")
        return step(system, messages) if callable(step) else step

    llm = ScriptedLLM(chat=chat)
    llm.interprets_language = True
    return Backend(llm=llm, settings=Settings()), llm, seen


def say(backend, customer, text):
    return Client(backend, customer).say(text)


def amount(customer, order):
    result = execute_tool(CustomerContext(customer, "ref"), "get_order_status", {"order_id": order})
    return result.data["refund_amount"]


# ======================================================================================================================
# 1. S02: a pending refund is never turned into a promise
# ======================================================================================================================

class TestRefundStatusWording:
    @pytest.mark.parametrize("sentence", [
        "You will receive the refund in a few days.", "The refund will be credited to your account soon.",
        "Your refund of Rs 1,999 is going to arrive shortly.", "You should receive your money back soon.",
        "The refund will be refunded to your card.", "You'll get the refund once it is processed.",
        "Aapko ₹1,999 ka refund milne wala hai.", "Refund jald hi mil jayega.", "Aapka refund credit ho jayega.",
        "Paisa wapas milega.", "आपको रिफंड मिलेगा।",
        "रिफंड क्रेडिट हो जाएगा।"])
    def test_a_promise_of_the_refund_is_replaced(self, sentence):
        assert guard.reword_refund_claims(f"Status noted. {sentence} Thanks.", "REPLACED.", future=True) == \
            "Status noted. REPLACED. Thanks."

    @pytest.mark.parametrize("sentence", [
        "The refund has been credited to your account.", "Your refund is completed.", "The refund was already refunded.",
        "Refund credit ho gaya.", "Aapka refund mil gaya.", "रिफंड मिल गया।"])
    def test_a_claim_that_it_arrived_or_is_complete_is_replaced_even_when_no_promise_is_allowed_to_change(self, sentence):
        assert guard.reword_refund_claims(f"Hello. {sentence}", "STATUS.", future=False) == "Hello. STATUS."

    @pytest.mark.parametrize("sentence", [
        "Your refund of ₹1,999 is Pending.", "The refund status is Processed.", "I can't promise a refund.",
        "Refunds are typically processed within 3-7 business days.", "Your order will arrive tomorrow.",
        "Order kab aayega, main check karta hoon.", "The refund policy allows 7 days.", "Refund status \"Pending\" hai."])
    def test_a_plain_status_a_policy_statement_and_non_refund_sentences_are_untouched(self, sentence):
        text = f"Hello. {sentence}"
        assert guard.reword_refund_claims(text, "REPLACED.", future=True) == text

    def test_with_no_replacement_the_offending_sentence_is_dropped(self):
        assert guard.reword_refund_claims("A. You will receive the refund. B.", None, future=True) == "A. B."

    def test_s02_before_and_after_in_hinglish(self, temp_db):
        before = ('Order ID **ORD1010** ka status "Returned" hai, lekin iske liye koi return request file nahi hui hai. '
                  'Refund status "Pending" hai aur aapko ₹1,999 ka refund milne wala hai.\n\n'
                  'Agar aapko aur koi madad chahiye, toh bataiye!')
        backend, _, _ = scripted(calls(("get_my_returns", {}), ("get_order_status", {"order_id": "ORD1010"})),
                                 LLMResult(text=before))
        reply = say(backend, "C105", "ORD1010 ka return ho gaya kya?").text
        pending = flow_texts.text("refund_pending", "hinglish", amount=amount("C105", "ORD1010"))
        assert "milne wala" not in reply
        assert reply == ('Order ID **ORD1010** ka status "Returned" hai, lekin iske liye koi return request file nahi hui '
                         f'hai. {pending}\n\nAgar aapko aur koi madad chahiye, toh bataiye!')
        assert "Pending" in reply and "vaada nahi" in reply  # the status is said, and no promise is made

    @pytest.mark.parametrize("language,text", [
        ("en", "Your refund for ORD1010 is Pending. You will receive ₹1,999 in a few days."),
        ("hi", "रिफंड Pending है। आपको रिफंड "
               "मिलेगा।")])
    def test_a_pending_refund_is_never_turned_into_a_promise_in_any_language(self, temp_db, language, text):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1010"})), LLMResult(text=text))
        message = {"en": "Where is my refund for ORD1010?",
                   "hi": "मेरा ORD1010 का रिफंड कहाँ है"}[language]
        reply = say(backend, "C105", message).text
        assert flow_texts.text("refund_pending", language, amount=amount("C105", "ORD1010")) in reply
        assert "will receive" not in reply and "मिलेगा" not in reply

    def test_processed_is_not_said_to_have_arrived(self, temp_db):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1006"})),
                                 LLMResult(text="Your refund of ₹2,499 has been credited to your account."))
        reply = say(backend, "C103", "Where is my refund for ORD1006?").text
        assert reply == flow_texts.text("refund_processed", "en", amount=amount("C103", "ORD1006"))

    def test_processed_may_be_described_as_processed_with_the_banks_timing(self, temp_db):
        text = "The refund is Processed. It can take a few days to show in your account, depending on your bank."
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1006"})), LLMResult(text=text))
        assert say(backend, "C103", "Where is my refund for ORD1006?").text == text

    def test_failed_is_not_promised_and_offers_a_ticket(self, temp_db):
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1012"})),
                                 LLMResult(text="Do not worry, your refund will be sent soon."))
        reply = say(backend, "C106", "What happened to my refund for ORD1012?").text
        assert reply == flow_texts.text("refund_failed", "en", amount=amount("C106", "ORD1012"))
        assert "support ticket" in reply

    def test_no_refund_means_the_policy_can_be_stated_freely(self, temp_db):
        text = "You will receive a refund once the returned item passes quality checks."
        backend, _, _ = scripted(calls(("get_order_status", {"order_id": "ORD1001"})), LLMResult(text=text))
        assert say(backend, "C101", "Will I get a refund for ORD1001?").text == text  # Not Applicable: no status to contradict

    def test_without_a_tool_result_there_is_nothing_to_check_against(self, temp_db):
        text = "You will receive a refund once the item passes quality checks."
        backend, _, _ = scripted(LLMResult(text=text))
        assert say(backend, "C105", "How do refunds work?").text == text

    def test_the_tool_notes_say_what_each_status_means_and_that_completed_is_not_one(self, temp_db):
        for customer, order in (("C105", "ORD1010"), ("C103", "ORD1006"), ("C106", "ORD1012")):
            note = execute_tool(CustomerContext(customer, "ref"), "get_order_status", {"order_id": order}).note
            assert "Pending = a refund is recorded and has NOT been paid" in note
            assert "Completed is not a refund status" in note
        plain = execute_tool(CustomerContext("C101", "ref"), "get_order_status", {"order_id": "ORD1001"}).note
        assert "Refund status meanings" not in plain  # nothing to explain when there is no refund

    def test_the_refund_status_vocabulary_in_the_data_has_no_completed(self):
        schema = (Path(__file__).resolve().parents[1] / "app" / "db" / "schema.py").read_text(encoding="utf-8")
        assert "'Not Applicable', 'Pending', 'Processed', 'Failed'" in schema


# ======================================================================================================================
# 2. The return journey in English, Hinglish and Hindi
# ======================================================================================================================

MESSAGE = {
    "en": "I want to return the earbuds from ORD1001, they keep cutting out",
    "hinglish": "mujhe ORD1001 ke earbuds return karne hain, woh baar baar band ho jaate hain kyunki kharab hain",
    "hi": "मुझे ORD1001 के इयरबड्स रिटर्न "
          "करने हैं, वे बार-बार बंद "
          "हो जाते हैं क्योंकि खराब हैं",
}


def start_return(language, order="ORD1001", item="IT101"):
    backend, _, _ = scripted(calls(("start_return_request", {"order_id": order, "item_id": item})))
    client = Client(backend, "C101")
    return client, client.say(MESSAGE[language])


@pytest.mark.parametrize("language", LANGUAGES)
class TestReturnJourneyLocalised:
    def test_the_category_menu_and_its_labels(self, temp_db, language):
        _, choice = start_return(language)
        assert choice.status == "needs_choice"
        assert choice.text == flow_texts.text("category_question", language, name="Wireless Earbuds", item_id="IT101",
                                               order_id="ORD1001")
        assert [o.label for o in choice.choice.options] == [flow_texts.text(f"cat_{c}", language) for c in
                                                            ("damaged", "wrong_product", "standard")] + [
            flow_texts.text("cat_cancel", language)]

    def test_the_confirmation_card_and_the_submitted_message(self, temp_db, language):
        client, choice = start_return(language)
        card = client.pick(choice, "standard")
        assert card.status == "needs_confirmation" and card.text == flow_texts.text("return_card_lead", language)
        assert card.pending.summary == flow_texts.text(
            "return_card_summary", language, item_id="IT101", order_id="ORD1001",
            category=flow_texts.text("catname_standard", language), reason=MESSAGE[language])
        done = client.confirm(card)
        return_id = re.search(r"RET_\w+", done.text).group(0)
        assert done.text == flow_texts.text("return_submitted", language, return_id=return_id, item_id="IT101",
                                            order_id="ORD1001")

    def test_declining_the_card_and_choosing_never_mind_and_typing_cancel(self, temp_db, language):
        client, choice = start_return(language)
        card = client.pick(choice, "standard")
        assert client.confirm(card, accepted=False).text == flow_texts.text("cancelled", language)
        client2, choice2 = start_return(language)
        assert client2.pick(choice2, "cancel").text == flow_texts.text("category_cancel", language)
        client3, _ = start_return(language)
        assert client3.say("cancel").text == flow_texts.text("cancelled", language)

    def test_a_closed_reporting_window_is_explained_and_a_ticket_offered(self, temp_db, language):
        client, choice = start_return(language)
        reply = client.pick(choice, "damaged")  # ORD1001 was delivered 72 hours ago: the 48-hour window has closed
        assert reply.text == (flow_texts.text("cat_window_closed", language, hours=48, closes="2026-11-06",
                                              name=flow_texts.text("catname_damaged", language))
                              + flow_texts.text("ticket_offer_review", language))

    def test_reminders_while_a_choice_or_a_card_is_open(self, temp_db, language):
        client, choice = start_return(language)
        assert client.say("sure").text == flow_texts.text("remind_choice", language)
        # Revision 4.5 (shared improvement): a greeting is a read-only message; it is answered and the open choice stays.
        greeted = client.say("hello?")
        assert greeted.text == flow_texts.text("greeting", language) and greeted.choice is not None
        card = client.pick(choice, "standard")
        assert client.say("sure").text == flow_texts.text("remind_pending", language)
        greeted = client.say("hello?")
        assert greeted.text == flow_texts.text("greeting", language) and greeted.pending is not None
        assert card.pending is not None

    def test_a_repeated_or_unknown_confirmation_is_refused_in_the_customers_language(self, temp_db, language):
        client, choice = start_return(language)
        card = client.pick(choice, "standard")
        client.confirm(card)
        assert client.confirm(card).text == flow_texts.text("refuse_done", language)
        unknown = client.backend.handle_message(client.state, decision=ConfirmationDecision("no-such-action", True))
        assert unknown.text == flow_texts.text("refuse_unknown", language)

    def test_a_shipped_order_cannot_be_returned_and_says_why(self, temp_db, language):
        backend, _, _ = scripted(calls(("start_return_request", {"order_id": "ORD1002", "item_id": "IT102"})))
        reply = say(backend, "C101", MESSAGE[language].replace("ORD1001", "ORD1002").replace("earbuds", "charger")
                    .replace("इयरबड्स", "चार्जर"))
        assert reply.text == flow_texts.text("ineligible_status", language, status="Shipped")


@pytest.mark.parametrize("language", LANGUAGES)
class TestEveryEligibilityOutcomeIsLocalised:
    @pytest.mark.parametrize("verdict", [
        {"outcome": "order_not_found"}, {"outcome": "item_not_found"},
        {"outcome": "order_status_ineligible", "order_status": "Shipped"}, {"outcome": "order_status_ineligible"},
        {"outcome": "active_return_exists"}, {"outcome": "delivery_timestamp_missing"},
        {"outcome": "outside_return_window", "window_hours": 168, "window_closes_at": "2026-11-05T00:00:00Z"},
        {"outcome": "outside_return_window"}, {"outcome": "something_else"}])
    def test_each_verdict_has_a_sentence_in_each_language(self, language, verdict):
        plain = engine_module.Engine._ineligible_text(verdict, language)
        assert plain and "{" not in plain
        if language != "en":
            assert plain != engine_module.Engine._ineligible_text(verdict, "en")
        if verdict["outcome"] == "outside_return_window":  # the offer variant swaps the plain ticket sentence
            offer = engine_module.Engine._ineligible_text(verdict, language, offer=True)
            assert offer.endswith(flow_texts.text("ticket_offer_review", language))
            assert plain.endswith(flow_texts.text("ticket_plain", language))

    def test_reason_problems_are_localised(self, language):
        too_long = engine_module.Engine._reason_problem("x" * 1001, language=language)
        assert too_long == (flow_texts.text("reason_too_long", language, n=1001, max=1000)
                            + flow_texts.text("reason_notice", language))

    def test_the_which_item_question_is_localised(self, language):
        from app.tools.results import ToolResult
        feedback = ToolResult("invalid_input", "item_not_in_order", {"items": [{"item_id": "IT1", "product_name": "A"}]})
        assert engine_module.Engine._which_item_text(feedback, language) == flow_texts.text(
            "which_item", language, names="IT1 (A)")

    def test_the_refusal_reasons_are_localised(self, language):
        for reason in ("unknown", "expired", "claimed", "done", "cancelled", "cap_reached", "anything"):
            assert engine_module.Engine._refusal_text(reason, language)


class TestEnglishIsExactlyWhatItWasBefore:
    @pytest.mark.parametrize("verdict,expected", [
        ({"outcome": "order_not_found"}, "I couldn't find that order on your account."),
        ({"outcome": "item_not_found"}, "I couldn't find that item on that order."),
        ({"outcome": "order_status_ineligible", "order_status": "Shipped"},
         "Returns can only be requested for delivered orders. This order's status is Shipped."),
        ({"outcome": "order_status_ineligible"}, "Returns can only be requested for delivered orders."),
        ({"outcome": "active_return_exists"},
         "There is already an active return request for that item, so I can't start another."),
        ({"outcome": "delivery_timestamp_missing"},
         "I can't confirm the delivery date for this order, so I can't check the return window. I can raise a support "
         "ticket to record this, but I can't promise a reply or a review."),
        ({"outcome": "outside_return_window", "window_hours": 168, "window_closes_at": "2026-11-05T00:00:00Z"},
         "The 168-hour return window for this type of return closed on 2026-11-05, so I can't start a return. I can "
         "raise a support ticket to record this if you'd like. This chat can't connect you to a human agent, so I can't promise a reply."),
        ({"outcome": "outside_return_window"},
         "The return window for this type of return has closed, so I can't start a return. I can raise a support "
         "ticket to record this if you'd like. This chat can't connect you to a human agent, so I can't promise a reply."),
        ({"outcome": "x"}, "I can't start a return for that item.")])
    def test_ineligible_texts(self, verdict, expected):
        assert engine_module.Engine._ineligible_text(verdict) == expected

    def test_the_offer_variant_is_the_old_ticket_offer(self):
        verdict = {"outcome": "outside_return_window", "window_hours": 168, "window_closes_at": "2026-11-05T00:00:00Z"}
        assert engine_module.Engine._window_closed_offer_text(verdict).endswith(
            "I can help you raise a support ticket to record what happened. Would you like to do that? "
            "This chat can't connect you to a human agent, so I can't promise a review, a reply or any particular outcome.")

    def test_other_pinned_sentences(self):
        assert flow_texts.text("return_card_lead") == "This item is eligible for a return. Please confirm to submit the request."
        assert flow_texts.text("category_question", name="X", item_id="IT1", order_id="ORD1") == \
            "What best describes the reason for returning X (item IT1) from order ORD1?"
        assert flow_texts.text("return_submitted", return_id="R", item_id="I", order_id="O") == \
            "Your return request R has been submitted for item I on order O. Its status is 'requested'."
        assert flow_texts.text("cat_cancel") == "Never mind"
        assert engine_module.REASON_NOTICE.endswith("Type cancel to stop.")


class TestTheCatalogue:
    def test_one_catalogue_every_sentence_in_three_languages_with_the_same_placeholders(self):
        english = flow_texts.TEXTS["en"]
        assert len(english) >= 85
        for language in ("hinglish", "hi"):
            assert set(flow_texts.TEXTS[language]) == set(english)
            for key, sentence in english.items():
                theirs = flow_texts.TEXTS[language][key]
                assert set(re.findall(r"\{(\w+)\}", theirs)) == set(re.findall(r"\{(\w+)\}", sentence)) or key == "lead_limit", key
                if key != "catname_standard":  # "standard return" is the same word in English and Hinglish
                    assert theirs != sentence, key  # nothing was left untranslated

    def test_hindi_is_devanagari_and_hinglish_is_not(self):
        for key, sentence in flow_texts.TEXTS["hi"].items():
            assert re.search(r"[ऀ-ॿ]", sentence), key
        for key, sentence in flow_texts.TEXTS["hinglish"].items():
            assert not re.search(r"[ऀ-ॿ]", sentence), key

    def test_every_key_the_engine_asks_for_exists_and_none_is_left_unused(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "orchestrator" / "engine.py").read_text(encoding="utf-8")
        literal = set(re.findall(r'flow_texts\.text\(\s*"([a-z_]+)"', source))
        dynamic = ("cat_", "catname_", "problem_", "refuse_", "refund_", "which_order")
        keys = set(flow_texts.TEXTS["en"])
        assert literal <= keys, literal - keys
        used = literal | {k for k in keys if k.startswith(dynamic)} | {
            "ticket_offer_review", "ticket_plain", "outside_window_body", "outside_window_body_generic",
            "pending_plain", "pending_order", "reason_unusable", "ask_issue"}
        # the ternaries in the engine ("pending_order" if ... else "pending_plain") hide some literals from the pattern
        assert not (keys - used - set(re.findall(r'"([a-z_]+)"', source))), keys - used

    def test_the_draft_status_is_pinned_for_every_language_but_english(self):
        assert flow_texts.REVIEW_STATUS["en"] == "approved"
        assert flow_texts.REVIEW_STATUS["hinglish"].startswith("draft") and flow_texts.REVIEW_STATUS["hi"].startswith("draft")

    def test_terminology_is_consistent_across_the_catalogue(self):
        hinglish = " ".join(flow_texts.TEXTS["hinglish"].values())
        hindi = " ".join(flow_texts.TEXTS["hi"].values())
        assert "return" in hinglish and "ticket" in hinglish and "confirm" in hinglish
        assert "रिटर्न" in hindi and "टिकट" in hindi and "पुष्टि" in hindi
        # the same word for the same thing: Hindi never mixes in 'वापसी' for a return
        assert "वापसी" not in hindi


# ======================================================================================================================
# 3. Technical review
# ======================================================================================================================

class TestOrderIdFormat:
    def test_every_seeded_order_id_is_ord_and_four_digits_and_is_recognised(self, temp_db, raw):
        ids = [r[0] for r in raw.execute("SELECT order_id FROM orders ORDER BY order_id").fetchall()]
        assert len(ids) == 15 and all(re.fullmatch(r"ORD\d{4}", i) for i in ids)
        for order_id in ids:
            assert guard.order_ids_in(f"where is {order_id}?") == [order_id]

    def test_item_ids_are_not_mistaken_for_orders(self, temp_db, raw):
        assert guard.order_ids_in("IT101 and IT1005") == []

    @pytest.mark.parametrize("written,expected", [("ORD-1010", "ORD1010"), ("ord 1010", "ORD1010"), ("ORD_1010", "ORD1010"),
                                                  ("order ORD1010!", "ORD1010"), ("ord1010", "ORD1010")])
    def test_usual_ways_of_writing_an_order_id_are_normalised(self, written, expected):
        assert guard.order_ids_in(written) == [expected]

    @pytest.mark.parametrize("written", ["ORD 5", "ORD-12", "order 1010", "1010", "ORDER1010", "XORD1010"])
    def test_what_is_not_clearly_an_order_id_is_not_read_as_one(self, written):
        assert guard.order_ids_in(written) == []

    def test_a_dashed_id_is_looked_up_by_code_when_the_model_ignores_it(self, temp_db):
        backend, _, _ = scripted(LLMResult(text="x"))
        assert say(backend, "C101", "My friend's order ORD-1005 is late").text == flow_texts.text(
            "not_on_account", "en", ids="ORD1005")


@pytest.fixture
def two_delivered_earbud_orders(monkeypatch):
    """C101 gets a second DELIVERED order holding the same product as ORD1001, so 'my earbuds' fits two orders."""
    real_orders, real_items = repository.list_customer_orders, repository.get_order_items

    def orders(customer):
        extra = [{"order_id": "ORD1099", "status": "Delivered"}] if customer == "C101" else []
        return [*real_orders(customer), *extra]

    def items(customer, order_id):
        if order_id == "ORD1099":
            return [{"item_id": "IT199", "product_id": "P1001", "quantity": 1, "price": 1}]
        return real_items(customer, order_id)

    monkeypatch.setattr(repository, "list_customer_orders", orders)
    monkeypatch.setattr(repository, "get_order_items", items)


class TestAmbiguousOrdersAreNeverResolvedByGuessing:
    NO_ID = "I want to return my earbuds, they keep cutting out"

    def propose(self, order="ORD1001", item="IT101"):
        return calls(("start_return_request", {"order_id": order, "item_id": item}))

    def test_a_return_with_two_candidate_orders_and_no_id_asks_which_one(self, temp_db, two_delivered_earbud_orders):
        backend, _, _ = scripted(self.propose())
        client = Client(backend, "C101")
        reply = client.say(self.NO_ID)
        assert reply.status == "ok" and reply.choice is None and reply.pending is None
        assert reply.text == flow_texts.text("which_order_for_item", "en", product="Wireless Earbuds",
                                              orders="ORD1001 (Delivered), ORD1099 (Delivered)")
        assert not re.search(r"₹|November|Total", reply.text)  # IDs and statuses only

    def test_the_question_is_in_the_customers_language(self, temp_db, two_delivered_earbud_orders):
        backend, _, _ = scripted(self.propose())
        reply = say(backend, "C101", "mujhe apne earbuds return karne hain, woh baar baar band ho jaate hain kyunki kharab hain")
        assert reply.text == flow_texts.text("which_order_for_item", "hinglish", product="Wireless Earbuds",
                                              orders="ORD1001 (Delivered), ORD1099 (Delivered)")

    def test_a_typed_order_id_settles_it(self, temp_db, two_delivered_earbud_orders):
        backend, _, _ = scripted(self.propose())
        assert say(backend, "C101", MESSAGE["en"]).status == "needs_choice"

    def test_an_id_typed_earlier_in_the_conversation_settles_it(self, temp_db, two_delivered_earbud_orders):
        backend, _, _ = scripted(LLMResult(text="Delivered."), self.propose())
        client = Client(backend, "C101")
        client.say("Where is ORD1001?")
        assert client.say(self.NO_ID).status == "needs_choice"

    def test_an_eligibility_check_after_an_id_typed_earlier_is_unchanged(self, temp_db, two_delivered_earbud_orders):
        backend, _, seen = scripted(LLMResult(text="Noted."),
                                    calls(("check_return_eligibility", {"order_id": "ORD1001", "item_id": "IT101"})),
                                    LLMResult(text="Open."))
        client = Client(backend, "C101")
        client.say("I am asking about ORD1001")
        reply = client.say("Can I still return my earbuds?").text
        assert reply.startswith("Open.") and "Which order" not in reply  # answered (the engine adds its own limits sentence)

    def test_a_ticket_after_an_id_typed_earlier_keeps_that_order(self, temp_db):
        backend, _, _ = scripted(LLMResult(text="Noted."),
                                 calls(("start_support_ticket", {"issue_type": "order", "order_id": "ORD1002"})))
        client = Client(backend, "C101")
        client.say("ORD1002 is the order I mean")
        reply = client.say("my charger stopped working, please raise a ticket")
        assert reply.status == "needs_confirmation" and "ORD1002" in reply.pending.summary

    def test_a_return_with_an_id_typed_earlier_proceeds(self, temp_db, two_delivered_earbud_orders):
        backend, _, _ = scripted(LLMResult(text="Noted."), self.propose())
        client = Client(backend, "C101")
        client.say("ORD1001 is the order I mean")
        assert client.say(self.NO_ID).status == "needs_choice"

    def test_a_single_matching_order_needs_no_question(self, temp_db):
        backend, _, _ = scripted(self.propose())
        assert say(backend, "C101", self.NO_ID).status == "needs_choice"  # only ORD1001 holds earbuds

    def test_an_order_that_is_not_delivered_does_not_make_it_ambiguous(self, temp_db):
        backend, _, _ = scripted(calls(("start_return_request", {"order_id": "ORD1002", "item_id": "IT102"})))
        reply = say(backend, "C101", "I want to return my charger, it stopped working")  # ORD1002 and ORD1014: none delivered
        assert "delivered orders" in reply.text

    def test_an_eligibility_answer_for_a_guessed_order_becomes_a_question_the_model_never_composes(
            self, temp_db, two_delivered_earbud_orders):
        backend, llm, seen = scripted(calls(("check_return_eligibility", {"order_id": "ORD1001", "item_id": "IT101"})),
                                      LLMResult(text="SHOULD NEVER BE USED"))
        reply = say(backend, "C101", "Can I still return my earbuds?")
        assert reply.text == flow_texts.text("which_order_for_item", "en", product="Wireless Earbuds",
                                              orders="ORD1001 (Delivered), ORD1099 (Delivered)")
        assert llm.chat_calls == 1  # the model is not called again: it never sees the check's result
        assert not any(m.get("role") == "tool" for m in seen["messages"][-1])
        assert not re.search(r"UTC|ordinary|deadline|\d{4}", reply.text.replace("ORD1001", "").replace("ORD1099", ""))

    def test_an_eligibility_answer_with_a_typed_id_is_unchanged(self, temp_db, two_delivered_earbud_orders):
        backend, _, seen = scripted(calls(("check_return_eligibility", {"order_id": "ORD1001", "item_id": "IT101"})),
                                    LLMResult(text="Open."))
        say(backend, "C101", "Can I still return my earbuds from ORD1001?")
        assert '"which_order"' not in [m for m in seen["messages"][-1] if m.get("role") == "tool"][0]["content"]

    def test_a_ticket_is_not_linked_to_an_order_the_model_picked_among_look_alikes(self, temp_db):
        # ORD1002 and ORD1014 both hold the charger: "my charger" does not say which
        backend, _, _ = scripted(calls(("start_support_ticket", {"issue_type": "order", "order_id": "ORD1002"})))
        reply = say(backend, "C101", "my charger stopped working, please raise a ticket")
        assert reply.status == "needs_confirmation" and "ORD1002" not in reply.pending.summary

    def test_a_ticket_keeps_an_order_the_customer_named(self, temp_db):
        backend, _, _ = scripted(calls(("start_support_ticket", {"issue_type": "order", "order_id": "ORD1002"})))
        reply = say(backend, "C101", "ORD1002 stopped working, please raise a ticket")
        assert "ORD1002" in reply.pending.summary

    def test_a_ticket_keeps_an_order_whose_product_is_unique(self, temp_db):
        backend, _, _ = scripted(calls(("start_support_ticket", {"issue_type": "order", "order_id": "ORD1001"})))
        reply = say(backend, "C101", "my earbuds stopped working, please raise a ticket")
        assert "ORD1001" in reply.pending.summary  # only one order holds earbuds: the product name is enough


class TestTheRefundDurationGuard:
    SOURCE = ["The ordinary return period (7 days from delivery) is open (about 5 days left). The deadline (48 hours from "
              "delivery) closed. Once the returned item passes quality checks, refunds are processed within 3–7 "
              "business days depending on your payment method and bank."]

    def strip(self, text, session=()):
        return guard.strip_unsupported_durations(text, guard.duration_keys(self.SOURCE) | set(session))

    @pytest.mark.parametrize("text", [
        "Refunds are processed within 3-7 business days.", "Refunds take 3–7 business days.",
        "Refunds take 3 - 7 business days.", "Refunds take three to seven business days.",
        "You have 7 days from delivery.", "The reporting window was 48 hours.", "About 5 days left.",
        "Refund 3-7 business din mein process hota hai.", "Return 7 din ke andar karein.",
        "रिफंड 3-7 दिन में होता है।",
        "No numbers at all.", "Order ORD1010 for ₹1,999 on 4 November 2026."])
    def test_supported_timelines_and_sentences_without_one_are_kept(self, text):
        assert self.strip(text) == text

    @pytest.mark.parametrize("text", [
        "It will be credited within 24 hours.", "Refunds take 2 days.", "Refunds take 10-14 business days.",
        "Refunds take one to two days.", "Refund 30 din mein aayega.", "Refund 2 hafte mein hota hai.",
        "रिफंड 10 दिन में होता है।",
        "It usually takes a week.", "Expect it within 4 working days."])
    def test_an_unsupported_timeline_is_removed(self, text):
        assert self.strip(f"Status noted. {text} Thanks.") == "Status noted. Thanks."

    def test_the_session_remembers_what_a_tool_verified(self):
        assert self.strip("It takes 2 days.", session={"2"}) == "It takes 2 days."

    def test_the_customers_own_words_never_count(self, temp_db):
        backend, _, _ = scripted(LLMResult(text="Yes, refunds take 10 days. Anything else?"))
        assert say(backend, "C105", "I heard refunds take 10 days, is that right?").text == "Anything else?"
