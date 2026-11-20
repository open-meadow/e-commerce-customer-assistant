"""Process-local rate limiting and provider budgets (H3).

Everything here lives in the memory of ONE process: two workers or two replicas each keep their own counters, so a limit of N
becomes up to N times the number of instances. A multi-instance deployment needs a shared store (for example Redis) for the
session registry and for these counters; see docs/session_resource_protection.md.

Standard library only. Time comes from ``app.backend.clock`` so tests control it.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections import deque
from typing import Any, Iterator, Optional

from app.backend import clock
from app.llm.base import LLMError


class SessionLimitError(Exception):
    """A session could not be created. ``reason`` is ``capacity`` or ``creation_rate``; ``retry_after`` is in seconds."""

    def __init__(self, reason: str, retry_after: int = 30) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = max(1, int(retry_after))


class SlidingWindowLimiter:
    """At most ``limit`` accepted attempts per key within ``window_seconds``.

    A refused attempt is not recorded, so hammering a limit never extends it. The number of tracked keys is bounded: when it
    would be exceeded, keys with no attempts left in the window are dropped first, then the oldest keys.
    """

    def __init__(self, limit: int, window_seconds: float, *, max_keys: int = 10000) -> None:
        self.limit = max(1, int(limit))
        self.window = float(window_seconds)
        self.max_keys = max(1, int(max_keys))
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, now) -> deque:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        while hits and (now - hits[0]).total_seconds() >= self.window:
            hits.popleft()
        return hits

    def would_allow(self, key: str) -> bool:
        now = clock.now()
        with self._lock:
            return len(self._trim(key, now)) < self.limit

    def allow(self, key: str) -> bool:
        now = clock.now()
        with self._lock:
            hits = self._trim(key, now)
            if len(hits) >= self.limit:
                return False
            if key not in self._hits:
                self._make_room(now)
                self._hits[key] = hits
            hits.append(now)
            return True

    def release(self, key: str) -> None:
        """Take back the most recent accepted attempt (used when a later check refuses the same request)."""
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key`` may try again (0 if it may now)."""
        now = clock.now()
        with self._lock:
            hits = self._trim(key, now)
            if len(hits) < self.limit:
                return 0
            return max(1, int(self.window - (now - hits[0]).total_seconds()) + 1)

    def forget(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def key_count(self) -> int:
        with self._lock:
            return len(self._hits)

    def _make_room(self, now) -> None:
        if len(self._hits) < self.max_keys:
            return
        for key in [k for k in self._hits if not self._trim(k, now)]:
            del self._hits[key]
        while len(self._hits) >= self.max_keys:
            del self._hits[next(iter(self._hits))]


_current_session: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("support_provider_session", default=None)


class ProviderBudget:
    """How many model calls one session may make, and how many all sessions together may make per minute."""

    def __init__(self, per_session: int, per_minute: int) -> None:
        self.per_session = max(1, int(per_session))
        self._window = SlidingWindowLimiter(per_minute, 60, max_keys=4)
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def session(self, ref: str) -> Iterator[None]:
        """Charge the model calls made inside this block to the session ``ref``."""
        token = _current_session.set(ref)
        try:
            yield
        finally:
            _current_session.reset(token)

    def refusal(self, ref: str) -> Optional[str]:
        """``session`` or ``window`` if a model call would be refused now, else ``None``. Charges nothing."""
        with self._lock:
            if self._used.get(ref, 0) >= self.per_session:
                return "session"
        return None if self._window.would_allow("all") else "window"

    def charge(self) -> None:
        """Account for one model call; raises ``LLMError(category="budget")`` when a limit is reached."""
        ref = _current_session.get()
        with self._lock:
            if ref is not None and self._used.get(ref, 0) >= self.per_session:
                raise LLMError("The session's model-call budget is used up.", category="budget")
            if not self._window.allow("all"):
                raise LLMError("The model-call rate limit is reached.", category="budget")
            if ref is not None:
                self._used[ref] = self._used.get(ref, 0) + 1

    def forget(self, ref: str) -> None:
        with self._lock:
            self._used.pop(ref, None)

    def used(self, ref: str) -> int:
        with self._lock:
            return self._used.get(ref, 0)


class BudgetedLLM:
    """Wraps a model client so every call is charged to the current session's budget. Adapters never retry; this does not either."""

    def __init__(self, inner: Any, budget: ProviderBudget) -> None:
        self._inner = inner
        self._budget = budget

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        self._budget.charge()
        return self._inner.chat(*args, **kwargs)

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        self._budget.charge()
        return self._inner.complete(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
