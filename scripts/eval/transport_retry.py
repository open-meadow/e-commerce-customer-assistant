"""Evaluation-harness-only retry of requests that provably never left the machine. Product code is not involved.

A live evaluation run is invalid if any reply was replaced by "temporarily unavailable". On 2026-11-17 a flaky local DNS
resolver invalidated several runs although no request had reached the provider. The product's LLM clients deliberately do
not retry, so this wrapper sits UNDER them, around ``urllib.request.urlopen``, and re-sends the same request when (and only
when) the failure happened before any byte could have been transmitted:

* RETRIED (at most ``MAX_RETRIES`` = 2 extra attempts per logical request): DNS resolution failure (``socket.gaierror``),
  connection refused, and network/host unreachable. These can only occur while connecting.
* NEVER RETRIED: every other failure, because its outcome is ambiguous or already known. That includes read and connect
  timeouts (a timeout cannot be told apart from a slow server that already received the request), connection resets,
  ``http.client`` errors, and any HTTP status (the provider answered). The run then fails the normal way and is invalid.

Properties: the same request object (same URL, headers and payload) is re-sent; each attempt is its own entry in the usage
ledger, linked by ``logical_id`` and ``attempt``; waits are bounded (default 5 s, then 20 s, ending early as soon as the
host resolves); nothing here waits indefinitely. The embedding path is not wrapped: the OpenAI SDK has its own retries,
which are part of the production behaviour being measured and are counted by the ledger like any other request.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import hashlib
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlparse

import usage_ledger

MAX_RETRIES = 2
DEFAULT_DELAYS = (5.0, 20.0)
_UNREACHABLE_ERRNO = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, 10050, 10051, 10065}  # 1005x: WinSock


def pre_transmission_reason(exc: BaseException) -> Optional[str]:
    """Name the failure if it provably happened before transmission, else None (never retry)."""
    if isinstance(exc, urllib.error.HTTPError) or not isinstance(exc, urllib.error.URLError):
        # HTTPError: the provider answered. A bare OSError/TimeoutError/http.client error comes from reading a response.
        return None
    reason = exc.reason
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(reason, OSError) and (reason.errno in _UNREACHABLE_ERRNO or getattr(reason, "winerror", None)
                                         in _UNREACHABLE_ERRNO):
        return "network_unreachable"
    return None  # timeouts, resets and anything unrecognised are ambiguous


# The test case, conversation or scenario the harness is running. Set by the harness, read here only to say WHICH test a
# transport incident belongs to, so a failure that did not become an error reply is still attributed.
OWNER: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("transport_owner", default=None)


@contextlib.contextmanager
def owner(label: str) -> Iterator[None]:
    token = OWNER.set(label)
    try:
        yield
    finally:
        OWNER.reset(token)


class Collector:
    """What the retry layer did, for the run report. Network trouble is reported here, apart from chatbot results."""

    def __init__(self) -> None:
        self.logical_requests = 0
        self.attempts_total = 0
        self.recovered: list[dict[str, Any]] = []
        self.exhausted: list[dict[str, Any]] = []
        self.not_retried: dict[str, int] = {}
        self.by_reason: dict[str, int] = {}
        self.incidents: list[dict[str, Any]] = []  # every failure and recovery, with the test it belongs to

    def summary(self) -> dict[str, Any]:
        return {"scope": "evaluation harness only; product code unchanged",
                "max_retries_per_logical_request": MAX_RETRIES,
                "retried_only": "failures before transmission: dns, connection_refused, network_unreachable",
                "logical_requests": self.logical_requests, "attempts_total": self.attempts_total,
                "retry_attempts": self.attempts_total - self.logical_requests,
                "recovered": self.recovered, "exhausted": self.exhausted,
                "not_retried_failures_by_type": dict(self.not_retried), "retry_reasons": dict(self.by_reason),
                "incidents": list(self.incidents),
                "tests_with_unrecovered_transport_failure": sorted(
                    {i["owner"] for i in self.incidents if i["outcome"] in ("not_retried", "exhausted") and i["owner"]})}


def annotate(report: dict[str, Any], collector: Collector) -> None:
    """Add the retry record to a report. A run with an exhausted request is not scored."""
    summary = collector.summary()
    report["transport_retries"] = summary
    if summary["exhausted"] and isinstance(report.get("integrity"), dict):
        report["integrity"]["run_valid_for_scoring"] = False
        report["integrity"]["transport_retries_exhausted"] = len(summary["exhausted"])
    if summary["tests_with_unrecovered_transport_failure"] and isinstance(report.get("integrity"), dict):
        # An unrecovered failure may not have produced an error reply (a swallowed interpretation call, for instance).
        report["integrity"]["transport_affected_tests"] = summary["tests_with_unrecovered_transport_failure"]


def _logical_id(request: Any) -> str:
    if isinstance(request, str):
        return hashlib.sha256(request.encode()).hexdigest()[:12]
    data = getattr(request, "data", None)
    digest = hashlib.sha256(f"{request.get_method()} {request.full_url}".encode())
    digest.update(bytes(data) if isinstance(data, (bytes, bytearray)) else b"")
    return digest.hexdigest()[:12]


def _host(request: Any) -> str:
    return urlparse(request if isinstance(request, str) else request.full_url).hostname or ""


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


@contextlib.contextmanager
def retrying(max_retries: int = MAX_RETRIES, *, delays: Optional[tuple[float, ...]] = None,
             collector: Optional[Collector] = None, sleep: Optional[Callable[[float], None]] = None,
             probe: Optional[Callable[[str], bool]] = None) -> Iterator[Collector]:
    """Wrap ``urllib.request.urlopen`` with the pre-transmission retry. Install AFTER the ledger guards so that each
    attempt is a separate ledger entry."""
    if not 0 <= max_retries <= MAX_RETRIES:
        raise ValueError(f"max_retries must be between 0 and {MAX_RETRIES}")
    collector = collector if collector is not None else Collector()
    original = urllib.request.urlopen
    delays = DEFAULT_DELAYS if delays is None else delays  # looked up when called, so they can be replaced
    sleep = sleep if sleep is not None else time.sleep
    probe = probe if probe is not None else _resolves

    def wait(delay: float, host: str) -> None:
        for _ in range(max(1, int(delay))):  # bounded: at most ``delay`` seconds, less once the host resolves
            sleep(1.0)
            if probe(host):
                return

    def urlopen(request, *args, **kwargs):
        logical = _logical_id(request)
        collector.logical_requests += 1
        reasons: list[str] = []
        attempt = 1
        while True:
            token = usage_ledger.ATTEMPT.set({"logical_id": logical, "attempt": attempt,
                                              "retry_reason": reasons[-1] if reasons else None})
            collector.attempts_total += 1
            try:
                result = original(request, *args, **kwargs)  # the SAME request object: same URL, headers and payload
            except Exception as exc:  # noqa: BLE001 - classified, then re-raised or retried
                reason = pre_transmission_reason(exc)
                if reason is None:
                    name = type(exc.reason).__name__ if isinstance(exc, urllib.error.URLError) and not isinstance(
                        exc, urllib.error.HTTPError) else type(exc).__name__
                    collector.not_retried[name] = collector.not_retried.get(name, 0) + 1
                    collector.incidents.append({"owner": OWNER.get(), "logical_id": logical, "outcome": "not_retried",
                                                "error": name, "attempt": attempt})
                    raise
                collector.by_reason[reason] = collector.by_reason.get(reason, 0) + 1
                reasons.append(reason)
                if attempt > max_retries:
                    collector.exhausted.append({"logical_id": logical, "attempts": attempt, "reasons": reasons})
                    collector.incidents.append({"owner": OWNER.get(), "logical_id": logical, "outcome": "exhausted",
                                                "error": reason, "attempt": attempt})
                    raise
                wait(delays[min(attempt - 1, len(delays) - 1)] if delays else 1.0, _host(request))
                attempt += 1
                continue
            finally:
                usage_ledger.ATTEMPT.reset(token)
            if attempt > 1:
                collector.recovered.append({"logical_id": logical, "attempts": attempt, "reasons": reasons})
                collector.incidents.append({"owner": OWNER.get(), "logical_id": logical, "outcome": "recovered",
                                            "error": reasons[-1], "attempt": attempt})
            return result

    urllib.request.urlopen = urlopen
    try:
        yield collector
    finally:
        urllib.request.urlopen = original
