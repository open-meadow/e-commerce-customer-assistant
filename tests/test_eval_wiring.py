"""Phase 1 (trustworthy baseline), step A: the interpretation layer's switch is explicit and visible.

Before: ``Engine`` looked for ``interprets_language`` on whatever object it was given, and the evaluation's
``RecordingLLM`` wrapper did not forward it, so a live run silently used the offline stand-in's behaviour.
Now: the capability is forwarded, decided once, shown in every report, and a mismatch stops the run.

Scripted model output only; no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import real_eval as ev  # noqa: E402
from app.backend import Backend, Settings  # noqa: E402
from app.llm.base import LLMResult  # noqa: E402
from app.llm.rule_based import RuleBasedLLM  # noqa: E402
from backend_helpers import Client  # noqa: E402
from test_llm_interpretation import SmartLLM  # noqa: E402

REQUEST = "I want to return item IT101 from order ORD1001"
SMALL_CASES = Path(__file__).resolve().parents[1] / "app" / "db" / "data" / "eval" / "queries_conversational.csv"


class TestCapabilityIsForwarded:
    def test_the_recorder_forwards_a_capable_model(self):
        assert ev.RecordingLLM(SmartLLM()).interprets_language is True

    def test_the_recorder_forwards_the_stand_in_as_not_capable(self):
        assert ev.RecordingLLM(RuleBasedLLM()).interprets_language is False

    def test_the_recorder_counts_which_tool_free_tasks_were_asked(self):
        recorder = ev.RecordingLLM(SmartLLM(["has_reason"]))
        from app.llm import tasks

        tasks.interpret_reply(recorder, "return_request", "asked", "text")
        assert recorder.complete_by_task == {"TASK: interpret_reply": 1}
        recorder.reset()
        assert recorder.complete_by_task == {}


class TestBackendSetting:
    def test_a_wrapped_capable_model_activates_the_layer(self, temp_db):
        assert Backend(llm=ev.RecordingLLM(SmartLLM()), settings=Settings()).interpretation_active is True

    def test_the_stand_in_leaves_it_off(self, temp_db):
        assert Backend(llm=RuleBasedLLM(), settings=Settings()).interpretation_active is False

    def test_an_explicit_setting_wins_in_both_directions(self, temp_db):
        assert Backend(llm=SmartLLM(), settings=Settings(), interpret_replies=False).interpretation_active is False
        assert Backend(llm=RuleBasedLLM(), settings=Settings(), interpret_replies=True).interpretation_active is True

    def test_a_wrapped_model_really_is_asked_to_interpret(self, temp_db, raw):
        recorder = ev.RecordingLLM(SmartLLM(["no_reason", "changes_mind"]))
        backend = Backend(llm=recorder, settings=Settings())
        c = Client(backend, "C101")
        c.say(REQUEST)
        reply = c.say("I don't want to return it anymore")
        assert "won't start" in reply.text
        assert recorder.complete_by_task.get("TASK: interpret_reply") == 2
        assert backend.engine.interpret_calls == 2

    def test_a_wrapped_model_sees_messages_the_keyword_guard_would_have_answered(self, temp_db):
        seen = []

        class Chat(SmartLLM):
            def chat(self, system, messages, tools, *, max_output_tokens):
                seen.append(messages[-1]["content"])
                return LLMResult(text="Let me check your orders.")

        backend = Backend(llm=ev.RecordingLLM(Chat()), settings=Settings())
        text = "The charger isn't working, and I need a replacement."
        Client(backend, "C101").say(text)
        assert seen == [text]


class TestEvaluationEnvironment:
    def test_a_capable_model_with_a_broken_wrapper_stops_the_run(self, monkeypatch):
        class Forgetful(ev.RecordingLLM):
            def __init__(self, inner):
                super().__init__(inner)
                del self.interprets_language  # a wrapper that drops the capability

        monkeypatch.setattr(ev, "RecordingLLM", Forgetful)
        with pytest.raises(RuntimeError, match="interpretation"):
            with ev.environment(lambda: SmartLLM()):
                pass

    def test_the_stand_in_does_not_trigger_the_wiring_check(self):
        with ev.environment(RuleBasedLLM) as env:
            assert env.backend.interpretation_active is False


@pytest.fixture(scope="module")
def stand_in_report():
    return ev.run(SMALL_CASES, RuleBasedLLM)


@pytest.fixture(scope="module")
def capable_report():
    return ev.run(SMALL_CASES, lambda: SmartLLM())


class TestReportShowsActiveLayers:
    def test_the_stand_in_report_says_the_layer_was_off(self, stand_in_report):
        layers = stand_in_report["layers"]
        assert layers["interpretation_active"] is False and layers["guard_mode"] == "keyword"
        assert layers["interpret_reply_calls"] == 0

    def test_the_capable_report_says_the_layer_was_on(self, capable_report):
        layers = capable_report["layers"]
        assert layers["interpretation_active"] is True and layers["guard_mode"] == "model"
        assert layers["model"]["provider"] == "smart-scripted"

    def test_every_report_names_its_prompt_retrieval_and_temperature(self, stand_in_report):
        layers = stand_in_report["layers"]
        assert len(layers["system_prompt_sha256"]) == 12 and layers["retrieval_mode"] == "local"
        assert "temperature" in layers

    def test_the_prompt_fingerprint_changes_when_the_prompt_changes(self, monkeypatch):
        first = ev.layers_description(None, 0, "local")["system_prompt_sha256"]
        monkeypatch.setattr(ev.engine_module, "SYSTEM_PROMPT", ev.engine_module.SYSTEM_PROMPT + " Extra rule.")
        assert ev.layers_description(None, 0, "local")["system_prompt_sha256"] != first
