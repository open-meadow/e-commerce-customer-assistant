"""Optional ``LLM_TEMPERATURE``: unset sends nothing (reasoning models reject the parameter); set is sent and
recorded so repeated evaluation runs are comparable. Mocked transport only; no network."""

from __future__ import annotations

import pytest

from app.llm import providers
from app.llm.base import LLMConfigError
from test_providers import Transport, anthropic_blocks, responses_text

BASE = {"LLM_PROVIDER": "openai", "LLM_MODEL": "m", "OPENAI_API_KEY": "sk-test-not-real"}
ANTHROPIC = {"LLM_PROVIDER": "anthropic", "LLM_MODEL": "m", "ANTHROPIC_API_KEY": "key-test-not-real"}


class TestTemperatureSetting:
    def test_unset_sends_no_temperature(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("a"))
        providers.build_llm_client(BASE).chat("S", [{"role": "user", "content": "hi"}], [], max_output_tokens=32)
        assert "temperature" not in t.sent(0)["body"]

    @pytest.mark.parametrize("value, expected", [("0", 0.0), ("0.2", 0.2), ("1", 1.0)])
    def test_set_is_sent_to_openai(self, monkeypatch, value, expected):
        t = Transport(monkeypatch, responses_text("a"))
        client = providers.build_llm_client({**BASE, "LLM_TEMPERATURE": value})
        client.chat("S", [{"role": "user", "content": "hi"}], [], max_output_tokens=32)
        assert t.sent(0)["body"]["temperature"] == expected and client._temperature == expected

    def test_set_is_sent_to_anthropic(self, monkeypatch):
        t = Transport(monkeypatch, anthropic_blocks({"type": "text", "text": "a"}))
        providers.build_llm_client({**ANTHROPIC, "LLM_TEMPERATURE": "0.3"}).chat(
            "S", [{"role": "user", "content": "hi"}], [], max_output_tokens=32)
        assert t.sent(0)["body"]["temperature"] == 0.3

    def test_the_tool_free_tasks_use_it_too(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("has_reason"))
        providers.build_llm_client({**BASE, "LLM_TEMPERATURE": "0"}).complete("S", "p", max_output_tokens=16)
        assert t.sent(0)["body"]["temperature"] == 0.0

    @pytest.mark.parametrize("bad", ["warm", "-0.1", "2.5", "nan"])
    def test_a_bad_value_is_a_configuration_error(self, bad):
        with pytest.raises(LLMConfigError, match="LLM_TEMPERATURE"):
            providers.build_llm_client({**BASE, "LLM_TEMPERATURE": bad})

    def test_readiness_reports_a_bad_value_by_name_only(self):
        report = providers.readiness({**BASE, "LLM_TEMPERATURE": "warm"})
        assert report["configured"] is False and "LLM_TEMPERATURE (not a number between 0 and 2)" in report["missing"]
        assert "warm" not in str(report)

    def test_blank_means_unset(self, monkeypatch):
        t = Transport(monkeypatch, responses_text("a"))
        providers.build_llm_client({**BASE, "LLM_TEMPERATURE": ""}).chat(
            "S", [{"role": "user", "content": "hi"}], [], max_output_tokens=32)
        assert "temperature" not in t.sent(0)["body"]

    def test_the_stand_in_ignores_it(self):
        assert providers.build_llm_client({"LLM_PROVIDER": "rule_based", "LLM_TEMPERATURE": "0"}).name == "rule_based"
