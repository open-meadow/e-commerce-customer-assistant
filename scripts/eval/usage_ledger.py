"""A persistent, cumulative ledger of every provider request an evaluation attempts.

Why it exists: on 2026-11-17 an interrupted run left no record, and a per-process counter reset on every start, so the
approved call ceiling could not be shown to have been respected. This ledger fixes the accounting, not the model.

Design
------
* One append-only JSON-lines file outside the repository (default ``~/.ecommerce-support-eval/usage_ledger.jsonl``).
  Every event is one line, written under an OS file lock, flushed and fsynced before the request it describes goes out.
* The hooks sit at the transport layer: ``urllib.request.urlopen`` (the chat clients) and ``Client.send`` of BOTH ``httpx``
  and ``httpx2`` (the installed OpenAI SDK 3.x, which backs Chroma's embeddings, runs on its own ``httpx2``; a hook on
  ``httpx`` alone would have missed every embedding request). One transport request is one ledger entry, so SDK-internal
  retries and embeddings are counted independently and nothing is counted twice. Only the host, path, method and a short
  fingerprint are stored. Never a header, a key, a prompt or a reply. Other HTTP libraries and async clients are NOT
  hooked; ``summary()['coverage']`` lists what was hooked and which installed libraries are not counted.
* Events per request: ``reserve`` (budget checked and claimed, before anything is sent), ``sent`` (written just before the
  transport is called) and ``outcome`` (``ok`` or ``error`` with the HTTP status or error class). If the process dies:
    - after ``reserve`` but before ``sent``: ``reserved_unsent``. The transport was never called, but it still counts
      against the budget until an operator voids it with a reason.
    - after ``sent`` but before ``outcome``: ``unknown_outcome``. It may have been billed, so it counts as spent.
* The budget is cumulative over the life of the ledger: ``remaining = ceiling - used``, where ``used`` is the sum of four
  kinds of usage that are kept apart in ``summary()['baseline']``: ledger-recorded requests (this ledger, every
  non-voided reservation), ``client_recorded`` prior attempts (an exact count the client kept), ``estimated`` prior usage
  (counted at its upper bound) and provider-verified ``historical`` dashboard imports (which replace the prior entries they
  supersede). ``unresolved`` prior usage has no number and BLOCKS the ledger. So does a missing or ``pending`` baseline:
  ``init`` requires an explicit ``prior_usage`` of ``none`` or ``pending``, and a baseline is never treated as zero by
  default. Windows are timezone-aware UTC datetimes (a naive timestamp is refused), and a historical import is refused
  when it would count the same requests twice. A ``reconcile`` import is only a cross-check and is never added.
* Fail closed: a missing ceiling, an unreadable file, a corrupt line (other than a torn final line from a crash while
  writing, which cannot describe a sent request) or a lock that cannot be taken refuses the request.
  There is no fallback to zero.

Provider-confirmed usage can only come from the provider's own dashboard, imported by an operator with ``import-dashboard``.
Everything else here is client-side bookkeeping, labelled as such in ``summary()``.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlparse

DEFAULT_PATH = Path.home() / ".ecommerce-support-eval" / "usage_ledger.jsonl"
ENV_PATH = "EVAL_USAGE_LEDGER"
LOCK_TIMEOUT_SECONDS = 10.0
_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}
EVENTS = {"ceiling", "baseline", "unknown_prior", "dashboard", "reserve", "sent", "outcome", "void"}
_REQUIRED = {
    "ceiling": {"ceiling"}, "baseline": {"status"}, "unknown_prior": {"id", "basis", "window_start", "window_end"},
    "dashboard": {"kind", "requests", "window_start", "window_end"}, "sent": {"req_id"}, "void": {"req_id"},
    "reserve": {"req_id", "run_id", "ts", "fingerprint", "kind"}, "outcome": {"req_id", "result"},
}
BASES = ("client_recorded", "estimated", "unresolved")
_HTTP_CLIENT_MODULES = ("httpx", "httpx2")  # the OpenAI SDK 3.x ships and uses its own copy, "httpx2"
_UNHOOKED_CANDIDATES = ("requests", "aiohttp", "urllib3", "http.client")


# Set by the harness's transport retry around each attempt so the ledger can link attempts of one logical request:
# {"logical_id": ..., "attempt": 1, "retry_reason": None or "dns"}. Read only; it never changes what is sent.
ATTEMPT: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar("ledger_attempt", default=None)


class LedgerError(RuntimeError):
    """The ledger cannot be trusted or used. The caller must not send the request."""


class BudgetExhausted(BaseException):
    """Reserving one more request would pass the ceiling. A BaseException so no ``except Exception`` can swallow it."""

    def __init__(self, used: int, ceiling: int):
        super().__init__(f"usage ledger: {used} of {ceiling} requests already counted")
        self.used, self.ceiling = used, ceiling


def _utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime. A naive timestamp is refused, never assumed to be UTC."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except (ValueError, AttributeError):
        raise LedgerError(f"not an ISO-8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerError(f"timestamp {value!r} has no timezone; give it as UTC (...Z) or with an explicit offset")
    return parsed.astimezone(timezone.utc)


def _window(start: str, end: str) -> tuple[datetime, datetime]:
    first, last = _utc(start), _utc(end)
    if last <= first:
        raise LedgerError("a window must end after it starts")
    return first, last


def _overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@contextlib.contextmanager
def _locked(path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """An exclusive inter-process lock on ``<ledger>.lock``. Raises LedgerError rather than waiting forever."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LedgerError("usage ledger lock could not be taken; refusing to continue") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        handle.close()


class Ledger:
    def __init__(self, path: Optional[Path] = None, *, run_id: Optional[str] = None,
                 lock_timeout: float = LOCK_TIMEOUT_SECONDS):
        self.path = Path(path or os.environ.get(ENV_PATH) or DEFAULT_PATH)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.lock_timeout = lock_timeout
        self.torn_tail = False
        self.hooked: list[str] = []
        self.unhooked_present: list[str] = []

    # -- file access (always called with the lock held) ------------------------------------------------------------

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise LedgerError(f"usage ledger {self.path} does not exist; initialise it with: init --ceiling N --reason ...")
        try:
            raw = self.path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LedgerError(f"usage ledger unreadable: {type(exc).__name__}") from None
        lines = raw.split("\n")
        events: list[dict[str, Any]] = []
        self.torn_tail = False
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            last = index == len(lines) - 1 and not raw.endswith("\n")
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("event") not in EVENTS:
                    raise ValueError("unknown event")
                if not _REQUIRED[event["event"]] <= event.keys():
                    raise ValueError("incomplete event")
            except ValueError:
                if last:  # a crash while writing the final line: that line was never fully durable, nothing was sent for it
                    self.torn_tail = True
                    continue
                raise LedgerError(f"usage ledger corrupt at line {index + 1}; refusing to continue") from None
            events.append(event)
        return events

    def _append(self, event: dict[str, Any]) -> None:
        event = {"ts": _now(), **event}
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        if self.path.exists() and self.path.stat().st_size:
            data = self.path.read_bytes()
            if not data.endswith(b"\n"):
                # A torn final line (a crash while writing). It was never durable, and the event it would have been
                # written before is covered by the earlier ones. Keep it in a sidecar for audit, cut it from the ledger.
                cut = data.rfind(b"\n") + 1
                with open(self.path.with_suffix(self.path.suffix + ".torn"), "ab") as sidecar:
                    sidecar.write(data[cut:] + b"\n")
                with open(self.path, "r+b") as handle:
                    handle.truncate(cut)
                    handle.flush()
                    os.fsync(handle.fileno())
        with open(self.path, "ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

    # -- operator commands ---------------------------------------------------------------------------------------

    def init(self, ceiling: int, reason: str, *, prior_usage: str) -> None:
        """Create the ledger. ``prior_usage`` is required, never defaulted: "none" asserts that nothing was spent before
        this ledger existed; "pending" says there was usage that is not yet recorded, which blocks every request until
        the baseline is established with record-unknown-prior / import-dashboard and declare-baseline."""
        if prior_usage not in ("none", "pending"):
            raise LedgerError("prior_usage must be 'none' (nothing was spent before) or 'pending' (to be recorded)")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.path, self.lock_timeout):
            if self.path.exists() and self.path.stat().st_size:
                raise LedgerError("usage ledger already exists; use set-ceiling to change the ceiling")
            self._append({"event": "ceiling", "ceiling": int(ceiling), "reason": reason})
            self._append({"event": "baseline", "status": prior_usage, "reason": reason})

    def set_ceiling(self, ceiling: int, reason: str) -> None:
        with _locked(self.path, self.lock_timeout):
            self._read()
            self._append({"event": "ceiling", "ceiling": int(ceiling), "reason": reason})

    def declare_baseline(self, status: str, reason: str) -> None:
        """The operator's explicit statement. "declared": every earlier request is represented by an unknown_prior
        entry or a historical dashboard import. "none": nothing was spent earlier. "pending": back to blocked."""
        if status not in ("declared", "none", "pending"):
            raise LedgerError("status must be declared, none or pending")
        with _locked(self.path, self.lock_timeout):
            state = _derive(self._read())
            if status == "none" and (state["priors"] or state["historical_imports"]):
                raise LedgerError("prior usage is recorded; declare the baseline 'declared', not 'none'")
            if status == "declared" and not (state["priors"] or state["historical_imports"]):
                raise LedgerError("nothing is recorded; declare 'none' only if no usage happened before the ledger")
            self._append({"event": "baseline", "status": status, "reason": reason})

    def record_unknown_prior(self, entry_id: str, *, basis: str, low: Optional[int], high: Optional[int],
                             window_start: str, window_end: str, note: str) -> None:
        """Usage from before the ledger existed. ``basis``: client_recorded (an exact count the client kept, low == high),
        estimated (a range) or unresolved (no usable number: the baseline stays blocked until a dashboard figure
        supersedes it). The window is required so overlaps with other records can be detected."""
        if basis not in BASES:
            raise LedgerError(f"basis must be one of {', '.join(BASES)}")
        if basis == "unresolved":
            if low is not None or high is not None:
                raise LedgerError("an unresolved entry carries no numbers")
        elif low is None or high is None or low < 0 or high < low:
            raise LedgerError("give 0 <= low <= high")
        elif basis == "client_recorded" and low != high:
            raise LedgerError("a client-recorded count is exact: low must equal high")
        new_window = _window(window_start, window_end)
        with _locked(self.path, self.lock_timeout):
            state = _derive(self._read())
            if entry_id in state["priors"]:
                raise LedgerError(f"unknown_prior {entry_id!r} already recorded")
            for other in state["priors"].values():
                if not other["superseded_by"] and _overlap(new_window, _window(other["window_start"], other["window_end"])):
                    raise LedgerError(f"window overlaps entry {other['id']!r}; merge them so nothing is counted twice")
            for record in state["requests"].values():
                if new_window[0] <= _utc(record["ts"]) <= new_window[1]:
                    raise LedgerError("window contains requests the ledger itself recorded; they would be counted twice")
            self._append({"event": "unknown_prior", "id": entry_id, "basis": basis, "low": low, "high": high,
                          "window_start": window_start, "window_end": window_end, "note": note})

    def import_dashboard(self, *, kind: str, requests: int, window_start: str, window_end: str, model: str, source: str,
                         input_tokens: Optional[int] = None, output_tokens: Optional[int] = None,
                         supersedes: Optional[list[str]] = None, note: str = "") -> None:
        """Provider-confirmed usage, typed in by the operator from the provider's own usage page.

        ``historical`` counts toward spend and replaces the unknown_prior entries it ``supersedes`` (its window must
        cover theirs). It is refused if its window overlaps anything already counted: a ledger-recorded request, an
        unsuperseded prior entry, or another historical import. ``reconcile`` is a cross-check of a window the ledger
        recorded itself and is never added to spend."""
        if kind not in ("historical", "reconcile"):
            raise LedgerError("kind must be 'historical' (counts toward spend) or 'reconcile' (cross-check only)")
        if requests < 0:
            raise LedgerError("requests cannot be negative")
        window = _window(window_start, window_end)
        supersedes = list(supersedes or [])
        with _locked(self.path, self.lock_timeout):
            state = _derive(self._read())
            if kind == "reconcile" and supersedes:
                raise LedgerError("a reconcile import supersedes nothing")
            if kind == "historical":
                for entry_id in supersedes:
                    entry = state["priors"].get(entry_id)
                    if entry is None:
                        raise LedgerError(f"no unknown_prior entry {entry_id!r} to supersede")
                    if entry["superseded_by"]:
                        raise LedgerError(f"{entry_id!r} is already superseded")
                    first, last = _window(entry["window_start"], entry["window_end"])
                    if not (window[0] <= first and last <= window[1]):
                        raise LedgerError(f"the import window does not cover entry {entry_id!r}; a partial replacement "
                                          "would count the remainder twice or not at all")
                for entry in state["priors"].values():
                    if (not entry["superseded_by"] and entry["id"] not in supersedes
                            and _overlap(window, _window(entry["window_start"], entry["window_end"]))):
                        raise LedgerError(f"the import window overlaps entry {entry['id']!r}; supersede it or narrow the window")
                for other in state["historical_imports"]:
                    if _overlap(window, _window(other["window_start"], other["window_end"])):
                        raise LedgerError("the import window overlaps an earlier historical import")
                for record in state["requests"].values():
                    if window[0] <= _utc(record["ts"]) <= window[1]:
                        raise LedgerError("the import window contains requests this ledger recorded; import it as "
                                          "'reconcile' (a cross-check), not 'historical' (they would be counted twice)")
            self._append({"event": "dashboard", "kind": kind, "requests": int(requests), "window_start": window_start,
                          "window_end": window_end, "model": model, "source": source, "input_tokens": input_tokens,
                          "output_tokens": output_tokens, "supersedes": supersedes, "note": note})

    def void(self, req_id: str, reason: str) -> None:
        """Release a request that was reserved but never sent. A request that may have been sent cannot be voided."""
        with _locked(self.path, self.lock_timeout):
            state = _derive(self._read())
            record = state["requests"].get(req_id)
            if record is None:
                raise LedgerError(f"no request {req_id!r}")
            if record["sent"]:
                raise LedgerError("that request was sent (or may have been); it stays counted")
            self._append({"event": "void", "req_id": req_id, "reason": reason})

    def ensure_ready(self) -> None:
        """Raise unless a request could be reserved right now (a ceiling and an established baseline)."""
        state = self.state()
        if state["ceiling"] is None:
            raise LedgerError("usage ledger has no ceiling; refusing to continue")
        if state["baseline_blockers"]:
            raise LedgerError("historical baseline not established: " + "; ".join(state["baseline_blockers"]))

    # -- request accounting ---------------------------------------------------------------------------------------

    def reserve(self, kind: str, host: str, path: str, method: str, fingerprint: str,
                link: Optional[dict[str, Any]] = None) -> str:
        """Check the ceiling and claim one request, atomically. Raises BudgetExhausted or LedgerError before any send."""
        req_id = uuid.uuid4().hex[:16]
        with _locked(self.path, self.lock_timeout):
            state = _derive(self._read())
            if state["ceiling"] is None:
                raise LedgerError("usage ledger has no ceiling; refusing to continue")
            if state["baseline_blockers"]:
                raise LedgerError("historical baseline not established: " + "; ".join(state["baseline_blockers"]))
            if state["used"] + 1 > state["ceiling"]:
                raise BudgetExhausted(state["used"], state["ceiling"])
            self._append({"event": "reserve", "req_id": req_id, "run_id": self.run_id, "pid": os.getpid(), "kind": kind,
                          "host": host, "path": path, "method": method, "fingerprint": fingerprint,
                          **{k: v for k, v in (link or {}).items() if k in ("logical_id", "attempt", "retry_reason")}})
        return req_id

    def mark_sent(self, req_id: str) -> None:
        with _locked(self.path, self.lock_timeout):
            self._append({"event": "sent", "req_id": req_id})

    def finish(self, req_id: str, result: str, *, status: Optional[int] = None, error: Optional[str] = None) -> None:
        with _locked(self.path, self.lock_timeout):
            self._append({"event": "outcome", "req_id": req_id, "result": result, "status": status, "error": error})

    def state(self) -> dict[str, Any]:
        with _locked(self.path, self.lock_timeout):
            return _derive(self._read())

    def summary(self, *, run_only: bool = False) -> dict[str, Any]:
        state = self.state()
        out = _summarise(state, self.run_id if run_only else None, self.torn_tail)
        out["coverage"] = {"hooked": list(self.hooked), "installed_but_not_counted": list(self.unhooked_present)}
        return out


def _derive(events: list[dict[str, Any]]) -> dict[str, Any]:
    ceiling: Optional[int] = None
    baseline_status: Optional[str] = None
    priors: dict[str, dict[str, Any]] = {}
    dashboards: list[dict[str, Any]] = []
    requests: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = event["event"]
        if kind == "ceiling":
            ceiling = int(event["ceiling"])
        elif kind == "baseline":
            baseline_status = event["status"]
        elif kind == "unknown_prior":
            priors[event["id"]] = {**event, "superseded_by": None}
        elif kind == "dashboard":
            dashboards.append(event)
            if event["kind"] == "historical":
                for entry_id in event.get("supersedes") or []:
                    if entry_id in priors:
                        priors[entry_id]["superseded_by"] = event["window_start"] + ".." + event["window_end"]
        elif kind == "reserve":
            requests[event["req_id"]] = {**event, "sent": False, "result": None, "voided": False, "status": None,
                                         "error": None}
        elif event.get("req_id") in requests:
            record = requests[event["req_id"]]
            if kind == "sent":
                record["sent"] = True
            elif kind == "outcome":
                record["result"], record["status"], record["error"] = event["result"], event.get("status"), event.get("error")
            elif kind == "void":
                record["voided"] = True
    local = [r for r in requests.values() if not r["voided"]]
    live = [p for p in priors.values() if not p["superseded_by"]]
    exact = [p for p in live if p["basis"] == "client_recorded"]
    estimated = [p for p in live if p["basis"] == "estimated"]
    unresolved = [p for p in live if p["basis"] == "unresolved"]
    historical = [d for d in dashboards if d["kind"] == "historical"]
    verified = sum(d["requests"] for d in historical)
    blockers = []
    if baseline_status is None:
        blockers.append("no baseline statement: declare whether anything was spent before this ledger existed")
    elif baseline_status == "pending":
        blockers.append("the baseline is marked pending")
    blockers += [f"unresolved prior usage {p['id']!r} has no number" for p in unresolved]
    used = len(local) + sum(p["high"] for p in exact) + sum(p["high"] for p in estimated) + verified
    return {"ceiling": ceiling, "requests": requests, "local": local, "priors": priors, "dashboards": dashboards,
            "historical_imports": historical, "baseline_status": baseline_status, "baseline_blockers": blockers,
            "exact_prior": sum(p["high"] for p in exact), "estimated_low": sum(p["low"] for p in estimated),
            "estimated_high": sum(p["high"] for p in estimated), "unresolved": unresolved, "exact_entries": exact,
            "estimated_entries": estimated, "verified": verified, "used": used}


def _outcome_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "reserved_locally": len(records),
        "sent_to_transport": sum(1 for r in records if r["sent"]),
        "ok": sum(1 for r in records if r["result"] == "ok"),
        "error": sum(1 for r in records if r["result"] == "error"),
        "unknown_outcome": sum(1 for r in records if r["sent"] and r["result"] is None),
        "reserved_unsent": sum(1 for r in records if not r["sent"]),
    }


def _retries(records: list[dict[str, Any]]) -> int:
    """Requests whose identical predecessor in the same run ended in an error: independently counted retries."""
    failed: set[tuple[str, str]] = set()
    retries = 0
    for record in records:
        key = (record["run_id"], record["fingerprint"])
        if key in failed:
            retries += 1
        if record["result"] == "error":
            failed.add(key)
    return retries


def _summarise(state: dict[str, Any], run_id: Optional[str], torn_tail: bool) -> dict[str, Any]:
    records = [r for r in state["local"] if run_id is None or r["run_id"] == run_id]
    by_kind: dict[str, int] = {}
    for record in records:
        by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
    ceiling = state["ceiling"]
    out: dict[str, Any] = {
        "scope": "this run" if run_id else "whole ledger",
        "basis": "client-side bookkeeping at the transport layer; not provider-confirmed",
        "ceiling": ceiling,
        "requests": _outcome_counts(records),
        "by_kind": by_kind,
        "retries_counted_independently": _retries(records),
        "harness_retry_attempts": sum(1 for r in records if (r.get("attempt") or 1) > 1),
        "torn_tail_ignored": torn_tail,
    }
    if run_id is None:
        blocked = bool(state["baseline_blockers"])
        out["baseline"] = {
            "status": state["baseline_status"],
            "blocking": state["baseline_blockers"],
            "provider_verified": {"requests": state["verified"], "imports": [
                {"window": [d["window_start"], d["window_end"]], "model": d["model"], "requests": d["requests"],
                 "source": d["source"], "supersedes": d.get("supersedes") or []} for d in state["historical_imports"]]},
            "client_recorded": {"requests": state["exact_prior"], "entries": [p["id"] for p in state["exact_entries"]]},
            "estimated": {"low": state["estimated_low"], "high": state["estimated_high"],
                          "entries": [p["id"] for p in state["estimated_entries"]]},
            "unresolved": {"entries": [p["id"] for p in state["unresolved"]]},
            "superseded_entries": {p["id"]: p["superseded_by"] for p in state["priors"].values() if p["superseded_by"]},
        }
        low_used = (len(state["local"]) + state["exact_prior"] + state["estimated_low"] + state["verified"])
        out["budget"] = {
            "used_total": None if blocked else state["used"],
            "used_is_an_upper_bound": bool(state["estimated_entries"]),
            "ledger_recorded": len(state["local"]),
            "remaining": None if blocked or ceiling is None else ceiling - state["used"],
            "remaining_if_estimates_were_at_their_lower_bound": None if blocked or ceiling is None else ceiling - low_used,
            "blocked": blocked,
        }
        out["dashboard_reconciliation"] = [
            {"window": [d["window_start"], d["window_end"]], "model": d["model"], "provider_requests": d["requests"],
             "ledger_requests_in_window": sum(
                 1 for r in state["local"] if _window(d["window_start"], d["window_end"])[0] <= _utc(r["ts"])
                 <= _window(d["window_start"], d["window_end"])[1]),
             "note": "cross-check only; embeddings and chat models may be listed separately by the provider"}
            for d in state["dashboards"] if d["kind"] == "reconcile"]
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Transport hooks
# ---------------------------------------------------------------------------------------------------------------------

def _classify(path: str) -> str:
    path = path.lower()
    if path.endswith("/embeddings"):
        return "embedding"
    if path.endswith(("/responses", "/chat/completions", "/messages")):
        return "chat"
    return "other"


def _fingerprint(method: str, path: str, body: Optional[bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(f"{method} {path}".encode("utf-8"))
    digest.update(body or b"")
    return digest.hexdigest()[:12]


def _guarded(ledger: Ledger, host: str, path: str, method: str, body: Optional[bytes],
             call: Callable[[], Any], status_of: Callable[[Any], Optional[int]]) -> Any:
    if host in _LOOPBACK:
        return call()
    req_id = ledger.reserve(_classify(path), host, path, method, _fingerprint(method, path, body), ATTEMPT.get())
    ledger.mark_sent(req_id)  # after this line the transport may be reached; before it, nothing has left the process
    try:
        result = call()
    except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised unchanged
        status = getattr(exc, "code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        ledger.finish(req_id, "error", status=status if isinstance(status, int) else None, error=type(exc).__name__)
        raise
    status = status_of(result)
    if status is not None and status >= 400:
        ledger.finish(req_id, "error", status=status, error="http_status")
    else:
        ledger.finish(req_id, "ok", status=status)
    return result


@contextlib.contextmanager
def guards(ledger: Ledger) -> Iterator[Ledger]:
    """Count every provider request made through urllib or an httpx-family client while active.

    Verifies the ledger first and refuses unless it has a ceiling and an established baseline (fail closed). Hooks
    ``urllib.request.urlopen`` and ``Client.send`` of every importable module in ``_HTTP_CLIENT_MODULES``. The OpenAI SDK
    3.x runs on its own ``httpx2``, which a hook on ``httpx`` alone would miss. ``ledger.hooked`` lists what was hooked and
    ``ledger.unhooked_present`` lists other HTTP libraries that are installed and NOT counted."""
    ledger.ensure_ready()
    import urllib.request
    original_urlopen = urllib.request.urlopen
    patched: list[tuple[Any, Any]] = []

    def urlopen(request, *args, **kwargs):
        if isinstance(request, str):
            url, method, body = request, "GET", None
        else:
            url, method, body = request.full_url, request.get_method(), getattr(request, "data", None)
        parsed = urlparse(url)
        data = body if isinstance(body, (bytes, bytearray)) else None
        return _guarded(ledger, parsed.hostname or "", parsed.path, method, data and bytes(data),
                        lambda: original_urlopen(request, *args, **kwargs), lambda r: getattr(r, "status", None))

    def make_send(original_send):
        def send(self, request, *args, **kwargs):
            url = request.url
            try:
                body = request.content
            except Exception:  # noqa: BLE001 - a streaming body: fingerprint without it
                body = None
            return _guarded(ledger, url.host or "", url.path, request.method, body,
                            lambda: original_send(self, request, *args, **kwargs), lambda r: r.status_code)
        return send

    hooked = ["urllib.request.urlopen"]
    for name in _HTTP_CLIENT_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        patched.append((module.Client, module.Client.send))
        module.Client.send = make_send(module.Client.send)
        hooked.append(f"{name}.Client.send")
    urllib.request.urlopen = urlopen
    ledger.hooked = hooked
    ledger.unhooked_present = [n for n in _UNHOOKED_CANDIDATES if importlib.util.find_spec(n) is not None] + [
        f"{n}.AsyncClient" for n in _HTTP_CLIENT_MODULES if n in sys.modules]
    try:
        yield ledger
    finally:
        urllib.request.urlopen = original_urlopen
        for client, original_send in patched:
            client.send = original_send


# ---------------------------------------------------------------------------------------------------------------------
# Command line (operator actions only; nothing here sends a request)
# ---------------------------------------------------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--ceiling", type=int, required=True); p.add_argument("--reason", required=True)
    p.add_argument("--prior-usage", choices=("none", "pending"), required=True,
                   help="none: nothing was spent before this ledger. pending: there was, it is not recorded yet (blocks)")
    p = sub.add_parser("set-ceiling"); p.add_argument("--ceiling", type=int, required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("declare-baseline")
    p.add_argument("--status", choices=("declared", "none", "pending"), required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("record-unknown-prior")
    p.add_argument("--id", required=True); p.add_argument("--basis", choices=BASES, required=True)
    p.add_argument("--low", type=int); p.add_argument("--high", type=int)
    p.add_argument("--window-start", required=True); p.add_argument("--window-end", required=True)
    p.add_argument("--note", required=True)
    p = sub.add_parser("import-dashboard")
    p.add_argument("--kind", choices=("historical", "reconcile"), required=True)
    p.add_argument("--requests", type=int, required=True); p.add_argument("--window-start", required=True)
    p.add_argument("--window-end", required=True); p.add_argument("--model", required=True)
    p.add_argument("--source", required=True); p.add_argument("--input-tokens", type=int)
    p.add_argument("--output-tokens", type=int); p.add_argument("--supersedes", nargs="*", default=[])
    p.add_argument("--note", default="")
    p = sub.add_parser("void"); p.add_argument("--req-id", required=True); p.add_argument("--reason", required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    ledger = Ledger(args.ledger)
    try:
        if args.command == "init":
            ledger.init(args.ceiling, args.reason, prior_usage=args.prior_usage)
        elif args.command == "set-ceiling":
            ledger.set_ceiling(args.ceiling, args.reason)
        elif args.command == "declare-baseline":
            ledger.declare_baseline(args.status, args.reason)
        elif args.command == "record-unknown-prior":
            ledger.record_unknown_prior(args.id, basis=args.basis, low=args.low, high=args.high,
                                        window_start=args.window_start, window_end=args.window_end, note=args.note)
        elif args.command == "import-dashboard":
            ledger.import_dashboard(kind=args.kind, requests=args.requests, window_start=args.window_start,
                                    window_end=args.window_end, model=args.model, source=args.source,
                                    input_tokens=args.input_tokens, output_tokens=args.output_tokens,
                                    supersedes=args.supersedes, note=args.note)
        elif args.command == "void":
            ledger.void(args.req_id, args.reason)
        sys.stdout.write(json.dumps(ledger.summary(), indent=2) + "\n")
    except LedgerError as exc:
        sys.stderr.write(f"usage ledger: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
