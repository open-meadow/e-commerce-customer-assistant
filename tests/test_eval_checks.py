"""Phase 1 (trustworthy baseline), step B: the evaluation's checks measure what their names say.

Each new or redefined check is fed a known-good and a known-bad reply. No model, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import real_eval as ev  # noqa: E402
import run_eval  # noqa: E402
from app.llm.rule_based import RuleBasedLLM  # noqa: E402

ZERO = {"return_requests": 0, "support_tickets": 0}
WROTE = {"return_requests": 1, "support_tickets": 0}


@pytest.fixture(scope="module")
def truth():
    with ev.environment(RuleBasedLLM):
        yield ev.Truth.load()


def case(**overrides):
    base = {"id": "T1", "query": "Where is ORD1001?", "intent": "order_status", "category": "order_status",
            "example_expected_action": "x", "notes": "x", "customer_id": "C101",
            "expected_behavior": "answer", "expected_tool": "get_order_status"}
    base.update(overrides)
    return base


def reply(text="ok", status="ok", pending=None, choice=None, error_code=None):
    return SimpleNamespace(text=text, status=status, error_code=error_code, pending=pending, choice=choice,
                           citations=())


def obs(proposed=(), executed=(), chat_calls=1):
    return {"proposed": [{"name": n, "arguments": {}} for n in proposed], "executed": list(executed),
            "chat_calls": chat_calls, "complete_calls": 0}


def score(c, r, o=None, before=ZERO, after=ZERO, truth=None):
    return ev.score_case(c, r, o or obs(), before, after, truth)


# ---------------------------------------------------------------------------
# Expected tools may be alternatives
# ---------------------------------------------------------------------------

class TestExpectedToolAlternatives:
    def refund(self):
        return case(query="Refund kitne din mein aata hai?", expected_tool="get_refund_policy|search_policies",
                    category="policy")

    @pytest.mark.parametrize("proposed", ["get_refund_policy", "search_policies"])
    def test_either_alternative_passes(self, truth, proposed):
        assert score(self.refund(), reply(), obs([proposed]), truth=truth)["tool_selection"] == ev.PASS

    def test_another_tool_fails(self, truth):
        assert score(self.refund(), reply(), obs(["lookup_product"]), truth=truth)["tool_selection"] == ev.FAIL

    def test_the_loader_accepts_alternatives_and_rejects_unknown_ones(self, tmp_path):
        header = ",".join(run_eval._REQUIRED_COLUMNS if hasattr(run_eval, "_REQUIRED_COLUMNS") else [
            "id", "query", "intent", "category", "example_expected_action", "notes", "customer_id",
            "expected_behavior", "expected_tool"])
        good = tmp_path / "good.csv"
        good.write_text(header + "\nQ1,why,policy,policy,x,x,C101,answer,get_refund_policy|search_policies\n",
                        encoding="utf-8")
        assert run_eval.load_cases(good, {"C101"})[0]["expected_tool"] == "get_refund_policy|search_policies"
        bad = tmp_path / "bad.csv"
        bad.write_text(header + "\nQ1,why,policy,policy,x,x,C101,answer,get_refund_policy|teleport\n", encoding="utf-8")
        with pytest.raises(ValueError, match="unknown expected tool"):
            run_eval.load_cases(bad, {"C101"})


# ---------------------------------------------------------------------------
# Returns: progress, not a one-turn confirmation
# ---------------------------------------------------------------------------

def return_case():
    return case(query="The earbuds in ORD1001 keep cutting out. Send them back.", intent="standard_return",
                category="indirect", expected_behavior="require_confirmation", expected_tool="start_return_request")


MENU = SimpleNamespace(options=[SimpleNamespace(option_id="standard")])
CARD = SimpleNamespace(kind="return_request", action_id="a1")


class TestReturnFlowProgress:
    def test_reaching_the_category_menu_is_progress(self, truth):
        out = score(return_case(), reply(status="needs_choice", choice=MENU), obs(["start_return_request"]), truth=truth)
        assert out["return_flow_progress"] == ev.PASS and out["confirmation_gate"] == ev.NA

    def test_reaching_the_card_is_progress(self, truth):
        out = score(return_case(), reply(status="needs_confirmation", pending=CARD), obs(["start_return_request"]),
                    truth=truth)
        assert out["return_flow_progress"] == ev.PASS

    def test_asking_for_the_reason_is_progress(self, truth):
        text = "Could you tell me briefly, in your own words, why you want to return this item?"
        assert score(return_case(), reply(text), obs(["start_return_request"]), truth=truth)[
            "return_flow_progress"] == ev.PASS

    def test_a_guessed_item_that_ends_the_turn_is_not_progress(self, truth):
        out = score(return_case(), reply("I couldn't find that item on that order."), obs(["start_return_request"]),
                    truth=truth)
        assert out["return_flow_progress"] == ev.FAIL

    def test_a_write_is_never_progress(self, truth):
        out = score(return_case(), reply(status="needs_choice", choice=MENU), obs(["start_return_request"]),
                    ZERO, WROTE, truth)
        assert out["return_flow_progress"] == ev.FAIL

    def test_no_attempt_at_all_is_not_progress(self, truth):
        assert score(return_case(), reply("Sure, tell me more."), obs([]), truth=truth)["return_flow_progress"] == ev.FAIL

    def test_the_strict_gate_still_applies_to_tickets(self, truth):
        ticket = case(expected_behavior="require_confirmation", expected_tool="start_support_ticket",
                      category="escalation")
        card = SimpleNamespace(kind="support_ticket", action_id="a1")
        ok = score(ticket, reply(status="needs_confirmation", pending=card), obs(["start_support_ticket"]), truth=truth)
        assert ok["confirmation_gate"] == ev.PASS and ok["return_flow_progress"] == ev.NA
        bad = score(ticket, reply("Done."), obs(["start_support_ticket"]), truth=truth)
        assert bad["confirmation_gate"] == ev.FAIL


# ---------------------------------------------------------------------------
# Language-neutral facts
# ---------------------------------------------------------------------------

class TestOwnedFactsAreLanguageNeutral:
    @pytest.mark.parametrize("text", ["Order ORD1001: Delivered.", "आपका ऑर्डर ORD1001 डिलिवर हो चुका है।",
                                      "ORD1001 deliver ho chuka hai", "ORD1001 delivered ho gaya hai"])
    def test_the_stored_status_in_any_supported_language_passes(self, truth, text):
        assert score(case(), reply(text), truth=truth)["owned_fact_grounding"] == ev.PASS

    @pytest.mark.parametrize("text", ["ORD1001 abhi shipped hai", "ORD1001 is on its way", "ORD1001 अभी रास्ते में है"])
    def test_a_wrong_or_missing_status_fails(self, truth, text):
        assert score(case(), reply(text), truth=truth)["owned_fact_grounding"] == ev.FAIL

    def list_case(self, intent="order_status"):
        return case(query="Bhai mera order abhi tak nahi aaya.", intent=intent, expected_tool="list_my_orders")

    def test_every_order_still_on_its_way_must_be_mentioned(self, truth):
        ok = score(self.list_case(), reply("Aapke do orders abhi raaste mein hain: ORD1002 aur ORD1014."), truth=truth)
        assert ok["owned_fact_grounding"] == ev.PASS  # the delivered order ORD1001 is not required
        bad = score(self.list_case(), reply("ORD1002 abhi shipped hai."), truth=truth)
        assert bad["owned_fact_grounding"] == ev.FAIL  # ORD1014 is silently dropped

    def test_a_plain_list_request_needs_every_order(self, truth):
        partial = score(self.list_case("list_orders"), reply("ORD1002 and ORD1014"), truth=truth)
        full = score(self.list_case("list_orders"), reply("ORD1001, ORD1002, ORD1014"), truth=truth)
        assert partial["owned_fact_grounding"] == ev.FAIL and full["owned_fact_grounding"] == ev.PASS


# ---------------------------------------------------------------------------
# Reply language
# ---------------------------------------------------------------------------

class TestLanguage:
    @pytest.mark.parametrize("text, language", [
        ("Where is my order?", "en"), ("Where the hell is my order? It's been ages.", "en"),
        ("Bhai mera order abhi tak nahi aaya.", "hinglish"), ("Refund kitne din mein aata hai?", "hinglish"),
        ("मेरा ऑर्डर ORD1013 कहाँ है?", "hi"), ("ORD1002 status", "en"), ("Order main hai", "en"),
        ("Order ID **ORD1013** ka total amount **₹2,499** tha.", "hinglish"),
        ("Your order was shipped. The total was ₹2,499 and the ETA is Monday.", "en"),
    ])
    def test_detection(self, text, language):
        assert ev.detect_language(text) == language

    def hinglish_case(self):
        return case(query="Bhai mera order abhi tak nahi aaya.", expected_tool="list_my_orders")

    def test_an_english_customer_must_get_english(self, truth):
        english = case(query="Where is my order ORD1001?")
        assert score(english, reply("Your order ORD1001 is Delivered."), truth=truth)["language_match"] == ev.PASS
        assert score(english, reply("Aapka order ORD1001 deliver ho chuka hai, aapko aur kuch chahiye?"),
                     truth=truth)["language_match"] == ev.FAIL

    def test_a_hinglish_customer_gets_hinglish_not_english(self, truth):
        assert score(self.hinglish_case(), reply("Aapke order ORD1002 abhi shipped hai, aur ORD1014 processing mein hai."),
                     truth=truth)["language_match"] == ev.PASS
        assert score(self.hinglish_case(), reply("Your order ORD1002 is shipped."),
                     truth=truth)["language_match"] == ev.FAIL

    def test_a_devanagari_customer_gets_devanagari_or_hinglish(self, truth):
        hindi = case(query="मेरा ऑर्डर ORD1001 कहाँ है?")
        assert score(hindi, reply("आपका ऑर्डर ORD1001 डिलिवर हो चुका है।"), truth=truth)["language_match"] == ev.PASS

    def test_menus_and_cards_are_not_judged(self, truth):
        out = score(self.hinglish_case(), reply("What best describes the reason?", status="needs_choice", choice=MENU),
                    truth=truth)
        assert out["language_match"] == ev.NA


# ---------------------------------------------------------------------------
# Unnecessary questions
# ---------------------------------------------------------------------------

class TestUnnecessaryQuestions:
    def cancel(self):
        return case(query="Just cancel ORD1014, I don't want it anymore.", category="unsupported",
                    expected_behavior="unsupported_action", expected_tool="none")

    def test_asking_what_the_issue_is_after_the_customer_stated_it_fails(self, truth):
        out = score(self.cancel(), reply("Happy to raise a ticket. What is the issue you'd like it to be about? "
                                         "Please include the order ID if there is one."), obs(["start_support_ticket"]),
                    truth=truth)
        assert out["unnecessary_question"] == ev.FAIL

    def test_a_direct_answer_passes(self, truth):
        out = score(self.cancel(), reply("I can't cancel orders myself. I can raise a ticket for ORD1014 so an agent "
                                         "can look into it."), truth=truth)
        assert out["unnecessary_question"] == ev.PASS

    def test_a_context_free_request_for_a_human_may_ask_what_it_is_about(self, truth):
        human = case(query="kisi insaan se baat karao", category="human_request", expected_behavior="offer_escalation",
                     expected_tool="start_support_ticket")
        assert score(human, reply("Happy to raise a ticket. What is the issue?"), truth=truth)[
            "unnecessary_question"] == ev.NA

    def test_asking_for_an_order_id_without_looking_is_unnecessary(self, truth):
        ask = case(query="Bhai mera order abhi tak nahi aaya.", expected_tool="list_my_orders")
        asked = score(ask, reply("Could you share the order ID so I can check?"), obs([]), truth=truth)
        looked = score(ask, reply("ORD1002 abhi shipped hai. Kisi aur order ke baare mein poochna hai?"),
                       obs(["list_my_orders"]), truth=truth)
        assert asked["unnecessary_question"] == ev.FAIL and looked["unnecessary_question"] == ev.PASS


# ---------------------------------------------------------------------------
# Claims the tool data does not support
# ---------------------------------------------------------------------------

class TestClaimSupport:
    def refund(self):
        return case(query="Damn it, give me my money back for ORD1001", expected_tool="get_refund_policy")

    @pytest.mark.parametrize("text", [
        "Aapka item return ke liye eligible nahi hai.", "Your item is not eligible for return.",
        "Your refund will be processed within a day.", "You will get your money back tomorrow.",
        "Refund approve ho gaya hai."])
    def test_unsupported_eligibility_or_refund_claims_fail(self, truth, text):
        assert score(self.refund(), reply(text), obs(["get_refund_policy"]), truth=truth)["claim_support"] == ev.FAIL

    @pytest.mark.parametrize("text", [
        "Refunds usually take 3 to 7 business days after the returned item passes the quality check.",
        "Is order ka refund status 'Not Applicable' hai.", "I can raise a support ticket if you want."])
    def test_ordinary_statements_pass(self, truth, text):
        assert score(self.refund(), reply(text), obs(["get_refund_policy"]), truth=truth)["claim_support"] == ev.PASS

    def test_a_claim_the_tool_data_states_passes(self, truth):
        executed = [{"name": "search_policies", "status": "ok", "code": "passages",
                     "data": {"passages": [{"text": "Only unused items are eligible for return."}]}}]
        out = score(self.refund(), reply("Only unused items are eligible for return."),
                    obs(["search_policies"], executed), truth=truth)
        assert out["claim_support"] == ev.PASS

    def test_backend_generated_return_text_is_not_judged(self, truth):
        out = score(return_case(), reply("This item is eligible for a return. Please confirm to submit the request.",
                                         status="needs_confirmation", pending=CARD), obs(["start_return_request"]),
                    truth=truth)
        assert out["claim_support"] == ev.NA


# ---------------------------------------------------------------------------
# Three separate scorecards
# ---------------------------------------------------------------------------

class TestScorecards:
    def test_every_check_has_exactly_one_dimension(self):
        dims = {meta["dimension"] for meta in ev.CHECKS.values()}
        assert dims <= {"technical", "conversational", "experience", "mechanical"}

    def test_each_scorecard_adds_up_from_its_own_checks(self):
        results = [{"case_id": "A", "category": "x", "writes": False,
                    "checks": {**{n: ev.NA for n in ev.CHECKS}, "no_unconfirmed_write": ev.PASS, "tool_selection": ev.FAIL,
                               "language_match": ev.PASS, "unnecessary_question": ev.FAIL}}]
        card = ev.summarize(results, [])["scorecards"]
        assert card["technical"]["passed"] == 1 and card["technical"]["applicable"] == 1
        assert card["conversational"]["passed"] == 1 and card["conversational"]["applicable"] == 2
        assert card["experience"]["automated"]["failed"] == 1
        assert card["experience"]["hand_rubric"]["status"] == "not_assessed"

    def test_dimensions_are_never_blended_into_one_number(self):
        card = ev.summarize([], [])["scorecards"]
        assert "overall" not in card and set(card) == {"technical", "conversational", "experience"}

    def test_a_report_without_the_new_checks_still_compares(self):
        old = {"summary": {"checks": {"scope_guard": {"rate": 1.0, "failed_case_ids": []}}}, "results": [],
               "llm": {"provider": "x", "model": None}}
        comparison = ev.compare_reports(old, old)
        assert comparison["checks"]["scope_guard"]["first_rate"] == 1.0
        assert comparison["checks"]["language_match"]["first_rate"] is None


# ---------------------------------------------------------------------------
# Backend-generated menus and cards are not model text
# ---------------------------------------------------------------------------

class TestGroundedIdentifiersJudgeOnlyModelText:
    def test_a_category_menu_naming_the_item_id_is_not_judged(self, truth):
        menu_reply = reply("What best describes the reason for returning 65W GaN Charger (item IT106) from order "
                           "ORD1005?", status="needs_choice", choice=MENU)
        out = score(return_case(), menu_reply, obs(["start_return_request"]), truth=truth)
        assert out["grounded_identifiers"] == ev.NA

    def test_a_confirmation_card_is_not_judged(self, truth):
        card_reply = reply("Return item IT101 from order ORD1001.", status="needs_confirmation", pending=CARD)
        assert score(return_case(), card_reply, obs(["start_return_request"]), truth=truth)[
            "grounded_identifiers"] == ev.NA

    def test_an_invented_id_in_plain_model_text_still_fails(self, truth):
        out = score(case(), reply("Your order ORD7777 is Delivered."), obs(["get_order_status"]), truth=truth)
        assert out["grounded_identifiers"] == ev.FAIL

    def test_an_id_the_customer_typed_still_passes(self, truth):
        assert score(case(), reply("ORD1001 is Delivered."), obs(["get_order_status"]), truth=truth)[
            "grounded_identifiers"] == ev.PASS
