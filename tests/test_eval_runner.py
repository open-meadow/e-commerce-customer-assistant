import csv

from scripts.eval.run_eval import run_cases, score_results


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

    scores = score_results(results)

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


def test_run_cases_reports_demo_replies_without_claiming_scores(tmp_path):
    cases_path = tmp_path / "cases.csv"
    with cases_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
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
        )
        writer.writerow(
            [
                "Q001",
                "What is my order status?",
                "order_status",
                "order_status",
                "Ask for an order ID",
                "No order ID supplied",
                "C101",
                "clarify",
                "none",
            ]
        )
        writer.writerow(
            [
                "Q002",
                "How can I return an item?",
                "return_process",
                "returns",
                "Explain the demo return flow",
                "Fake backend only",
                "C102",
                "clarify",
                "none",
            ]
        )

    report = run_cases(cases_path)

    assert report["evaluation_mode"] == "demo_only"
    assert report["case_count"] == 2
    assert report["tool_calls_observed"] is False
    assert report["answer_accuracy_scored"] is False
    assert report["scores"]["answer_accuracy"]["status"] == "not_assessed"
    assert [
        result["customer_id"] for result in report["results"]
    ] == ["C101", "C102"]
    assert all(result["observed_tool_calls"] is None for result in report["results"])
    assert all(
        result["scores"]["safety_checks"] == "not_assessed"
        for result in report["results"]
    )
