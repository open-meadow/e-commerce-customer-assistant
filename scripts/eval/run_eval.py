"""Run demo-only cases against the fake UI backend.

This runner captures replies for smoke-testing the evaluation data and case
isolation. It does not evaluate the real assistant or observe tool calls.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.ui.fake_backend import (
    ChatReply,
    handle_message,
    list_login_options,
    start_session,
)

_DEFAULT_CASES = _PROJECT_ROOT / "app" / "db" / "data" / "eval" / "queries.csv"
_FIELDS = (
    "id",
    "query",
    "intent",
    "category",
    "example_expected_action",
    "notes",
    "customer_id",
    "expected_behavior",
    "expected_tool",
)
_CATEGORIES = {
    "order_status",
    "delivery",
    "payment",
    "returns",
    "refunds",
    "product",
    "policy",
    "cancellation",
    "address_change",
    "warranty",
    "complaint",
    "escalation",
    "cross_customer",
    "prompt_injection",
    "out_of_scope",
    "ambiguous",
    "language_robustness",
}
_BEHAVIORS = {
    "answer",
    "clarify",
    "offer_escalation",
    "require_confirmation",
    "deny_cross_customer",
    "unsupported_action",
    "out_of_scope",
    "prompt_injection_resistant",
}
_TOOLS = {
    "none",
    "list_my_orders",
    "get_order_status",
    "get_refund_policy",
    "lookup_product",
    "start_return_request",
    "start_support_ticket",
}


def load_cases(
    path: Path, known_customers: "set[str] | None" = None
) -> list[dict[str, str]]:
    """Load cases and reject malformed rows or labels.

    ``known_customers`` defaults to the fake backend's customers; the real-backend
    evaluation passes the customers of its own seeded database instead.
    """
    try:
        with path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames != list(_FIELDS):
                raise ValueError(
                    f"{path}: expected CSV columns {','.join(_FIELDS)}"
                )
            rows = list(reader)
    except (OSError, csv.Error) as exc:
        raise ValueError(f"could not read evaluation cases at {path}: {exc}") from exc

    if not rows:
        raise ValueError(f"{path}: no evaluation cases found")

    if known_customers is None:
        known_customers = {option.customer_id for option in list_login_options()}
    seen_ids: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        if None in row or any(not value or not value.strip() for value in row.values()):
            raise ValueError(f"{path}:{line_number}: expected {len(_FIELDS)} non-empty fields")
        case_id = row["id"]
        if case_id in seen_ids:
            raise ValueError(f"{path}:{line_number}: duplicate case ID {case_id!r}")
        seen_ids.add(case_id)
        if row["customer_id"] not in known_customers:
            raise ValueError(
                f"{path}:{line_number}: unsupported fake-backend customer "
                f"{row['customer_id']!r}"
            )
        if row["category"] not in _CATEGORIES:
            raise ValueError(
                f"{path}:{line_number}: unknown category {row['category']!r}"
            )
        if row["expected_behavior"] not in _BEHAVIORS:
            raise ValueError(
                f"{path}:{line_number}: unknown expected behavior "
                f"{row['expected_behavior']!r}"
            )
        if row["expected_tool"] not in _TOOLS:
            raise ValueError(
                f"{path}:{line_number}: unknown expected tool "
                f"{row['expected_tool']!r}"
            )

    return rows


def _reply_data(reply: ChatReply) -> dict[str, Any]:
    """Serialize stable reply fields, omitting random IDs and expiry times."""
    data: dict[str, Any] = {
        "text": reply.text,
        "status": reply.status,
        "error_code": reply.error_code,
        "citations": [
            {"source": citation.source, "section": citation.section}
            for citation in reply.citations
        ],
    }
    if reply.pending is not None:
        data["pending"] = {
            "kind": reply.pending.kind,
            "summary": reply.pending.summary,
        }
    else:
        data["pending"] = None
    if reply.choice is not None:
        data["choice"] = {
            "question": reply.choice.question,
            "options": [
                {"option_id": option.option_id, "label": option.label}
                for option in reply.choice.options
            ],
        }
    else:
        data["choice"] = None
    return data


def score_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize evidence without scoring behavior the fake backend cannot prove."""
    category_counts: dict[str, int] = {}
    expected_tool_counts: dict[str, int] = {}
    for result in results:
        category = result["category"]
        category_counts[category] = category_counts.get(category, 0) + 1
        expected_tool = result["expected_tool"]
        expected_tool_counts[expected_tool] = (
            expected_tool_counts.get(expected_tool, 0) + 1
        )

        result["scores"] = {
            "answer_accuracy": "not_assessed",
            "tool_call_correctness": "not_assessed",
            "hallucination_checks": "not_assessed",
            "safety_checks": "not_assessed",
        }

    unavailable = {
        "status": "not_assessed",
        "assessed": 0,
        "not_assessed": len(results),
        "rate": None,
    }
    return {
        "answer_accuracy": {
            **unavailable,
            "reason": (
                "The fake backend returns canned demo text; it cannot measure "
                "semantic answer correctness."
            ),
        },
        "tool_call_correctness": {
            **unavailable,
            "expected_tool_cases": sum(
                count
                for tool, count in expected_tool_counts.items()
                if tool != "none"
            ),
            "expected_tool_counts": expected_tool_counts,
            "reason": "The fake backend exposes no observed tool-call trace.",
        },
        "hallucination_checks": {
            "status": "not_assessed",
            "unsupported_fact_checks": "not_assessed",
            "action_claim_checks": "not_assessed",
            "reason": (
                "Reply text cannot be checked against real tool results or a "
                "ground-truth response from the fake backend."
            ),
        },
        "safety_checks": {
            "status": "not_assessed",
            "cross_customer_isolation": "not_assessed",
            "prompt_injection_resistance": "not_assessed",
            "unsupported_action_handling": "not_assessed",
            "out_of_scope_false_rejection": "not_assessed",
            "reason": (
                "The demo backend has no real customer-data tools or scope "
                "guard to evaluate."
            ),
        },
        "per_category": {
            category: {
                "case_count": count,
                "answer_accuracy": None,
                "tool_call_correctness": None,
                "status": "not_assessed",
            }
            for category, count in sorted(category_counts.items())
        },
    }


def run_cases(path: Path) -> dict[str, Any]:
    """Run each case in a fresh fake-backend session and return raw results."""
    cases = load_cases(path)
    results: list[dict[str, Any]] = []
    for case in cases:
        session = start_session(case["customer_id"])
        if not session.ok or session.state is None:
            raise RuntimeError(
                f"could not start isolated session for case {case['id']}: "
                f"{session.error_code or 'unknown error'}"
            )
        reply = handle_message(session.state, message=case["query"])
        results.append(
            {
                "case_id": case["id"],
                "customer_id": case["customer_id"],
                "query": case["query"],
                "category": case["category"],
                "expected_behavior": case["expected_behavior"],
                "expected_tool": case["expected_tool"],
                "reply": _reply_data(reply),
                "observed_tool_calls": None,
                "tool_call_observability": "unavailable_in_fake_backend",
            }
        )

    scores = score_results(results)
    return {
        "evaluation_mode": "demo_only",
        "backend": "app.ui.fake_backend",
        "tool_calls_observed": False,
        "answer_accuracy_scored": False,
        "case_count": len(results),
        "scores": scores,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        type=Path,
        default=_DEFAULT_CASES,
        help=f"CSV case file (default: {_DEFAULT_CASES})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write JSON results to this path instead of standard output",
    )
    args = parser.parse_args()

    try:
        report = run_cases(args.cases)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
