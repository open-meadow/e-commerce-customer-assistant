"""Combine several attempts of the original evaluation set into one report, keeping the FIRST VALID result per test.

Why: a live run is only scored when no test lost a reply to a transport failure. On an unstable network a full run almost
never finishes clean, so a run is composed instead: results that completed cleanly are kept, and only the tests whose
attempt was damaged by a transport failure are replayed from a fresh context, then the first clean result of each test is
taken. The criteria, expected outcomes and denominators are unchanged: the same 30 cases, 12 conversations and 5 scenarios
are scored by the harness's own functions. The report says plainly that it combines attempts and lists, per test, which
attempt supplied it and which earlier attempts were set aside and why.

Replay safety: single-turn cases and conversations never write (a write needs a confirmation decision, which only the
scenarios send). Every scenario runs on its own throwaway database, so a replayed scenario starts from a fresh one. A step
is never replayed on its own: only whole conversations and whole scenarios.
"""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
from typing import Any, Optional, Sequence

import real_eval as ev

ERROR = "error"


def unclean_owners(report: dict[str, Any]) -> dict[str, set[str]]:
    """Tests a report cannot vouch for: an error reply, or an unrecovered transport failure attributed to them."""
    out: dict[str, set[str]] = {"cases": set(), "conversations": set(), "scenarios": set(), "diagnostics": set()}
    for case in report.get("results", []):
        if case["reply"]["status"] == ERROR:
            out["cases"].add(case["case_id"])
    for convo in (report.get("conversations") or {}).get("results", []):
        if any(t["reply"]["status"] == ERROR for t in convo["turns"]):
            out["conversations"].add(convo["id"])
    for named_set in (report.get("sets") or {}).values():  # the identity and eligibility sets of a --new-sets report
        for convo in named_set.get("results", []):
            if any(t["reply"]["status"] == ERROR for t in convo["turns"]):
                out["conversations"].add(convo["id"])
    for diagnostic in report.get("diagnostics", []):
        if diagnostic.get("error") or any(s.get("status") == ERROR for s in diagnostic.get("steps", [])):
            out["diagnostics"].add(diagnostic["diagnostic"])
    for scenario in report.get("scenarios", []):
        if scenario.get("error") or any(s.get("status") == ERROR for s in scenario.get("steps", [])):
            out["scenarios"].add(scenario["scenario"])
    for label in (report.get("integrity") or {}).get("transport_affected_tests", []):
        kind, _, name = label.partition(":")
        key = {"case": "cases", "conv": "conversations", "scenario": "scenarios", "diagnostic": "diagnostics"}.get(kind)
        if key:
            out[key].add(name)
    return out


def subset_files(cases_csv: Path, conversations_json: Path, owners: dict[str, Sequence[str]],
                 out_dir: Path) -> tuple[Path, Path]:
    """Write the cases and conversations to replay, unchanged, in their original order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted_cases, wanted_convos = set(owners.get("cases", ())), set(owners.get("conversations", ()))
    with open(cases_csv, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [row for row in reader if row.get("id") in wanted_cases]
        fields = reader.fieldnames or []
    cases_out = out_dir / "replay_cases.csv"
    with open(cases_out, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    convos = [c for c in json.loads(conversations_json.read_text(encoding="utf-8")) if c["id"] in wanted_convos]
    convos_out = out_dir / "replay_conversations.json"
    convos_out.write_text(json.dumps(convos, ensure_ascii=False, indent=1), encoding="utf-8")
    return cases_out, convos_out


def compose_original(base: dict[str, Any], base_name: str, keep: dict[str, Any],
                     replays: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Compose the original-set report. ``keep`` lists the base tests accepted as clean, each with the evidence for it:
    ``{"cases": {id: why}, "conversations": {id: why}, "scenarios": {name: why}}``."""
    case_ids = [c["case_id"] for c in base["results"]]
    convo_ids = [c["id"] for c in base["conversations"]["results"]]
    scenario_names = [s["scenario"] for s in base["scenarios"]]
    sources: dict[str, dict[str, Any]] = {name: report for name, report in [(base_name, base), *replays]}
    unclean = {name: unclean_owners(report) for name, report in sources.items()}

    def lookup(kind: str, ident: str):
        if kind == "cases":
            return lambda r: next((c for c in r["results"] if c["case_id"] == ident), None)
        if kind == "conversations":
            return lambda r: next((c for c in (r.get("conversations") or {}).get("results", []) if c["id"] == ident), None)
        return lambda r: next((s for s in r.get("scenarios", []) if s["scenario"] == ident), None)

    chosen: dict[str, dict[str, tuple[str, Any]]] = {"cases": {}, "conversations": {}, "scenarios": {}}
    log: dict[str, dict[str, Any]] = {"cases": {}, "conversations": {}, "scenarios": {}}
    for kind, ids in (("cases", case_ids), ("conversations", convo_ids), ("scenarios", scenario_names)):
        for ident in ids:
            attempts = []
            for name, report in sources.items():
                found = lookup(kind, ident)(report)
                if found is None:
                    continue
                if name == base_name:
                    accepted = ident in keep.get(kind, {})
                    why = keep.get(kind, {}).get(ident) if accepted else "not accepted as clean (transport-affected or unverifiable)"
                else:
                    accepted = ident not in unclean[name][kind]
                    why = "clean" if accepted else "transport failure in this attempt"
                attempts.append({"attempt": name, "accepted": accepted, "note": why})
                if accepted and ident not in chosen[kind]:
                    chosen[kind][ident] = (name, copy.deepcopy(found))
            log[kind][ident] = {"used": chosen[kind].get(ident, (None,))[0], "attempts": attempts}

    missing = {k: [i for i, v in log[k].items() if v["used"] is None] for k in log}
    results = [chosen["cases"][i][1] for i in case_ids if i in chosen["cases"]]
    convo_results = [chosen["conversations"][i][1] for i in convo_ids if i in chosen["conversations"]]
    scenarios = [chosen["scenarios"][n][1] for n in scenario_names if n in chosen["scenarios"]]
    conversations = {"case_source": base["conversations"]["case_source"], "results": convo_results,
                     "summary": ev.summarize_conversations(convo_results)}
    complete = not any(missing.values())
    totals = {"chat": 0, "complete": 0}
    for report in sources.values():
        for k in totals:
            totals[k] += (report.get("model_calls_total") or {}).get(k, 0)
    return {
        "schema_version": base.get("schema_version"), "evaluation_mode": "real_backend_composite_of_attempts",
        "composition": {
            "disclosure": ("This report COMBINES results from several attempts. Each test keeps its first clean result; "
                           "tests damaged by a transport failure were replayed from a fresh context (conversations "
                           "whole, scenarios on a fresh database). Criteria, expected outcomes and denominators are "
                           "those of the original set. Network failures are reported here, apart from chatbot results."),
            "complete": complete, "missing": missing,
            "attempts": [{"name": name, "role": "base" if name == base_name else "replay",
                          "model_calls_total": report.get("model_calls_total"),
                          "unclean_tests": {k: sorted(v) for k, v in unclean[name].items()},
                          "transport_retries": {k: v for k, v in (report.get("transport_retries") or {}).items()
                                                if k in ("logical_requests", "attempts_total", "retry_attempts",
                                                         "not_retried_failures_by_type", "retry_reasons",
                                                         "tests_with_unrecovered_transport_failure")}}
                         for name, report in sources.items()],
            "per_test": log,
            "model_calls_total_all_attempts": totals,
        },
        "backend": base.get("backend"), "llm": base.get("llm"), "layers": base.get("layers"),
        "database": base.get("database"), "retrieval": base.get("retrieval"), "case_source": base.get("case_source"),
        "case_count": len(results), "scenario_count": len(scenarios),
        "integrity": ev.integrity(results, [conversations], scenarios),
        "summary": ev.summarize(results, scenarios, conversations), "limitations": list(ev.LIMITATIONS),
        "results": results, "scenarios": scenarios, "conversations": conversations,
    }


def compose_new_sets(base: dict[str, Any], base_name: str, keep: dict[str, Any],
                     replays: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Compose a ``--new-sets`` report (identity set, eligibility set, diagnostics) the same way: first clean result per
    conversation or diagnostic. ``keep`` is ``{"conversations": {id: why}, "diagnostics": {name: why}}``."""
    set_names = list(base["sets"])
    ids = {name: [r["id"] for r in base["sets"][name]["results"]] for name in set_names}
    diag_names = [d["diagnostic"] for d in base["diagnostics"]]
    sources = {name: report for name, report in [(base_name, base), *replays]}
    unclean = {name: unclean_owners(report) for name, report in sources.items()}
    chosen_convos: dict[str, tuple[str, Any]] = {}
    chosen_diags: dict[str, tuple[str, Any]] = {}
    log: dict[str, dict[str, Any]] = {"conversations": {}, "diagnostics": {}}
    for kind, idents in (("conversations", [i for name in set_names for i in ids[name]]), ("diagnostics", diag_names)):
        target = chosen_convos if kind == "conversations" else chosen_diags
        for ident in idents:
            attempts = []
            for name, report in sources.items():
                if kind == "conversations":
                    found = next((c for s in (report.get("sets") or {}).values() for c in s["results"]
                                  if c["id"] == ident), None)
                else:
                    found = next((d for d in report.get("diagnostics", []) if d["diagnostic"] == ident), None)
                if found is None:
                    continue
                if name == base_name:
                    accepted = ident in keep.get(kind, {})
                    why = keep.get(kind, {}).get(ident) if accepted else "not accepted as clean (transport-affected)"
                else:
                    accepted = ident not in unclean[name][kind]
                    why = "clean" if accepted else "transport failure in this attempt"
                attempts.append({"attempt": name, "accepted": accepted, "note": why})
                if accepted and ident not in target:
                    target[ident] = (name, copy.deepcopy(found))
            log[kind][ident] = {"used": target.get(ident, (None,))[0], "attempts": attempts}
    missing = {k: [i for i, v in log[k].items() if v["used"] is None] for k in log}
    sets: dict[str, dict[str, Any]] = {}
    for name in set_names:
        results = [chosen_convos[i][1] for i in ids[name] if i in chosen_convos]
        sets[name] = {"summary": ev.summarize_conversations(results), "how_to_read": ev.SCREEN_NOTE, "results": results}
    diagnostics = [chosen_diags[n][1] for n in diag_names if n in chosen_diags]
    sheet = ev.review_sheet(sets)
    totals = {"chat": 0, "complete": 0}
    for report in sources.values():
        for k in totals:
            totals[k] += (report.get("model_calls_total") or {}).get(k, 0)
    return {
        "schema_version": base.get("schema_version"), "evaluation_mode": "real_backend_new_sets_composite_of_attempts",
        "composition": {
            "disclosure": ("This report COMBINES results from several attempts. Each conversation and diagnostic keeps its "
                           "first clean result; those damaged by a transport failure were replayed from a fresh context. "
                           "Criteria and expectations are unchanged. Counts only, never blended with Evaluation A or "
                           "with the original set."),
            "complete": not any(missing.values()), "missing": missing, "per_test": log,
            "attempts": [{"name": name, "role": "base" if name == base_name else "replay",
                          "model_calls_total": report.get("model_calls_total"),
                          "unclean_tests": {k: sorted(v) for k, v in unclean[name].items()}}
                         for name, report in sources.items()],
            "model_calls_total_all_attempts": totals},
        "backend": base.get("backend"), "llm": base.get("llm"), "layers": base.get("layers"),
        "database": base.get("database"), "retrieval": base.get("retrieval"), "case_sources": base.get("case_sources"),
        "integrity": ev.integrity([], [{"results": s["results"]} for s in sets.values()], diagnostics),
        "opt_in_checks": base.get("opt_in_checks"), "note": base.get("note"),
        "manual_review": {"required": True, "turns": len(sheet), "reviewed": 0, "rule": ev.MANUAL_REVIEW_RULE},
        "review_sheet": sheet, "sets": sets, "diagnostics": diagnostics,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("unclean", help="print the tests a report cannot vouch for")
    plan.add_argument("report", type=Path)
    build = sub.add_parser("compose")
    build.add_argument("--base", type=Path, required=True)
    build.add_argument("--keep", type=Path, required=True, help="JSON: {cases|conversations|scenarios: {id: evidence}}")
    build.add_argument("--replay", type=Path, nargs="*", default=[])
    build.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "unclean":
        found = unclean_owners(json.loads(args.report.read_text(encoding="utf-8")))
        print(json.dumps({k: sorted(v) for k, v in found.items()}))
        return 0
    composite = compose_original(json.loads(args.base.read_text(encoding="utf-8")), args.base.name,
                                 json.loads(args.keep.read_text(encoding="utf-8")),
                                 [(p.name, json.loads(p.read_text(encoding="utf-8"))) for p in args.replay])
    args.out.write_text(json.dumps(composite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"complete": composite["composition"]["complete"], "missing": composite["composition"]["missing"]}))
    return 0 if composite["composition"]["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
