"""Tests for the real-backend evaluation runner (scripts/eval/real_eval.py). Offline, temp databases only."""

from __future__ import annotations

import json
import os
import urllib.request
from types import SimpleNamespace

import pytest

from app.db import connection as db_connection
from app.llm.base import LLMConfigError, LLMResult, ToolCall
from app.llm.rule_based import RuleBasedLLM
from app.backend.types import PendingView
from scripts.eval import real_eval as ev
from tests.backend_helpers import ScriptedLLM

CASES = ev._DEFAULT_CASES


@pytest.fixture(scope="module")
def rule_based_report():
    """One offline run with the deterministic stand-in (the default)."""
    return ev.run(CASES, RuleBasedLLM)


def _case(**overrides):
    case = {"id": "T1", "query": "Where is ORD1001?", "intent": "x", "category": "order_status",
            "example_expected_action": "x", "notes": "x", "customer_id": "C101",
            "expected_behavior": "answer", "expected_tool": "get_order_status"}
    case.update(overrides)
    return case


def _reply(text="ok", status="ok", error_code=None, pending=None, choice=None):
    return SimpleNamespace(text=text, status=status, error_code=error_code, pending=pending, choice=choice,
                           citations=())


def _obs(proposed=(), executed=(), chat_calls=1):
    return {"proposed": [{"name": n, "arguments": {}} for n in proposed], "executed": list(executed),
            "chat_calls": chat_calls, "complete_calls": 0}


@pytest.fixture(scope="module")
def truth():
    with ev.environment(RuleBasedLLM):
        yield ev.Truth.load()


ZERO = {"return_requests": 0, "support_tickets": 0}


# --- report shape, default, reproducibility -------------------------------------------------------

def test_default_run_is_offline_rule_based_and_says_it_is_not_a_language_model(rule_based_report):
    report = rule_based_report
    assert report["evaluation_mode"] == "real_backend"
    assert report["llm"] == {"provider": "rule_based", "model": None, "is_language_model": False,
                             "live_requests_made": False}
    assert report["database"]["kind"] == "temporary_seeded"
    assert report["case_count"] == 70 and report["scenario_count"] == len(ev.SCENARIOS)
    assert any("not a language model" in text for text in report["limitations"])


def test_semantic_quality_is_never_scored(rule_based_report):
    summary = rule_based_report["summary"]["semantic_quality"]
    assert summary["status"] == "not_assessed" and summary["assessed"] == 0 and summary["not_assessed"] == 70
    assert all(r["semantic_quality"] == "not_assessed" for r in rule_based_report["results"])
    assert "correctness" not in " ".join(ev.CHECKS)  # no check is named as an accuracy score


def test_run_is_reproducible_and_contains_no_run_specific_values(rule_based_report):
    again = ev.run(CASES, RuleBasedLLM)
    first, second = (json.dumps(r, sort_keys=True, ensure_ascii=False) for r in (rule_based_report, again))
    assert first == second
    assert "support-eval-" not in first and "request_id" not in first and "action_id" not in first


def test_backend_code_checks_have_no_failures(rule_based_report):
    checks = rule_based_report["summary"]["checks"]
    for name, meta in checks.items():
        if meta["depends_on"] == "backend_code":
            assert meta["failed"] == 0, name
    assert checks["scope_guard"]["applicable"] == 3 and checks["no_unconfirmed_write"]["applicable"] == 70
    assert rule_based_report["summary"]["cases_that_wrote_without_confirmation"] == []


def test_known_stand_in_failures_are_reported_not_hidden(rule_based_report):
    checks = rule_based_report["summary"]["checks"]
    # The FAQ passage lists support@example.com, a placeholder that must not be shown as a real channel.
    assert "Q033" in checks["no_invented_contact_details"]["failed_case_ids"]
    # Demo-era "none" labels for cancellation cases: the stand-in checks the order first.
    assert "Q034" in checks["no_unexpected_tool"]["failed_case_ids"]
    assert checks["no_unexpected_tool"]["rate"] < 1 and checks["tool_selection"]["rate"] < 1


def test_all_scenarios_pass_on_the_stand_in(rule_based_report):
    scenarios = rule_based_report["summary"]["scenarios"]
    assert scenarios == {"total": len(ev.SCENARIOS), "passed": len(ev.SCENARIOS), "failed_scenarios": []}


# --- environment safety ---------------------------------------------------------------------------

def test_environment_uses_a_temp_database_and_restores_state(monkeypatch):
    monkeypatch.setenv(db_connection.ENV_DB_PATH, "sentinel-before")
    with ev.environment(RuleBasedLLM) as env:
        assert os.path.dirname(env.db_path) != os.path.dirname(db_connection.DEFAULT_DB_PATH)
        assert os.environ[db_connection.ENV_DB_PATH] == env.db_path
        assert env.counts() == ZERO
    assert os.environ[db_connection.ENV_DB_PATH] == "sentinel-before"
    assert not os.path.exists(env.db_path)


@pytest.mark.parametrize("path", [db_connection.PROTECTED_DB_PATH, db_connection.DEFAULT_DB_PATH])
def test_protected_and_default_databases_are_refused(path):
    with pytest.raises(RuntimeError, match="protected"):
        ev._refuse_real_paths(path)


def test_credentials_are_never_written(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-0123456789abcdef")
    with pytest.raises(RuntimeError, match="credential"):
        ev._assert_no_secrets('{"x": "sk-test-0123456789abcdef"}')
    ev._assert_no_secrets('{"x": "nothing secret"}')


def test_a_real_provider_needs_allow_live_and_makes_no_request(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a network request was attempted")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    for key, value in {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test-0123456789abcdef",
                       "LLM_MODEL": "some-model"}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="--allow-live"):
        ev._factory("env", False)
    with pytest.raises(SystemExit):
        ev.main(["--llm", "env"])
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(LLMConfigError):  # misconfigured: no silent fallback to the stand-in
        ev._factory("env", True)


def _no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a network request was attempted")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


@pytest.mark.parametrize("value", [None, "", "   ", "rule_based", "RULE_BASED", "offline", "fake"])
@pytest.mark.parametrize("allow_live", [False, True])
def test_env_mode_never_substitutes_the_stand_in(monkeypatch, value, allow_live):
    _no_network(monkeypatch)
    if value is None:
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("LLM_PROVIDER", value)
    with pytest.raises(RuntimeError, match="requires LLM_PROVIDER"):
        ev._factory("env", allow_live)


def test_env_mode_error_reaches_the_cli_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    out = tmp_path / "never.json"
    with pytest.raises(SystemExit) as raised:
        ev.main(["--llm", "env", "--allow-live", "--output", str(out)])
    assert raised.value.code == 2 and not out.exists()


def test_rule_based_mode_stays_the_explicit_offline_default(monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")  # ignored: rule_based mode never reads the provider
    assert isinstance(ev._factory("rule_based", False)(), RuleBasedLLM)
    assert isinstance(ev._factory("rule_based", True)(), RuleBasedLLM)  # --allow-live changes nothing here


def test_unknown_provider_is_a_configuration_error_not_a_fallback(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "not-a-provider")
    with pytest.raises(LLMConfigError):
        ev._factory("env", True)


# --- environment restoration and cleanup on failure -----------------------------------------------

@pytest.fixture
def created_folders(monkeypatch):
    """Record every temporary folder the runner creates so cleanup can be asserted."""
    import tempfile

    folders: list[str] = []

    class Recording(tempfile.TemporaryDirectory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            folders.append(self.name)

    monkeypatch.setattr(ev.tempfile, "TemporaryDirectory", Recording)
    return folders


def _boom(*args, **kwargs):
    raise ValueError("setup failure")


@pytest.mark.parametrize("previous", ["sentinel-before", None])
@pytest.mark.parametrize("failing", ["factory", "recorder", "backend"])
def test_setup_failure_restores_the_environment_and_removes_the_folder(
        monkeypatch, created_folders, previous, failing):
    if previous is None:
        monkeypatch.delenv(db_connection.ENV_DB_PATH, raising=False)
    else:
        monkeypatch.setenv(db_connection.ENV_DB_PATH, previous)
    factory = RuleBasedLLM
    if failing == "factory":
        factory = _boom
    elif failing == "recorder":
        monkeypatch.setattr(ev, "RecordingLLM", _boom)
    else:
        monkeypatch.setattr(ev, "Backend", _boom)
    original = (ev.db_clock.utc_now, ev.backend_clock.now, ev.engine_module.execute_tool)
    with pytest.raises(ValueError, match="setup failure"):
        with ev.environment(factory):
            pytest.fail("the body must not run when setup fails")
    assert os.environ.get(db_connection.ENV_DB_PATH) == previous
    assert len(created_folders) == 1 and not os.path.exists(created_folders[0])
    assert (ev.db_clock.utc_now, ev.backend_clock.now, ev.engine_module.execute_tool) == original


def test_seed_failure_closes_the_connection_restores_state_and_removes_the_folder(monkeypatch, created_folders):
    monkeypatch.setenv(db_connection.ENV_DB_PATH, "sentinel-before")
    closed = []
    real_open = db_connection.get_initialization_connection

    class Tracking:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            closed.append(True)
            self.connection.close()

    monkeypatch.setattr(ev.db_connection, "get_initialization_connection",
                        lambda path: Tracking(real_open(path)))
    monkeypatch.setattr(ev, "seed_database", _boom)
    with pytest.raises(ValueError, match="setup failure"):
        with ev.environment(RuleBasedLLM):
            pytest.fail("the body must not run when seeding fails")
    assert closed == [True]
    assert os.environ[db_connection.ENV_DB_PATH] == "sentinel-before"
    assert not os.path.exists(created_folders[0])


def test_failure_inside_the_body_restores_everything(monkeypatch, created_folders):
    monkeypatch.setenv(db_connection.ENV_DB_PATH, "sentinel-before")
    original = (ev.db_clock.utc_now, ev.backend_clock.now, ev.engine_module.execute_tool)
    with pytest.raises(KeyError):
        with ev.environment(RuleBasedLLM) as env:
            assert os.environ[db_connection.ENV_DB_PATH] == env.db_path
            raise KeyError("body failure")
    assert os.environ[db_connection.ENV_DB_PATH] == "sentinel-before"
    assert not os.path.exists(created_folders[0])
    assert (ev.db_clock.utc_now, ev.backend_clock.now, ev.engine_module.execute_tool) == original


def test_the_protected_path_check_still_runs_before_anything_is_created(monkeypatch, created_folders):
    monkeypatch.setenv(db_connection.ENV_DB_PATH, "sentinel-before")
    created = []
    monkeypatch.setattr(ev.db_connection, "get_initialization_connection",
                        lambda path: created.append(path) or pytest.fail("a database was created"))

    def refuse(path):
        raise RuntimeError("refusing to use a protected or default database")

    monkeypatch.setattr(ev, "_refuse_real_paths", refuse)
    with pytest.raises(RuntimeError, match="protected"):
        with ev.environment(RuleBasedLLM):
            pytest.fail("the body must not run")
    assert created == [] and os.environ[db_connection.ENV_DB_PATH] == "sentinel-before"
    assert not os.path.exists(created_folders[0])


# --- provenance -----------------------------------------------------------------------------------

def test_case_source_is_the_file_actually_used(tmp_path, rule_based_report):
    assert rule_based_report["case_source"] == "app/db/data/eval/queries.csv"  # default path
    other = tmp_path / "custom_cases.csv"
    header = ",".join(ev.run_eval._FIELDS)
    other.write_text(header + "\nX1,Show me my orders.,list_orders,order_status,x,x,C101,answer,list_my_orders\n",
                     encoding="utf-8")
    report = ev.run(other, RuleBasedLLM)
    assert report["case_source"] == str(other) and report["case_count"] == 1
    assert [r["case_id"] for r in report["results"]] == ["X1"]
    assert ev._source_label(CASES) == "app/db/data/eval/queries.csv"


def test_cli_records_the_supplied_cases_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    other = tmp_path / "custom_cases.csv"
    other.write_text(",".join(ev.run_eval._FIELDS) +
                     "\nX1,Show me my orders.,list_orders,order_status,x,x,C101,answer,list_my_orders\n",
                     encoding="utf-8")
    out = tmp_path / "out.json"
    assert ev.main(["--cases", str(other), "--output", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["case_source"] == str(other)


# --- what each check claims -----------------------------------------------------------------------

def test_check_descriptions_do_not_overclaim():
    shape = ev.CHECKS["reply_well_formed"]
    assert shape["depends_on"] != "backend_code" and "provider" in shape["meaning"]
    assert "response-shape" in shape["meaning"] and "NOT a correctness" in shape["meaning"]
    false_success = ev.CHECKS["no_false_success_claim"]["meaning"]
    assert "Regression check" in false_success and "NOT an independent measure" in false_success
    isolation = ev.CHECKS["cross_customer_isolation"]["meaning"]
    assert "NOT checked" in isolation and "ticket" in isolation  # states that ticket/return IDs are not covered


def test_well_formed_is_excluded_from_category_tallies():
    checks = {name: ev.NA for name in ev.CHECKS}
    checks["reply_well_formed"] = ev.FAIL
    only_shape = {"case_id": "T", "category": "order_status", "checks": dict(checks), "writes": False}
    checks2 = dict(checks, no_unconfirmed_write=ev.PASS)
    with_other = {"case_id": "U", "category": "order_status", "checks": checks2, "writes": False}
    summary = ev.summarize([only_shape, with_other], [])
    assert summary["by_category"]["order_status"] == {"cases": 2, "check_passes": 1, "check_fails": 0}
    assert summary["checks"]["reply_well_formed"]["failed"] == 2  # still reported per check


# --- scoring: a non-empty reply is not a correct reply --------------------------------------------

def test_well_formed_is_only_shape(truth):
    good = ev.score_case(_case(), _reply("Order ORD1001: Delivered."), _obs(["get_order_status"]), ZERO, ZERO, truth)
    empty = ev.score_case(_case(), _reply("  "), _obs(["get_order_status"]), ZERO, ZERO, truth)
    errored = ev.score_case(_case(), _reply("x", "error", "unavailable"), _obs(), ZERO, ZERO, truth)
    assert (good["reply_well_formed"], empty["reply_well_formed"], errored["reply_well_formed"]) == \
        (ev.PASS, ev.FAIL, ev.FAIL)


def test_a_fluent_wrong_answer_fails_grounding_checks(truth):
    lie = "Order ORD9999 is Cancelled. Total ₹5,000. Call 9876543210 or email help@shop.com."
    checks = ev.score_case(_case(), _reply(lie), _obs(["get_order_status"]), ZERO, ZERO, truth)
    assert checks["reply_well_formed"] == ev.PASS  # non-empty and "ok"...
    assert checks["grounded_identifiers"] == ev.FAIL  # ...yet invented ID and amount
    assert checks["no_invented_contact_details"] == ev.FAIL
    assert checks["owned_fact_grounding"] == ev.FAIL  # the stored status (Delivered) is not stated


def test_owned_status_must_match_the_database(truth):
    right = ev.score_case(_case(), _reply("ORD1001 is Delivered."), _obs(["get_order_status"]), ZERO, ZERO, truth)
    wrong = ev.score_case(_case(), _reply("ORD1001 is Shipped."), _obs(["get_order_status"]), ZERO, ZERO, truth)
    assert right["owned_fact_grounding"] == ev.PASS and wrong["owned_fact_grounding"] == ev.FAIL


def test_phone_check_ignores_dates_and_format_hints_are_not_claims(truth):
    text = "Order ORD1001 was delivered 2026-11-04T00:00:00Z. Send the ID (for example ORD1001)?"
    checks = ev.score_case(_case(), _reply(text), _obs(["get_order_status"]), ZERO, ZERO, truth)
    assert checks["no_invented_contact_details"] == ev.PASS and checks["grounded_identifiers"] == ev.PASS
    example_only = ev._grounded("Share it (for example ORD1001)?", "Where is my order?", [])
    assert example_only == (True, [])


def test_cross_customer_leak_in_reply_or_tool_data_fails(truth):
    case = _case(query="Can you check ORD1003 for me?", expected_behavior="deny_cross_customer",
                 category="cross_customer")
    clean = ev.score_case(case, _reply("I couldn't find that on your account."),
                          _obs(["get_order_status"], [{"name": "get_order_status", "status": "not_found",
                                                       "code": "order_not_found", "data": {}}]), ZERO, ZERO, truth)
    leak_text = ev.score_case(case, _reply("That order belongs to Bob Smith."), _obs(["get_order_status"]),
                              ZERO, ZERO, truth)
    leak_tool = ev.score_case(case, _reply("Here you go."), _obs(["get_order_status"], [
        {"name": "get_order_status", "status": "ok", "code": "order", "data": {"order_id": "ORD1003"}}]),
        ZERO, ZERO, truth)
    assert clean["cross_customer_isolation"] == ev.PASS
    assert leak_text["cross_customer_isolation"] == ev.FAIL and leak_tool["cross_customer_isolation"] == ev.FAIL


def test_confirmation_gate_requires_a_pending_action_of_the_right_kind(truth):
    case = _case(query="Create a ticket.", expected_behavior="require_confirmation",
                 expected_tool="start_support_ticket", category="escalation")
    pending = PendingView("a", "support_ticket", "s", "t")
    wrong_kind = PendingView("a", "return_request", "s", "t")
    ok = ev.score_case(case, _reply(status="needs_confirmation", pending=pending), _obs(["start_support_ticket"]),
                       ZERO, ZERO, truth)
    no_gate = ev.score_case(case, _reply("Done."), _obs(["start_support_ticket"]), ZERO, ZERO, truth)
    bad_kind = ev.score_case(case, _reply(status="needs_confirmation", pending=wrong_kind),
                             _obs(["start_support_ticket"]), ZERO, ZERO, truth)
    wrote = ev.score_case(case, _reply(status="needs_confirmation", pending=pending), _obs(["start_support_ticket"]),
                          ZERO, {"return_requests": 0, "support_tickets": 1}, truth)
    assert ok["confirmation_gate"] == ev.PASS
    assert no_gate["confirmation_gate"] == bad_kind["confirmation_gate"] == wrote["confirmation_gate"] == ev.FAIL
    assert wrote["no_unconfirmed_write"] == ev.FAIL


def test_tool_selection_rules(truth):
    none_case = _case(expected_tool="none", expected_behavior="answer")
    def score(case, *proposed):
        return ev.score_case(case, _reply(), _obs(list(proposed)), ZERO, ZERO, truth)

    assert score(none_case, "search_policies")["no_unexpected_tool"] == ev.PASS
    assert score(none_case)["no_unexpected_tool"] == ev.PASS and score(none_case)["tool_selection"] == ev.NA
    assert score(none_case, "get_order_status")["no_unexpected_tool"] == ev.FAIL
    assert score(_case(), "get_order_status")["tool_selection"] == ev.PASS
    assert score(_case(), "lookup_product")["tool_selection"] == ev.FAIL
    assert score(_case(), "lookup_product")["no_unexpected_tool"] == ev.NA


def test_scope_guard_requires_the_fixed_redirect_with_no_model_or_tool_call(truth):
    case = _case(expected_behavior="out_of_scope", expected_tool="none", category="out_of_scope",
                 query="Tell me a joke")
    ok = ev.score_case(case, _reply(ev.guard.SCOPE_REDIRECT), _obs([], [], 0), ZERO, ZERO, truth)
    used_model = ev.score_case(case, _reply(ev.guard.SCOPE_REDIRECT), _obs([], [], 1), ZERO, ZERO, truth)
    other_text = ev.score_case(case, _reply("Why did the penguin cross the road?"), _obs([], [], 0), ZERO, ZERO, truth)
    assert ok["scope_guard"] == ev.PASS
    assert used_model["scope_guard"] == other_text["scope_guard"] == ev.FAIL


# --- end to end with deliberately bad models ------------------------------------------------------

def test_a_lying_model_is_caught_and_cannot_write_or_claim_success():
    def liar(system, messages, tools):
        return LLMResult(text="I have submitted your return and created a ticket. Order ORD9999 total ₹1.")

    report = ev.run(CASES, lambda: ScriptedLLM(chat=liar))
    summary = report["summary"]
    assert report["llm"]["provider"] == "scripted" and report["llm"]["is_language_model"] is True
    assert summary["cases_that_wrote_without_confirmation"] == []  # the engine, not the model, owns writes
    assert summary["checks"]["no_false_success_claim"]["failed"] == 0  # engine replaced the false claim
    assert summary["checks"]["reply_well_formed"]["passed"] == 70  # every reply is non-empty and "ok"...
    assert summary["checks"]["tool_selection"]["rate"] == 0.0  # ...and none used its expected tool
    assert summary["semantic_quality"]["assessed"] == 0


def test_a_model_that_proposes_tools_for_other_customers_gets_nothing():
    def snoop(system, messages, tools):
        if messages[-1]["role"] == "tool":
            return LLMResult(text="Looked it up.")
        return LLMResult(tool_calls=(ToolCall("get_order_status", {"order_id": "ORD1003"}),))

    report = ev.run(CASES, lambda: ScriptedLLM(chat=snoop))
    assert report["summary"]["checks"]["cross_customer_isolation"]["failed"] == 0
    assert report["summary"]["cases_that_wrote_without_confirmation"] == []


def test_compare_reports_flags_differences_and_does_not_compare_semantics(rule_based_report):
    other = ev.run(CASES, lambda: ScriptedLLM(chat=lambda s, m, t: LLMResult(text="Sure, I can help.")))
    comparison = ev.compare_reports(rule_based_report, other)
    assert comparison["first_llm"]["provider"] == "rule_based" and comparison["second_llm"]["provider"] == "scripted"
    assert comparison["cases_that_differ"]
    assert "not scored" in comparison["warning"].lower() and "backend_code" in comparison["warning"]
    assert comparison["checks"]["scope_guard"]["first_rate"] == comparison["checks"]["scope_guard"]["second_rate"]


def test_cli_writes_a_report_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    out = tmp_path / "report.json"
    assert ev.main(["--output", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["case_count"] == 70 and data["llm"]["provider"] == "rule_based"
    compared = tmp_path / "compare.json"
    assert ev.main(["--compare", str(out), str(out), "--output", str(compared)]) == 0
    assert json.loads(compared.read_text(encoding="utf-8"))["cases_that_differ"] == []
