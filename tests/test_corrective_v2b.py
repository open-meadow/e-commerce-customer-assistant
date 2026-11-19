"""Corrective v2, second round: what Evaluations A and B showed, and the offline checks for each fix.

Evidence (live, gpt-4o-mini): a named order said to belong to someone else (N01, N02, H10, C027) was refused without the
lookup; an English message after Hinglish was answered in Hinglish with an English sentence added by code (L01 turn 2, 5 of
5 runs) and Hinglish questions sometimes got English (S02, M12 turn 1); a general policy question ("how long do I get to send
something back") ran an eligibility check on an invented order (C020); an angry refund demand was answered with "would you
like me to check?" (C028). Offline: scripted models only. No network. These tests show the instructions and the plumbing are
in place; whether the model follows them is measured live, not here.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import app.backend  # noqa: E402,F401
from app.backend import Backend, Settings  # noqa: E402
from app.llm.base import LLMResult  # noqa: E402
from app.orchestrator import engine as engine_module  # noqa: E402
from app.orchestrator import guard  # noqa: E402
from tests.backend_helpers import Client, ScriptedLLM  # noqa: E402

PROMPT = engine_module.SYSTEM_PROMPT


class TestDecisiveLanguage:
    @pytest.mark.parametrize("text,expected", [
        ("Sorry, I meant the monitor. Can I still return that one?", "en"),
        ("Can I still return my earbuds from ORD1001?", "en"),
        ("ORD1010 ka return ho gaya kya?", "hinglish"),
        ("kya main ORD1005 ka charger abhi bhi return kar sakta hu?", "hinglish"),
        ("\u092e\u0947\u0930\u093e \u0911\u0930\u094d\u0921\u0930 \u0915\u092c \u0906\u090f\u0917\u093e", "hi"),
        ("ok", None), ("ji haan", None), ("ORD1007", None), ("mera order", None),
    ])
    def test_the_message_settles_its_language_or_nothing(self, text, expected):
        assert guard.decisive_language(text) == expected

    @pytest.mark.parametrize("text,previous", [("ok", "hinglish"), ("ORD1007", "hi"), ("ji haan", "en"),
                                               ("Sorry, I meant the monitor. Can I still return that one?", "hinglish"),
                                               ("ORD1010 ka return ho gaya kya?", "en")])
    def test_customer_language_is_unchanged_by_the_refactor(self, text, previous):
        decided = guard.decisive_language(text)
        assert guard.customer_language(text, previous) == (decided if decided else previous)


class TestTurnLanguageLine:
    def test_an_english_message_after_hinglish_adds_the_english_line(self):
        system = engine_module.system_prompt_for_turn("Sorry, I meant the monitor. Can I still return that one?")
        assert system.startswith(PROMPT) and system.endswith("whatever language earlier messages or tool results used.")
        assert "THIS TURN. The customer's latest message is in English." in system

    def test_hinglish_and_hindi_get_their_own_line(self):
        assert "in Hinglish. Reply in Hinglish" in engine_module.system_prompt_for_turn("ORD1010 ka return ho gaya kya?")
        assert "Reply in Hindi in Devanagari" in engine_module.system_prompt_for_turn(
            "\u092e\u0947\u0930\u093e \u0911\u0930\u094d\u0921\u0930 \u0915\u092c \u0906\u090f\u0917\u093e")

    @pytest.mark.parametrize("text", ["ok", "ji haan", "ORD1007", "mera order"])
    def test_an_ambiguous_message_adds_nothing(self, text):
        assert engine_module.system_prompt_for_turn(text) == PROMPT

    def test_the_prompt_the_model_receives_carries_the_line_for_that_turn_only(self, temp_db):
        seen = []

        def chat(system, messages, tools):
            seen.append(system)
            return LLMResult(text="ok")

        client = Client(Backend(llm=ScriptedLLM(chat=chat), settings=Settings()), "C103")
        client.say("kya main ORD1005 ka charger abhi bhi return kar sakta hu?")
        client.say("Sorry, I meant the monitor. Can I still return that one?")
        client.say("ji haan")
        assert "Reply in Hinglish" in seen[0] and "Reply in English only" in seen[1] and seen[2] == PROMPT
        assert seen[0].startswith(PROMPT) and seen[1].startswith(PROMPT)  # the standing prompt itself is untouched

    def test_the_standing_prompt_is_unchanged_by_the_per_turn_line(self):
        engine_module.system_prompt_for_turn("Can I still return my earbuds from ORD1001?")
        assert "THIS TURN" not in PROMPT


class TestPromptRound2:
    def test_a_named_order_is_looked_up_even_when_said_to_belong_to_someone_else(self):
        assert "even when they say it belongs to someone else (a friend, a colleague, a family member)" in PROMPT
        assert PROMPT.index("always call get_order_status for it") < PROMPT.index("The one exception to listing")

    def test_the_decline_exception_is_limited_to_requests_with_no_order_id(self):
        assert "with no order ID (by a customer ID, a name, or 'every customer')" in PROMPT

    def test_a_general_policy_question_uses_search_policies_not_an_eligibility_check(self):
        assert "Use it only when they name or clearly mean a particular order or item" in PROMPT
        assert "('how long do I get to send something back?') call search_policies instead" in PROMPT
        assert "never guess it" in PROMPT  # the item_id rule is kept

    def test_an_angry_refund_demand_for_a_named_order_is_looked_up_not_questioned(self):
        assert "if they demand a refund for a named order, look it up with get_refund_policy at once" in PROMPT

    def test_the_prompt_stays_proportionate(self):
        assert len(PROMPT) < 7000
