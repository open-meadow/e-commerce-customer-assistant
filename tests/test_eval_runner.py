import csv
import json
from pathlib import Path

import pytest

from scripts.eval import run_eval


_FIELDS = [
    "id",
    "query",
    "intent",
    "category",
    "example_expected_action",
    "notes",
    "customer_id",
    "expected_behavior",
    "expected_tool",
]


def _write_cases(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(_FIELDS)
        writer.writerows(rows)
    return path


def _case(
    case_id: str = "Q001",
    *,
    query: str = "What is my order status?",
    category: str = "order_status",
    customer_id: str = "C101",
    behavior: str = "clarify",
    tool: str = "none",
) -> list[str]:
    return [
        case_id,
        query,
        category,
        category,
        "Expected synthetic behavior",
        "Test case",
        customer_id,
        behavior,
        tool,
    ]


def test_score_results_marks_unobservable_metrics_not_assessed():
    results = [
        {
            "category": "order_status",
            "expected_tool": "get_order_status",
        },
        {
            "category": "order_status",
            "expected_tool": "none",
        },
        {
            "category": "prompt_injection",
            "expected_tool": "none",
        },
    ]

    scores = run_eval.score_results(results)

    assert scores["answer_accuracy"]["assessed"] == 0
    assert scores["answer_accuracy"]["not_assessed"] == 3
    assert scores["answer_accuracy"]["rate"] is None
    assert scores["tool_call_correctness"]["expected_tool_cases"] == 1
    assert scores["tool_call_correctness"]["rate"] is None
    assert scores["hallucination_checks"]["status"] == "not_assessed"
    assert scores["safety_checks"]["status"] == "not_assessed"
    assert scores["per_category"]["order_status"]["case_count"] == 2
    assert scores["per_category"]["order_status"]["answer_accuracy"] is None
    assert all(
        result["scores"]["answer_accuracy"] == "not_assessed"
        for result in results
    )


def test_load_cases_rejects_malformed_rows_and_labels(tmp_path):
    cases = [
        ([_case("Q001")[:-1]], "non-empty fields"),
        ([_case("Q001", customer_id="C999")], "unsupported fake-backend customer"),
        ([_case("Q001", category="unknown")], "unknown category"),
        ([_case("Q001", behavior="unknown")], "unknown expected behavior"),
        ([_case("Q001", tool="run_sql")], "unknown expected tool"),
        ([_case("Q001"), _case("Q001")], "duplicate case ID"),
    ]
    for index, (rows, message) in enumerate(cases):
        cases_path = _write_cases(tmp_path / f"case-{index}.csv", rows)
        with pytest.raises(ValueError, match=message):
            run_eval.load_cases(cases_path)


def test_load_cases_rejects_incorrect_headers(tmp_path):
    cases_path = tmp_path / "cases.csv"
    with cases_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(_FIELDS[:-1])
        writer.writerow(_case("Q001")[:-1])
    with pytest.raises(ValueError, match="expected CSV columns"):
        run_eval.load_cases(cases_path)


def test_run_cases_uses_a_fresh_session_for_each_customer_case(
    tmp_path,
    monkeypatch,
):
    cases_path = _write_cases(
        tmp_path / "cases.csv",
        [
            _case("Q001", customer_id="C101"),
            _case("Q002", customer_id="C102"),
        ],
    )
    started: list[tuple[str, object]] = []
    handled: list[tuple[object, str]] = []
    real_start_session = run_eval.start_session
    real_handle_message = run_eval.handle_message

    def tracking_start_session(customer_id: str):
        result = real_start_session(customer_id)
        assert result.state is not None
        started.append((customer_id, result.state))
        return result

    def tracking_handle_message(state, *, message):
        handled.append((state, message))
        return real_handle_message(state, message=message)

    monkeypatch.setattr(run_eval, "start_session", tracking_start_session)
    monkeypatch.setattr(run_eval, "handle_message", tracking_handle_message)

    report = run_eval.run_cases(cases_path)

    assert [customer_id for customer_id, _ in started] == ["C101", "C102"]
    assert len({id(state) for _, state in started}) == 2
    assert [state.customer_id for _, state in started] == ["C101", "C102"]
    assert [message for _, message in handled] == [
        "What is my order status?",
        "What is my order status?",
    ]
    assert all(
        handled_state is started_state
        for (handled_state, _), (_, started_state) in zip(handled, started)
    )
    assert report["case_count"] == 2


def test_run_cases_reports_unavailable_answer_tool_hallucination_and_safety_scores(
    tmp_path,
):
    cases_path = _write_cases(
        tmp_path / "cases.csv",
        [
            _case(
                "Q001",
                category="cross_customer",
                behavior="deny_cross_customer",
                tool="get_order_status",
            )
        ],
    )

    report = run_eval.run_cases(cases_path)

    assert report["evaluation_mode"] == "demo_only"
    assert report["case_count"] == 1
    assert report["tool_calls_observed"] is False
    assert report["answer_accuracy_scored"] is False
    assert report["scores"]["answer_accuracy"]["status"] == "not_assessed"
    result = report["results"][0]
    assert result["scores"] == {
        "answer_accuracy": "not_assessed",
        "tool_call_correctness": "not_assessed",
        "hallucination_checks": "not_assessed",
        "safety_checks": "not_assessed",
    }
    assert report["scores"]["tool_call_correctness"]["rate"] is None
    assert report["scores"]["hallucination_checks"]["unsupported_fact_checks"] == "not_assessed"
    assert report["scores"]["hallucination_checks"]["action_claim_checks"] == "not_assessed"
    assert report["scores"]["safety_checks"]["cross_customer_isolation"] == "not_assessed"
    assert report["scores"]["safety_checks"]["prompt_injection_resistance"] == "not_assessed"
    assert report["scores"]["safety_checks"]["unsupported_action_handling"] == "not_assessed"
    assert report["scores"]["safety_checks"]["out_of_scope_false_rejection"] == "not_assessed"


def test_sample_logs_are_linked_to_real_demo_runner_output():
    cases_path = Path("app/db/data/eval/queries.csv")
    logs_path = Path("app/db/data/eval/logs/sample_conversations.jsonl")
    cases = {case["id"]: case for case in run_eval.load_cases(cases_path)}
    report = run_eval.run_cases(cases_path)
    results = {result["case_id"]: result for result in report["results"]}
    logs = [
        json.loads(line)
        for line in logs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert {log["case_id"] for log in logs} == {"Q002", "Q016", "Q005", "Q059"}
    for log in logs:
        case_id = log["case_id"]
        case = cases[case_id]
        result = results[case_id]
        assert log["mode"] == "demo_only"
        assert log["backend"] == "app.ui.fake_backend"
        assert log["customer_id"] == case["customer_id"]
        assert log["expected_behavior"] == case["expected_behavior"]
        assert log["expected_tool"] == case["expected_tool"]
        assert log["observed_tool_calls"] is None
        assert log["assessment"] == "not_assessed"
        assert log["turns"][0] == {"role": "user", "content": case["query"]}
        assert log["turns"][1]["content"] == result["reply"]["text"]
        assert log["turns"][1]["status"] == result["reply"]["status"]
