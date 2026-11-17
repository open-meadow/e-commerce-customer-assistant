"""Corrective v2 instruments: a tighter timestamp check, exact call totals, an early stop on a provider outage, visible item
recovery, explicit denominators, the affected-behaviours set, E02's corrected truth and the retry runner. Offline only
(scripted models and the stand-in); no network and no real sleeping."""

from __future__ import annotations

import csv
import json
import socket
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "eval"
sys.path.insert(0, str(SCRIPTS))
import real_eval as ev  # noqa: E402
import run_valid  # noqa: E402
import app.backend  # noqa: E402,F401
from app.llm.base import LLMError, LLMResult, ToolCall  # noqa: E402
from app.llm.rule_based import RuleBasedLLM  # noqa: E402
from backend_helpers import ScriptedLLM  # noqa: E402

EVAL = Path(__file__).resolve().parents[1] / "app" / "db" / "data" / "eval"
AFFECTED = EVAL / "conversations_affected_v2.json"
CHECK = "check_return_eligibility"


@pytest.fixture(autouse=True)
def no_leftover_outage_limit():
    ev.PROVIDER_OUTAGE_LIMIT = 0
    yield
    ev.PROVIDER_OUTAGE_LIMIT = 0


class Sequence(ScriptedLLM):
    interprets_language = True

    def __init__(self, *answers):
        super().__init__(chat=self._next)
        self.answers = list(answers)

    def _next(self, system, messages, tools):
        return self.answers.pop(0)


def failing_model():
    def boom(system, messages, tools):
        raise LLMError("no route", category="dns")

    llm = ScriptedLLM(chat=boom)
    llm.interprets_language = True
    return llm


# ---------------------------------------------------------------------------
# The timestamp check
# ---------------------------------------------------------------------------

class TestTighterTimestampCheck:
    ISO = "2026-11-07T12:00:00Z"

    @pytest.mark.parametrize("reply", [
        "until 7 November 2026, 12:00 UTC", "until 12:00 UTC on 7 November", "November 7, 2026 at 12:00 UTC",
        "7 nov 2026 12:00 utc", "The deadline is open until 7th November 2026, 12:00 UTC (about 12 hours left)."])
    def test_the_moment_in_its_usual_forms_passes(self, reply):
        assert ev.states_time(reply, self.ISO)

    @pytest.mark.parametrize("reply", [
        "The ordinary period (7 days from delivery) is open until 12 November 2026, 12:00 UTC.",  # the live false pass
        "7 days after delivery; closes 11 November 2026, 12:00 UTC", "until 12:00 UTC",
        "until 7 November 2026", "until 7 November 2026, 12:00", "until 8 November 2026, 12:00 UTC",
        "until 7 December 2026, 12:00 UTC", "until 7 November 2026, 11:00 UTC", "7 days, November, 12:00, UTC"])
    def test_a_stray_digit_or_another_moments_pieces_do_not_stand_in(self, reply):
        assert not ev.states_time(reply, self.ISO)

    def test_one_moments_date_cannot_borrow_anothers_time(self):
        reply = "7 November 2026, 00:00 UTC is when it started and 11 November 2026, 12:00 UTC is when it ends."
        assert not ev.states_time(reply, self.ISO)

    def test_both_moments_of_a_two_period_answer_are_found(self):
        reply = ("Ordinary until 12 November 2026, 12:00 UTC. The reporting deadline is open until "
                 "7 November 2026, 12:00 UTC.")
        assert ev.states_time(reply, "2026-11-12T12:00:00Z") and ev.states_time(reply, self.ISO)


# ---------------------------------------------------------------------------
# Call totals and the early stop
# ---------------------------------------------------------------------------

class TestCallTotalsAndOutageStop:
    def test_every_attempt_is_counted_including_failed_ones(self):
        ev.reset_call_totals()
        report = ev.run_heldout(lambda: failing_model(), path=AFFECTED, set_name="affected")
        assert report["model_calls_total"]["chat"] >= 14  # one failed attempt per conversation at least
        assert report["integrity"]["run_valid_for_scoring"] is False

    def test_totals_cover_the_whole_original_style_run_including_scenarios(self):
        report = ev.run(EVAL / "queries_conversational.csv", RuleBasedLLM, conversations_path=EVAL / "conversations.json")
        assert report["model_calls_total"]["chat"] > 50 and set(report["model_calls_total"]) == {"chat", "complete"}

    def test_a_run_stops_after_consecutive_provider_failures(self):
        ev.PROVIDER_OUTAGE_LIMIT = 3
        with pytest.raises(ev.ProviderOutage) as stopped:
            ev.run_heldout(lambda: failing_model(), path=AFFECTED, set_name="affected")
        assert stopped.value.failures == 3 and ev.CALL_TOTALS["chat"] == 3  # not 14 or more

    def test_a_success_resets_the_streak(self):
        ev.PROVIDER_OUTAGE_LIMIT = 2
        ev.reset_call_totals()
        ev._note_provider_result(True)
        ev._note_provider_result(False)
        ev._note_provider_result(True)  # one failure again, not two in a row
        with pytest.raises(ev.ProviderOutage):
            ev._note_provider_result(True)

    def test_the_stop_is_off_by_default(self):
        report = ev.run_heldout(lambda: failing_model(), path=AFFECTED, set_name="affected")
        assert report["integrity"]["provider_error_replies"]["total"] > 3

    def test_the_command_line_writes_a_stub_with_the_calls_used_and_exits_3(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ev, "_factory", lambda mode, allow: (lambda: failing_model()))
        out = tmp_path / "stub.json"
        code = ev.main(["--affected", "--llm", "env", "--allow-live", "--stop-after-provider-failures", "3",
                        "--output", str(out)])
        stub = json.loads(out.read_text(encoding="utf-8"))
        assert code == 3 and stub["evaluation_mode"] == "aborted_provider_outage"
        assert stub["integrity"]["run_valid_for_scoring"] is False and stub["model_calls_total"]["chat"] == 3

    def test_an_ordinary_error_is_not_swallowed_as_an_outage_by_the_engine(self):
        assert issubclass(ev.ProviderOutage, BaseException) and not issubclass(ev.ProviderOutage, Exception)


# ---------------------------------------------------------------------------
# Item recovery and denominators in the reports
# ---------------------------------------------------------------------------

def run_set(tmp_path, convos, llm):
    path = tmp_path / "set.json"
    path.write_text(json.dumps(convos), encoding="utf-8")
    with ev.environment(lambda: llm) as env:
        return ev.run_conversations(path, env, record_calls=True)


def one(say, customer="C103"):
    return [{"id": "X1", "customer_id": customer, "category": "t", "turns": [{"say": say, "expect": {}}]}]


class TestItemRecoveryIsVisible:
    GUESS = LLMResult(tool_calls=(ToolCall(CHECK, {"order_id": "ORD1005", "item_id": "IT999"}, "e1"),))

    def test_the_turn_records_the_recovered_item_and_the_engines_extra_read(self, tmp_path):
        report = run_set(tmp_path, one("Is it too late to return the charger from ORD1005?"),
                         Sequence(self.GUESS, LLMResult(text="Until 12 November 2026, 12:00 UTC.")))
        turn = report["results"][0]["turns"][0]
        assert turn["item_recoveries"] == [{"order_id": "ORD1005", "model_item": "IT999", "used_item": "IT106"}]
        checks = [r for r in turn["tool_results"] if r["name"] == CHECK]
        assert [c["result"] for c in checks] == ["item_not_on_this_order", "timing_check"]  # the guess, then the recovery
        assert checks[1]["language"] == "en"
        assert turn["engine_added_reads"] == {CHECK: 1}  # the engine's own re-run of the check
        assert turn["proposed_tool_arguments"][0]["arguments"]["item_id"] == "IT999"  # the model's own guess is kept
        assert report["summary"]["item_recovery_events"] == 1

    def test_no_recovery_means_no_event(self, tmp_path):
        ok = LLMResult(tool_calls=(ToolCall(CHECK, {"order_id": "ORD1005", "item_id": "IT106"}, "e1"),))
        report = run_set(tmp_path, one("Is it too late to return the charger from ORD1005?"),
                         Sequence(ok, LLMResult(text="Until 12 November 2026, 12:00 UTC.")))
        turn = report["results"][0]["turns"][0]
        assert turn["item_recoveries"] == [] and report["summary"]["item_recovery_events"] == 0
        assert [r["result"] for r in turn["tool_results"] if r["name"] == CHECK] == ["timing_check"]

    def test_only_small_fields_are_kept_never_the_customer_facing_paragraphs(self, tmp_path):
        report = run_set(tmp_path, one("Is it too late to return the charger from ORD1005?"),
                         Sequence(self.GUESS, LLMResult(text="ok")))
        text = json.dumps(report["results"][0]["turns"][0]["tool_results"])
        assert "facts_to_state" not in text and "ordinary return period" not in text

    def test_the_review_sheet_carries_the_tool_results(self):
        item = ev.run_heldout(RuleBasedLLM, path=AFFECTED, set_name="affected")["review_sheet"][0]
        assert {"tool_results", "engine_added_reads", "item_recoveries"} <= set(item)

    def test_an_original_style_run_does_not_get_the_recorded_fields(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps(one("hello", "C101")), encoding="utf-8")
        with ev.environment(RuleBasedLLM) as env:
            turn = ev.run_conversations(path, env)["results"][0]["turns"][0]
        assert "tool_results" not in turn and "engine_added_reads" not in turn


class TestDenominatorsAreExplicit:
    def test_the_original_conversations_have_the_documented_denominators(self):
        with ev.environment(RuleBasedLLM) as env:
            summary = ev.run_conversations(EVAL / "conversations.json", env)["summary"]
        assert summary["conversations_total"] == 12 and summary["turns_total"] == 23
        assert summary["expectation_keys_total"] == 46 == summary["turn_expectations"]
        assert summary["universal_checks_applicable"] > 100 and "not turns" in summary["denominators_note"]
        assert summary["universal_checks_passed"] <= summary["universal_checks_applicable"]

    def test_the_old_keys_are_still_there_so_v2_and_v5_stay_comparable(self):
        with ev.environment(RuleBasedLLM) as env:
            summary = ev.run_conversations(EVAL / "conversations.json", env)["summary"]
        assert {"conversations_total", "conversations_passed", "turn_expectations", "turn_expectations_passed",
                "failed_turns", "universal_check_failures"} <= set(summary)

    def test_the_single_turn_scorecards_do_not_include_the_conversations(self):
        report = ev.run(EVAL / "queries_conversational.csv", RuleBasedLLM, conversations_path=EVAL / "conversations.json")
        technical = report["summary"]["scorecards"]["technical"]
        assert technical["applicable"] == sum(c["applicable"] for c in technical["checks"].values())
        assert report["summary"]["scorecards"]["conversational"]["context"]["turn_expectations"] == 46


# ---------------------------------------------------------------------------
# The affected-behaviours set and E02
# ---------------------------------------------------------------------------

class TestAffectedSet:
    def convos(self):
        return json.loads(AFFECTED.read_text(encoding="utf-8"))

    def test_composition(self):
        convos = ev.load_conversations(AFFECTED, {f"C10{i}" for i in range(1, 7)})
        ids = [c["id"] for c in convos]
        assert ids == ["G01", "G02", "G03", "G04", "N01", "N02", "O01", "O02", "S01", "S02", "S03", "S04", "S05", "L01"]
        assert sum(len(c["turns"]) for c in convos) == 15 and len(convos[-1]["turns"]) == 2

    def test_every_turn_states_what_is_right_and_wrong(self):
        for c in self.convos():
            for t in c["turns"]:
                assert len(t["ground_truth"]) > 60 and "WRONG" in t["ground_truth"], c["id"]

    def test_no_message_copies_a_held_out_or_development_message(self):
        held = {t["say"].strip().lower() for c in json.loads((EVAL / "heldout" / "heldout_v1.json").read_text(encoding="utf-8"))
                for t in c["turns"]}
        mine = {t["say"].strip().lower() for c in self.convos() for t in c["turns"]}
        dev = set()
        for name in ("conversations.json", "conversations_identity.json", "conversations_eligibility.json"):
            dev |= {t["say"].strip().lower() for c in json.loads((EVAL / name).read_text(encoding="utf-8")) for t in c["turns"]}
        for name in ("queries_conversational.csv", "queries.csv"):
            with open(EVAL / name, encoding="utf-8", newline="") as handle:
                dev |= {r["query"].strip().lower() for r in csv.DictReader(handle)}
        assert not mine & held and not mine & dev

    def test_the_facts_the_cases_rely_on_are_in_the_seed(self, temp_db):
        from app.db import repository
        owned = {c: {o["order_id"]: o for o in repository.list_customer_orders(c)} for c in ("C101", "C102", "C103", "C105")}
        assert "ORD1004" in owned["C102"] and "ORD1004" not in owned["C101"]
        assert "ORD1010" in owned["C105"] and "ORD1010" not in owned["C101"]
        assert owned["C105"]["ORD1010"]["status"] == "Returned" and not repository.get_customer_returns("C105")
        assert {"ORD1001", "ORD1002", "ORD1014"} == set(owned["C101"]) and {"ORD1003", "ORD1004", "ORD1015"} == set(owned["C102"])

    def test_the_runner_and_the_command_line_work_offline(self, tmp_path):
        report = ev.run_heldout(RuleBasedLLM, path=AFFECTED, set_name="affected", mode="real_backend_affected_behaviours",
                                note=ev.AFFECTED_NOTE)
        assert report["evaluation_mode"] == "real_backend_affected_behaviours" and set(report["sets"]) == {"affected"}
        assert report["manual_review"]["turns"] == 15 and "heldout" not in json.dumps(report["model_calls"])
        out = tmp_path / "a.json"
        assert ev.main(["--affected", "--output", str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["evaluation_mode"] == "real_backend_affected_behaviours"

    def test_the_language_switch_case_flags_a_hinglish_answer_to_the_english_message(self, tmp_path):
        ask = lambda item: LLMResult(tool_calls=(ToolCall(CHECK, {"order_id": "ORD1005", "item_id": item}, "e"),))
        good = [ask("IT106"), LLMResult(text="Ordinary period open until 12 November 2026, 12:00 UTC."),
                ask("IT105"), LLMResult(text="The ordinary return period is open until 12 November 2026, 12:00 UTC.")]
        bad = [ask("IT106"), LLMResult(text="Ordinary period 12 November 2026, 12:00 UTC tak khula hai."),
               ask("IT105"), LLMResult(text="Ordinary return period 12 November 2026, 12:00 UTC tak khula hai, kya aap "
                                              "return karna chahte hain?")]
        path = tmp_path / "l01.json"
        path.write_text(json.dumps([c for c in json.loads(AFFECTED.read_text(encoding="utf-8")) if c["id"] == "L01"]),
                        encoding="utf-8")
        with ev.environment(lambda: Sequence(*bad)) as env:
            turns = ev.run_conversations(path, env)["results"][0]["turns"]
        assert "language" in turns[1]["failed"]
        with ev.environment(lambda: Sequence(*good)) as env:
            turns = ev.run_conversations(path, env)["results"][0]["turns"]
        assert "language" not in turns[1]["failed"] and "states_times" not in turns[1]["failed"]


class TestE02Corrected:
    def test_it_no_longer_requires_the_optional_open_deadline(self):
        e02 = [c for c in json.loads((EVAL / "conversations_eligibility.json").read_text(encoding="utf-8"))
               if c["id"] == "E02"][0]["turns"][0]
        assert e02["expect"]["states_times"] == ["2026-11-12T12:00:00Z"]
        assert "optional under the approved spec" in e02["ground_truth"]
        assert "mentions_any" in e02["expect"]  # the limits are still required


# ---------------------------------------------------------------------------
# The retry runner
# ---------------------------------------------------------------------------

def make_runner(tmp_path, plan):
    """A fake real_eval: writes the report the plan says for each call and returns its exit code."""
    calls = []

    def run(cmd):
        out = Path(cmd[cmd.index("--output") + 1])
        calls.append(cmd)
        valid, total, code = plan[len(calls) - 1]
        out.write_text(json.dumps({"model_calls_total": {"chat": total, "complete": 0},
                                   "integrity": {"run_valid_for_scoring": valid,
                                                 "provider_error_replies": {"total": 0 if valid else 5}}}), encoding="utf-8")
        return code

    return run, calls


class TestRunValid:
    def go(self, tmp_path, plan, *, checks=None, budget=0, max_attempts=3):
        run, calls = make_runner(tmp_path, plan)
        sleeps = []
        queue = list(checks or [(True, "ok")] * 10)
        summary = run_valid.run_until_valid("orig", tmp_path / "out", ["--cases", "x.csv"], runner=["real_eval"],
                                            max_attempts=max_attempts, budget=budget, check=lambda: queue.pop(0),
                                            sleep=sleeps.append, run=run)
        return summary, calls, sleeps

    def test_a_valid_first_attempt_stops_there_without_waiting(self, tmp_path):
        summary, calls, sleeps = self.go(tmp_path, [(True, 100, 0)])
        assert summary["status"] == "valid" and len(calls) == 1 and sleeps == []
        assert (tmp_path / "out" / "orig_valid.json").exists() and (tmp_path / "out" / "orig_attempts.json").exists()

    def test_invalid_attempts_back_off_30_90_then_270(self, tmp_path):
        summary, calls, sleeps = self.go(tmp_path, [(False, 10, 3), (False, 10, 3), (False, 10, 3)], max_attempts=3)
        assert summary["status"] == "no_valid_run" and sleeps == [30, 90]  # no wait after the last attempt
        _, _, sleeps4 = self.go(tmp_path, [(False, 1, 3)] * 4, max_attempts=4)
        assert sleeps4 == [30, 90, 270]

    def test_a_failed_preflight_costs_no_calls_and_waits(self, tmp_path):
        summary, calls, sleeps = self.go(tmp_path, [(True, 100, 0)], checks=[(False, "dns: gaierror"), (True, "ok")])
        assert summary["status"] == "valid" and len(calls) == 1 and sleeps == [30]
        first = summary["attempts"][0]
        assert first["ran"] is False and first["calls"] == 0 and first["preflight"] == "dns: gaierror"

    def test_a_run_with_a_clean_report_but_a_nonzero_exit_is_not_accepted(self, tmp_path):
        summary, calls, sleeps = self.go(tmp_path, [(True, 10, 3), (True, 10, 0)])
        assert summary["status"] == "valid" and len(calls) == 2 and sleeps == [30]

    def test_every_attempt_is_recorded_with_its_calls_and_errors(self, tmp_path):
        summary, *_ = self.go(tmp_path, [(False, 40, 3), (True, 100, 0)])
        assert [a["calls"] for a in summary["attempts"]] == [40, 100] and summary["calls_attempted"] == 140
        assert summary["attempts"][0]["provider_error_replies"] == 5 and summary["attempts"][1]["valid"] is True
        history = json.loads((tmp_path / "out" / "orig_attempts.json").read_text(encoding="utf-8"))
        assert history["backoff_seconds"] == [30, 90, 270]

    def test_it_stops_before_an_attempt_that_could_exceed_the_budget(self, tmp_path):
        summary, calls, sleeps = self.go(tmp_path, [(False, 100, 1), (True, 100, 0)], budget=150)
        assert summary["status"] == "budget" and len(calls) == 1 and summary["calls_attempted"] == 100
        assert "exceed the call budget" in summary["attempts"][-1]["stopped"]

    def test_a_second_attempt_that_fits_the_budget_is_made(self, tmp_path):
        summary, calls, _ = self.go(tmp_path, [(False, 40, 3), (True, 100, 0)], budget=160)
        assert summary["status"] == "valid" and len(calls) == 2

    def test_the_stop_flag_and_the_output_are_added_to_every_attempt(self, tmp_path):
        _, calls, _ = self.go(tmp_path, [(False, 1, 3), (True, 1, 0)])
        assert all("--stop-after-provider-failures" in c and "3" in c and "--output" in c and "x.csv" in c for c in calls)
        assert calls[0][-1].endswith("orig_attempt1.json") and calls[1][-1].endswith("orig_attempt2.json")

    def test_the_preflight_reports_dns_and_connect_failures_without_a_request(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("no dns")))
        assert run_valid.preflight("api.example.test", 443) == (False, "dns: gaierror")
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [object()])
        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
        assert run_valid.preflight("api.example.test", 443) == (False, "connect: TimeoutError")

        class Open:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: Open())
        assert run_valid.preflight("api.example.test", 443) == (True, "ok")

    def test_the_host_comes_from_the_base_url(self):
        assert run_valid.api_host({}) == ("api.openai.com", 443)
        assert run_valid.api_host({"OPENAI_BASE_URL": "https://proxy.example:8443/v1"}) == ("proxy.example", 8443)

    def test_main_maps_the_outcome_to_an_exit_code_and_passes_the_arguments_after_the_dashes(self, tmp_path, monkeypatch):
        seen = {}

        def fake(label, out_dir, args, **kw):
            seen.update(label=label, args=list(args), budget=kw["budget"], backoff=list(kw["backoff"]))
            return {"label": label, "status": "budget", "calls_attempted": 5, "attempts": []}

        monkeypatch.setattr(run_valid, "run_until_valid", fake)
        code = run_valid.main(["--label", "L", "--out-dir", str(tmp_path), "--budget", "150", "--backoff", "1,2", "--",
                               "--affected", "--llm", "env"])
        assert code == 2 and seen["args"] == ["--affected", "--llm", "env"] and seen["budget"] == 150
        assert seen["backoff"] == [1.0, 2.0]
