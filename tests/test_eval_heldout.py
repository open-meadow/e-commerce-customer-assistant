"""The held-out set: paraphrased, unseen cases kept apart from every development set.

Why it exists: the 30 cases, the 12 conversations, the 15 identity and eligibility cases and the 70-case file have been
read, run and tuned against many times, so passing them again proves little. These cases were written before the
corrective changes, use different wording, and are run once for a baseline and once after the change; they are never
used to choose a wording. Offline, scripted and stand-in models only. No network.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import real_eval as ev  # noqa: E402
import app.backend  # noqa: E402,F401
from app.llm.rule_based import RuleBasedLLM  # noqa: E402
from app.tools.context import CustomerContext  # noqa: E402
from app.tools.registry import execute_tool  # noqa: E402

EVAL = Path(__file__).resolve().parents[1] / "app" / "db" / "data" / "eval"
HELDOUT = EVAL / "heldout" / "heldout_v1.json"
CUSTOMERS = {f"C10{i}" for i in range(1, 7)}
CHECK = "check_return_eligibility"


def cases():
    return json.loads(HELDOUT.read_text(encoding="utf-8"))


def messages(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {t["say"].strip().lower() for c in data for t in c["turns"]}


class TestComposition:
    def test_it_loads_with_the_agreed_composition(self):
        loaded = ev.load_conversations(HELDOUT, CUSTOMERS)
        assert [c["id"] for c in loaded] == [f"H{i:02d}" for i in range(1, 27)]
        assert all(len(c["turns"]) == 1 for c in loaded)

    def test_the_clusters_are_covered_with_controls(self):
        categories = [c["category"] for c in cases()]
        for prefix, count in (("heldout_human_request", 4), ("heldout_product", 4),
                              ("heldout_other_person_named_order", 2), ("heldout_other_customer_generic", 2),
                              ("heldout_replacement", 3), ("heldout_mixed_return_replacement", 1),
                              ("heldout_eligibility", 5), ("heldout_return_status", 2), ("heldout_control", 3)):
            assert sum(1 for c in categories if c.startswith(prefix)) == count, prefix

    def test_every_case_states_what_is_right_and_what_is_wrong(self):
        for c in cases():
            truth = c["turns"][0]["ground_truth"]
            assert len(truth) > 60 and ("Right" in truth or "Delivered" in truth or "ORD" in truth), c["id"]
            assert "WRONG" in truth, c["id"]

    def test_there_is_a_hinglish_case_for_each_language_sensitive_cluster(self):
        hinglish = {c["id"] for c in cases() if c["turns"][0]["expect"].get("language") == "hinglish"}
        assert {"H02", "H06", "H14"} <= hinglish

    def test_english_cases_carry_the_language_expectation_that_catches_drift(self):
        english = {c["id"] for c in cases() if c["turns"][0]["expect"].get("language") == "en"}
        assert {"H17", "H18", "H19", "H20", "H21", "H24", "H25"} <= english


class TestSeparation:
    def test_the_file_lives_in_its_own_directory(self):
        assert HELDOUT.parent.name == "heldout" and HELDOUT.parent != ev._DEFAULT_IDENTITY.parent

    def test_no_message_is_a_copy_of_a_development_message(self):
        held = messages(HELDOUT)
        dev: set[str] = set()
        for name in ("conversations.json", "conversations_identity.json", "conversations_eligibility.json"):
            dev |= messages(EVAL / name)
        for name in ("queries_conversational.csv", "queries.csv"):
            with open(EVAL / name, encoding="utf-8", newline="") as handle:
                dev |= {row["query"].strip().lower() for row in csv.DictReader(handle)}
        assert not held & dev

    def test_no_id_collides_with_a_development_id(self):
        ids = {c["id"] for c in cases()}
        dev_ids = set()
        for name in ("conversations.json", "conversations_identity.json", "conversations_eligibility.json"):
            dev_ids |= {c["id"] for c in json.loads((EVAL / name).read_text(encoding="utf-8"))}
        assert not ids & dev_ids

    def test_the_development_runners_never_read_it(self):
        report = ev.run_new_sets(RuleBasedLLM)
        assert set(report["sets"]) == {"identity", "eligibility"}
        import inspect
        for runner in (ev.run, ev.run_new_sets):
            assert "heldout" not in inspect.getsource(runner).lower()


class TestDataAgreesWithTheSeededDatabase:
    def test_expected_closing_times_equal_what_the_tool_returns(self, temp_db):
        checked = 0
        for convo in cases():
            expect = convo["turns"][0]["expect"]
            if "states_times" not in expect:
                continue
            args = expect["tool_args"][CHECK]
            result = execute_tool(CustomerContext(convo["customer_id"], "ref"), CHECK, args)
            assert result.status == "ok", convo["id"]
            closes = {w["closes_at_utc"] for w in json.loads(result.to_json())["data"]["windows"].values()}
            assert set(expect["states_times"]) <= closes, convo["id"]
            checked += 1
        assert checked == 5

    def test_the_named_orders_of_other_people_really_belong_to_someone_else(self, temp_db):
        from app.db import repository
        owned = {c: {o["order_id"] for o in repository.list_customer_orders(c)} for c in CUSTOMERS}
        assert "ORD1005" in owned["C103"] and "ORD1005" not in owned["C101"]
        assert "ORD1010" in owned["C105"] and "ORD1010" not in owned["C102"]
        assert {"ORD1005", "ORD1006"} <= owned["C103"]
        assert {"ORD1001", "ORD1003", "ORD1005", "ORD1010"} <= set().union(*owned.values())

    def test_the_prices_in_the_product_cases_are_the_catalog_prices(self):
        products = {p["id"]: p["price_inr"] for p in json.loads(
            (EVAL.parent / "source" / "Products.json").read_text(encoding="utf-8"))}
        assert (products["P1003"], products["P1002"], products["P1010"]) == (1999, 3799, 2499)

    def test_the_status_cases_match_the_seed(self, temp_db):
        from app.db import repository
        assert not repository.get_customer_returns("C101")  # H23: nothing on file
        assert {o["order_id"]: o["status"] for o in repository.list_customer_orders("C105")}["ORD1010"] == "Returned"


class TestRunner:
    def test_the_offline_run_reports_counts_only_and_demands_manual_review(self):
        report = ev.run_heldout(RuleBasedLLM)
        assert report["evaluation_mode"] == "real_backend_heldout"
        assert set(report["sets"]) == {"heldout"} and report["sets"]["heldout"]["summary"]["conversations_total"] == 26
        assert "scorecards" not in report and "summary" not in report
        assert report["manual_review"]["required"] is True and report["manual_review"]["turns"] == 26
        assert report["manual_review"]["reviewed"] == 0 and "can never certify" in report["manual_review"]["rule"]
        assert "never used to tune" in report["note"]
        assert report["integrity"]["run_valid_for_scoring"] is True

    def test_call_counts_and_tool_arguments_are_recorded(self):
        report = ev.run_heldout(RuleBasedLLM)
        assert report["model_calls"]["heldout"]["chat"] > 0
        with_tools = [i for i in report["review_sheet"] if i["tool_calls"]]
        assert with_tools and all({"name", "arguments"} <= set(c) for i in with_tools for c in i["tool_calls"])

    def test_every_sheet_entry_carries_its_ground_truth_and_no_verdict(self):
        sheet = ev.run_heldout(RuleBasedLLM)["review_sheet"]
        assert len(sheet) == 26 and all(i["manual_verdict"] is None for i in sheet)
        assert all(i["ground_truth"] != "(none recorded)" and i["set"] == "heldout" for i in sheet)

    def test_the_command_line_writes_a_separate_report(self, tmp_path):
        out = tmp_path / "h.json"
        assert ev.main(["--heldout", "--output", str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["evaluation_mode"] == "real_backend_heldout"

    def test_a_custom_file_can_be_given(self, tmp_path):
        small = tmp_path / "one.json"
        small.write_text(json.dumps(cases()[:1]), encoding="utf-8")
        out = tmp_path / "h.json"
        assert ev.main(["--heldout", str(small), "--output", str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["manual_review"]["turns"] == 1

    def test_the_review_sheet_renders_for_a_held_out_report(self, tmp_path, capsys):
        out = tmp_path / "h.json"
        ev.main(["--heldout", "--output", str(out)])
        assert ev.main(["--review-sheet", str(out)]) == 0
        printed = capsys.readouterr().out
        assert printed.count("Manual verdict") == 26 and "## H01" in printed

    def test_a_provider_failure_makes_a_held_out_run_invalid(self):
        from app.llm.base import LLMError
        from backend_helpers import ScriptedLLM

        def boom(system, messages, tools):
            raise LLMError("provider failed", category="timeout")

        llm = ScriptedLLM(chat=boom)
        llm.interprets_language = True
        assert ev.run_heldout(lambda: llm)["integrity"]["run_valid_for_scoring"] is False
