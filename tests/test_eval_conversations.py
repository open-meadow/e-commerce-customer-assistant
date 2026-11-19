"""Phase 1, step C: multi-turn conversations measure context, which single-turn cases cannot.

The runner and its expectations are tested with scripted models (no network).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import real_eval as ev  # noqa: E402
from app.llm.base import LLMResult, ToolCall  # noqa: E402
from app.llm.rule_based import RuleBasedLLM  # noqa: E402
from backend_helpers import ScriptedLLM  # noqa: E402

SHIPPED = Path(__file__).resolve().parents[1] / "app" / "db" / "data" / "eval" / "conversations.json"


def write(tmp_path, data):
    path = tmp_path / "conv.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def convo(turns, cid="C101", ident="X1", category="context_followup"):
    return {"id": ident, "customer_id": cid, "category": category, "note": "t", "turns": turns}


class Lookup(ScriptedLLM):
    """Answers every message with a get_order_status call for ORD1002, then plain text from the tool result."""

    name = "lookup"

    def __init__(self, final="ORD1002 is Shipped."):
        super().__init__(chat=self._chat)
        self.final = final

    def _chat(self, system, messages, tools):
        if messages[-1]["role"] == "tool":
            return LLMResult(text=self.final)
        return LLMResult(tool_calls=(ToolCall("get_order_status", {"order_id": "ORD1002"}, "c1"),))


class TestLoader:
    def test_the_shipped_file_loads_and_covers_every_customer(self):
        convos = ev.load_conversations(SHIPPED, {f"C10{i}" for i in range(1, 7)})
        assert len(convos) >= 12 and {c["customer_id"] for c in convos} == {f"C10{i}" for i in range(1, 7)}
        assert all(len(c["turns"]) >= 1 for c in convos) and len({c["id"] for c in convos}) == len(convos)

    @pytest.mark.parametrize("mutation, message", [
        (lambda c: c.update(customer_id="C999"), "unknown customer"),
        (lambda c: c["turns"][0]["expect"].update(vibes="good"), "unknown expectation"),
        (lambda c: c["turns"][0].update(say="  "), "empty"),
        (lambda c: c.update(turns=[]), "turns"),
        (lambda c: c["turns"][0]["expect"].update(tools_any=["teleport"]), "unknown tool"),
    ])
    def test_bad_files_are_refused_with_a_reason(self, tmp_path, mutation, message):
        data = convo([{"say": "Where is ORD1002?", "expect": {"tools_any": ["get_order_status"]}}])
        mutation(data)
        with pytest.raises(ValueError, match=message):
            ev.load_conversations(write(tmp_path, [data]), {"C101"})

    def test_duplicate_ids_are_refused(self, tmp_path):
        one = convo([{"say": "hi there", "expect": {}}])
        with pytest.raises(ValueError, match="duplicate"):
            ev.load_conversations(write(tmp_path, [one, dict(one)]), {"C101"})


class TestExpectations:
    def run(self, tmp_path, turns, llm=None):
        path = write(tmp_path, [convo(turns)])
        with ev.environment(lambda: llm or Lookup()) as env:
            return ev.run_conversations(path, env)

    def test_a_met_expectation_passes(self, tmp_path):
        out = self.run(tmp_path, [{"say": "Where is ORD1002?",
                                   "expect": {"tools_any": ["get_order_status"], "mentions": ["ORD1002"]}}])
        assert out["summary"]["conversations_passed"] == 1
        turn = out["results"][0]["turns"][0]
        assert turn["proposed_tool_calls"] == ["get_order_status"] and turn["failed"] == []

    @pytest.mark.parametrize("expect, failed", [
        ({"tools_any": ["list_my_orders"]}, "tools_any"), ({"tools_all": ["get_order_status", "search_policies"]}, "tools_all"),
        ({"mentions": ["ORD9999"]}, "mentions"), ({"mentions_any": ["nothing", "here"]}, "mentions_any"),
        ({"no_tools": True}, "no_tools"), ({"language": "hinglish"}, "language"),
        ({"statuses": ["needs_choice"]}, "statuses"), ({"no_pending": True, "pending_kind": "support_ticket"}, "pending_kind"),
    ])
    def test_an_unmet_expectation_fails_and_is_named(self, tmp_path, expect, failed):
        out = self.run(tmp_path, [{"say": "Where is ORD1002?", "expect": expect}])
        assert out["summary"]["conversations_passed"] == 0 and failed in out["results"][0]["turns"][0]["failed"]

    def test_not_asking_fails_when_the_reply_asks_for_the_order_again(self, tmp_path):
        out = self.run(tmp_path, [{"say": "when will it arrive?", "expect": {"not_asking": True}}],
                       llm=ScriptedLLM(chat=lambda s, m, t: LLMResult(text="Could you share the order ID?")))
        assert "not_asking" in out["results"][0]["turns"][0]["failed"]

    def test_each_turn_sees_the_earlier_ones_in_the_same_conversation(self, tmp_path):
        seen = []

        def chat(system, messages, tools):
            seen.append([m["content"] for m in messages if m["role"] == "user"])
            return LLMResult(text="ok")

        self.run(tmp_path, [{"say": "first message", "expect": {}}, {"say": "second message", "expect": {}}],
                 llm=ScriptedLLM(chat=chat))
        assert seen[-1] == ["first message", "second message"]

    def test_universal_checks_run_on_every_turn(self, tmp_path):
        out = self.run(tmp_path, [{"say": "Where is ORD1002?", "expect": {}}],
                       llm=Lookup("ORD1002 is Shipped. Email us at help@example.com or see ORD7777."))
        universal = out["results"][0]["turns"][0]["universal"]
        assert universal["no_invented_contact_details"] == ev.FAIL and universal["grounded_identifiers"] == ev.FAIL
        assert universal["no_unconfirmed_write"] == ev.PASS

    def test_identifiers_from_earlier_turns_are_grounded_later(self, tmp_path):
        out = self.run(tmp_path, [{"say": "Where is ORD1002?", "expect": {}}, {"say": "and that one?", "expect": {}}],
                       llm=Lookup("ORD1002 is Shipped."))
        assert all(t["universal"]["grounded_identifiers"] == ev.PASS for t in out["results"][0]["turns"])

    def test_a_turn_that_raises_is_recorded_not_fatal(self, tmp_path):
        def boom(system, messages, tools):
            raise RuntimeError("provider exploded")

        out = self.run(tmp_path, [{"say": "hello there", "expect": {"statuses": ["ok"]}}], llm=ScriptedLLM(chat=boom))
        assert out["results"][0]["turns"][0]["reply"]["status"] == "error"
        assert out["summary"]["conversations_passed"] == 0


class TestReport:
    def test_the_report_carries_conversations_and_the_context_scorecard(self, tmp_path):
        path = write(tmp_path, [convo([{"say": "Where is ORD1002?", "expect": {"mentions": ["ORD1002"]}}])])
        report = ev.run(ev._DEFAULT_CASES, RuleBasedLLM, conversations_path=path)
        assert report["conversations"]["summary"]["conversations_total"] == 1
        context = report["summary"]["scorecards"]["conversational"]["context"]
        assert set(context) >= {"conversations", "passed", "turn_expectations", "turn_expectations_passed"}

    def test_without_a_conversations_file_the_report_says_context_was_not_measured(self):
        report = ev.run(ev._DEFAULT_CASES, RuleBasedLLM)
        assert report["conversations"] is None
        assert report["summary"]["scorecards"]["conversational"]["context"]["status"] == "not_measured"
