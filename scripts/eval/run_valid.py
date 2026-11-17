"""Run a real-backend evaluation until it is VALID, with a connectivity preflight, backoff and a call budget.

Why: a live run is only scored when no reply was replaced by "the assistant is temporarily unavailable" (the report's
``integrity.run_valid_for_scoring``). On 2026-11-17 a network outage made six of nine attempts invalid because they were
retried immediately into the same outage. This runner waits between attempts, checks that the API host can be reached
before spending anything (a DNS lookup and a TCP connect: no request, no cost), stops a run early after consecutive
provider failures (exit code 3 of ``real_eval.py``), and refuses to start an attempt that would exceed the call budget.

    python scripts/eval/run_valid.py --label original --out-dir results --budget 160 -- \\
        --llm env --allow-live --retrieval production --cases ... --conversations ...

Everything after ``--`` goes to ``real_eval.py`` unchanged (``--output`` is added per attempt). The key is read from the
environment by ``real_eval.py``; to load it some other way pass ``--runner "python my_wrapper.py"``. Every attempt's
report is kept, and ``<label>_attempts.json`` records the history: preflight, exit code, validity, provider errors, calls.
Exit codes: 0 a valid run exists, 1 no valid run, 2 stopped by the budget.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

DEFAULT_BACKOFF = (30, 90, 270)
STOP_AFTER_FAILURES = 3


def api_host(env: Optional[dict[str, str]] = None) -> tuple[str, int]:
    base = (env if env is not None else os.environ).get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    parsed = urlparse(base)
    return parsed.hostname or "api.openai.com", parsed.port or (443 if parsed.scheme != "http" else 80)


def preflight(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """Can the API host be resolved and connected to? Opens and closes a socket; sends nothing."""
    try:
        socket.getaddrinfo(host, port)
    except OSError as exc:
        return False, f"dns: {type(exc).__name__}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "ok"
    except OSError as exc:
        return False, f"connect: {type(exc).__name__}"


def _calls(report: dict[str, Any]) -> int:
    totals = report.get("model_calls_total") or {}
    return int(totals.get("chat", 0)) + int(totals.get("complete", 0))


def run_until_valid(label: str, out_dir: Path, args: Sequence[str], *, runner: Sequence[str], max_attempts: int = 3,
                    backoff: Sequence[float] = DEFAULT_BACKOFF, budget: int = 0,
                    check: Callable[[], tuple[bool, str]] = lambda: preflight(*api_host()),
                    sleep: Callable[[float], None] = time.sleep,
                    run: Callable[[list[str]], int] = lambda cmd: subprocess.run(cmd).returncode) -> dict[str, Any]:
    """Attempt a run up to ``max_attempts`` times. Returns the history (also written next to the reports)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    spent, status = 0, "no_valid_run"
    for attempt in range(1, max_attempts + 1):
        entry: dict[str, Any] = {"attempt": attempt}
        ok, detail = check()
        entry["preflight"] = detail
        if not ok:
            entry.update({"ran": False, "calls": 0})
            history.append(entry)
        else:
            last = max((h.get("calls", 0) for h in history if h.get("ran")), default=0)
            if budget and spent + last > budget:
                entry.update({"ran": False, "calls": 0, "stopped": "the next attempt could exceed the call budget"})
                history.append(entry)
                status = "budget"
                break
            path = out_dir / f"{label}_attempt{attempt}.json"
            code = run([*runner, *args, "--stop-after-provider-failures", str(STOP_AFTER_FAILURES),
                        "--output", str(path)])
            entry.update({"ran": True, "exit_code": code, "report": path.name})
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = {}
            integrity = report.get("integrity", {})
            entry["valid"] = bool(integrity.get("run_valid_for_scoring"))
            entry["provider_error_replies"] = integrity.get("provider_error_replies", {}).get("total")
            entry["calls"] = _calls(report)
            spent += entry["calls"]
            history.append(entry)
            if entry["valid"] and code == 0:
                shutil.copyfile(path, out_dir / f"{label}_valid.json")
                status = "valid"
                break
            if budget and spent >= budget:
                status = "budget"
                break
        if attempt < max_attempts:
            sleep(backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0)
    summary = {"label": label, "status": status, "calls_attempted": spent, "budget": budget or None,
               "backoff_seconds": list(backoff), "attempts": history}
    (out_dir / f"{label}_attempts.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--backoff", default=",".join(str(b) for b in DEFAULT_BACKOFF),
                        help="seconds to wait after each failed attempt, comma separated (the last repeats)")
    parser.add_argument("--budget", type=int, default=0, help="stop before an attempt that could exceed this many calls")
    parser.add_argument("--runner", default=None, help="command that runs real_eval (default: this checkout's)")
    parser.add_argument("eval_args", nargs=argparse.REMAINDER, help="arguments for real_eval.py, after --")
    args = parser.parse_args(argv)
    eval_args = [a for a in args.eval_args if a != "--"] if args.eval_args[:1] == ["--"] else args.eval_args
    runner = shlex.split(args.runner) if args.runner else [sys.executable, str(Path(__file__).with_name("real_eval.py"))]
    summary = run_until_valid(args.label, args.out_dir, eval_args, runner=runner, max_attempts=args.max_attempts,
                              backoff=[float(b) for b in args.backoff.split(",") if b], budget=args.budget)
    sys.stdout.write(json.dumps({k: v for k, v in summary.items() if k != "attempts"}) + "\n")
    return {"valid": 0, "budget": 2}.get(summary["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
