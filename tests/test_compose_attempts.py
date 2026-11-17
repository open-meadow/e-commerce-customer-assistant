"""Composing attempts: first clean result per test, original denominators, full disclosure. Offline (rule-based model)."""

import copy
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import compose_attempts as ca  # noqa: E402
import real_eval as ev  # noqa: E402
import transport_retry as tr  # noqa: E402
from app.llm.rule_based import RuleBasedLLM  # noqa: E402

EVAL = Path(__file__).resolve().parents[1] / "app" / "db" / "data" / "eval"
CASES = EVAL / "queries_conversational.csv"
CONVERSATIONS = EVAL / "conversations.json"


@pytest.fixture(scope="module")
def clean():
    return ev.run(CASES, RuleBasedLLM, conversations_path=CONVERSATIONS)


def keep_all_except(report, cases=(), conversations=(), scenarios=()):
    return {"cases": {c["case_id"]: "verified clean" for c in report["results"] if c["case_id"] not in cases},
            "conversations": {c["id"]: "verified clean" for c in report["conversations"]["results"]
                              if c["id"] not in conversations},
            "scenarios": {s["scenario"]: "verified clean" for s in report["scenarios"] if s["scenario"] not in scenarios}}


def damaged(report, cases=(), conversations=(), scenarios=()):
    out = copy.deepcopy(report)
    for case in out["results"]:
        if case["case_id"] in cases:
            case["reply"]["status"], case["reply"]["error_code"] = "error", "unavailable"
    for convo in out["conversations"]["results"]:
        if convo["id"] in conversations:
            convo["turns"][0]["reply"]["status"] = "error"
    for scenario in out["scenarios"]:
        if scenario["scenario"] in scenarios:
            scenario["steps"][0]["status"] = "error"
    return out


class TestUncleanOwners:
    def test_a_clean_report_has_none(self, clean):
        assert all(not v for v in ca.unclean_owners(clean).values())

    def test_error_replies_are_found_in_cases_conversations_and_scenarios(self, clean):
        found = ca.unclean_owners(damaged(clean, cases=("C011",), conversations=("M08",), scenarios=("ticket_declined",)))
        assert found == {"cases": {"C011"}, "conversations": {"M08"}, "scenarios": {"ticket_declined"}}

    def test_a_failure_that_made_no_error_reply_is_found_through_the_transport_record(self, clean):
        report = copy.deepcopy(clean)
        report["integrity"]["transport_affected_tests"] = ["conv:M10", "scenario:return_confirmed", "case:C003"]
        assert ca.unclean_owners(report) == {"cases": {"C003"}, "conversations": {"M10"},
                                              "scenarios": {"return_confirmed"}}


class TestCompose:
    def test_keeping_every_test_reproduces_the_original_denominators_and_scores(self, clean):
        composite = ca.compose_original(clean, "base.json", keep_all_except(clean), [])
        assert composite["composition"]["complete"] and composite["case_count"] == 30
        assert len(composite["conversations"]["results"]) == 12 and composite["scenario_count"] == 5
        assert composite["summary"] == clean["summary"]
        assert composite["conversations"]["summary"] == clean["conversations"]["summary"]

    def test_damaged_tests_are_taken_from_a_clean_replay_and_the_rest_from_the_base(self, clean):
        base = damaged(clean, cases=("C011", "C012"), conversations=("M08",), scenarios=("ticket_declined",))
        keep = keep_all_except(clean, ("C011", "C012"), ("M08",), ("ticket_declined",))
        composite = ca.compose_original(base, "base.json", keep, [("replay1.json", clean)])
        per_test = composite["composition"]["per_test"]
        assert per_test["cases"]["C011"]["used"] == "replay1.json" and per_test["cases"]["C001"]["used"] == "base.json"
        assert per_test["conversations"]["M08"]["used"] == "replay1.json"
        assert per_test["scenarios"]["ticket_declined"]["used"] == "replay1.json"
        assert composite["composition"]["complete"] and composite["summary"] == clean["summary"]
        assert composite["integrity"]["provider_error_replies"]["total"] == 0
        # the set-aside attempt is disclosed, with the reason
        assert per_test["cases"]["C011"]["attempts"][0]["accepted"] is False

    def test_the_first_clean_attempt_wins_and_a_damaged_replay_is_skipped(self, clean):
        base = damaged(clean, cases=("C011",))
        bad_replay = damaged(clean, cases=("C011",))
        composite = ca.compose_original(base, "base.json", keep_all_except(clean, ("C011",)),
                                        [("r1.json", bad_replay), ("r2.json", clean), ("r3.json", clean)])
        entry = composite["composition"]["per_test"]["cases"]["C011"]
        assert entry["used"] == "r2.json"
        assert [a["accepted"] for a in entry["attempts"]] == [False, False, True, True]

    def test_a_test_with_no_clean_result_is_reported_missing_and_the_report_is_not_complete(self, clean):
        base = damaged(clean, conversations=("M08",))
        composite = ca.compose_original(base, "base.json", keep_all_except(clean, conversations=("M08",)), [])
        assert composite["composition"]["complete"] is False
        assert composite["composition"]["missing"]["conversations"] == ["M08"]
        assert len(composite["conversations"]["results"]) == 11  # never filled in with a damaged result

    def test_nothing_is_accepted_from_the_base_unless_the_keep_list_names_it(self, clean):
        composite = ca.compose_original(clean, "base.json", {"cases": {}, "conversations": {}, "scenarios": {}},
                                        [("r1.json", clean)])
        assert all(v["used"] == "r1.json" for kind in composite["composition"]["per_test"].values()
                   for v in kind.values())

    def test_inputs_are_not_modified_and_the_disclosure_and_call_totals_are_present(self, clean):
        base, replay = damaged(clean, cases=("C011",)), copy.deepcopy(clean)
        before = (copy.deepcopy(base), copy.deepcopy(replay))
        composite = ca.compose_original(base, "base.json", keep_all_except(clean, ("C011",)), [("r1.json", replay)])
        assert (base, replay) == before
        assert "COMBINES results from several attempts" in composite["composition"]["disclosure"]
        names = [a["name"] for a in composite["composition"]["attempts"]]
        assert names == ["base.json", "r1.json"]
        total = composite["composition"]["model_calls_total_all_attempts"]
        assert total["chat"] == 2 * clean["model_calls_total"]["chat"]


class TestFilesAndCli:
    def test_the_replay_subset_files_hold_only_the_requested_tests_unchanged(self, tmp_path):
        cases, convos = ca.subset_files(CASES, CONVERSATIONS, {"cases": ["C011", "C014"], "conversations": ["M08"]},
                                        tmp_path)
        assert cases.read_text(encoding="utf-8").count("\nC0") == 2
        loaded = json.loads(convos.read_text(encoding="utf-8"))
        original = [c for c in json.loads(CONVERSATIONS.read_text(encoding="utf-8")) if c["id"] == "M08"]
        assert loaded == original

    def test_the_command_line_composes_and_signals_incompleteness(self, tmp_path, clean, capsys):
        base_path, keep_path, out = tmp_path / "base.json", tmp_path / "keep.json", tmp_path / "out.json"
        base_path.write_text(json.dumps(damaged(clean, cases=("C011",))), encoding="utf-8")
        keep_path.write_text(json.dumps(keep_all_except(clean, ("C011",))), encoding="utf-8")
        assert ca.main(["compose", "--base", str(base_path), "--keep", str(keep_path), "--out", str(out)]) == 1
        replay = tmp_path / "replay.json"
        replay.write_text(json.dumps(clean), encoding="utf-8")
        assert ca.main(["compose", "--base", str(base_path), "--keep", str(keep_path), "--replay", str(replay),
                        "--out", str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["composition"]["complete"] is True


class TestOwnerAttribution:
    def test_a_failure_is_attributed_to_the_test_that_was_running(self, monkeypatch):
        failure = urllib.error.URLError(TimeoutError("timed out"))
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(failure))
        with tr.retrying(2, sleep=lambda s: None, probe=lambda h: True) as collector:
            with tr.owner("conv:M10"):
                with pytest.raises(urllib.error.URLError):
                    urllib.request.urlopen(urllib.request.Request("https://api.example.test/v1/responses", data=b"x"))
        assert collector.incidents[0]["owner"] == "conv:M10" and collector.incidents[0]["outcome"] == "not_retried"
        report = {"integrity": {"run_valid_for_scoring": True}}
        tr.annotate(report, collector)
        assert report["integrity"]["transport_affected_tests"] == ["conv:M10"]

    def test_a_recovered_failure_does_not_mark_the_test_affected(self, monkeypatch):
        outcomes = [urllib.error.URLError(__import__("socket").gaierror(11001, "x")), object()]

        def fake(*a, **k):
            item = outcomes.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        with tr.retrying(2, sleep=lambda s: None, probe=lambda h: True) as collector:
            with tr.owner("case:C013"):
                urllib.request.urlopen(urllib.request.Request("https://api.example.test/v1/responses", data=b"x"))
        assert collector.incidents[0]["outcome"] == "recovered"
        assert collector.summary()["tests_with_unrecovered_transport_failure"] == []

    def test_the_harness_sets_the_owner_for_cases_conversations_and_scenarios(self, monkeypatch):
        seen = set()
        original = ev.RecordingLLM.chat

        def spy(self, *args, **kwargs):
            seen.add(tr.OWNER.get())
            return original(self, *args, **kwargs)

        monkeypatch.setattr(ev.RecordingLLM, "chat", spy)
        ev.run(CASES, RuleBasedLLM, conversations_path=CONVERSATIONS)
        assert "case:C001" in seen and "conv:M01" in seen and any(o and o.startswith("scenario:") for o in seen)
