"""In-memory session registry and pending-action store (docs/03 Sections 6 and 7).

Single process, one lock. Everything here is volatile: a restart loses
sessions and pending actions, which is why the demo is single-process and
why an unconfirmed action can never survive a restart and be replayed.

Security properties:

* The session handle is a bearer credential: 256 random bits, never
  logged (only a short hash, ``session_ref``, is), never sent to the LLM.
* A pending action is bound to one session and one customer, expires, and
  can be claimed exactly once. ``claim`` is atomic under the lock, so two
  concurrent confirmations cannot both proceed.
* Write capacity is enforced as committed plus reserved, so concurrent
  confirmations cannot exceed the per-session cap.

H3 resource protection (all process-local, see docs/session_resource_protection.md):

* Creation is atomic under the lock: the global cap and the creation rates are checked and the session stored in one step.
* Sessions expire when idle and when older than an absolute age; a background sweeper (and every create) removes them.
  A session whose turn is running or whose action is claimed is never removed, so an operation cannot lose its session.
* Listeners are told when a session is dropped, so the HTTP maps and LiveKit rooms are released with it.
* Finished pending actions are kept only in a small bounded number (a repeated confirmation still sees ``done``).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.backend import clock
from app.backend.config import Settings
from app.backend.limits import SessionLimitError, SlidingWindowLimiter
from app.orchestrator.issues import IssueBook

log = logging.getLogger("app.backend.sessions")


class SessionClosedError(Exception):
    """The session was ended or expired while a turn was still running; nothing more may be attached to it."""

_KEEP_FINISHED_PENDING = 20  # finished actions kept per session so a repeated confirmation still reports its state


@dataclass
class PendingAction:
    action_id: str
    session_handle: str
    customer_id: str
    kind: str  # return_request | support_ticket
    payload: dict[str, Any]
    summary: str
    created: datetime
    expires: datetime
    state: str = "open"  # open | claimed | done | cancelled | expired


@dataclass
class ChoiceState:
    prompt_id: str
    kind: str  # return_category | relation | third_issue
    options: tuple[str, ...]
    data: dict[str, Any] = field(default_factory=dict)
    expires: Optional[datetime] = None  # absolute; never extended by re-display


@dataclass
class Session:
    handle: str
    customer_id: str
    display_name: str
    created: datetime
    last_active: datetime
    session_ref: str = ""
    language: str = "en"  # the customer's language, kept from their latest clear message
    # Durations (numbers only) that a tool result or policy text has given in this session. A reply may state a
    # duration only if it is in a tool result this turn or here; the customer's own words never add to it.
    verified_durations: set[str] = field(default_factory=set)
    # Ticket IDs returned by a SUCCESSFUL repository creation in this session (lower case). The only session-level source
    # of trust for a ticket ID in a reply; never filled from model prose, customer input or any other text.
    known_ticket_ids: set[str] = field(default_factory=set)
    # Where the next page of the customer's ticket list starts, set only after a list that had more rows than one page.
    ticket_next_offset: Optional[int] = None
    history: list[dict[str, str]] = field(default_factory=list)
    message_count: int = 0
    request_count: int = 0  # every admitted request (message, decision or choice), for the per-session limit
    closed: bool = False  # set when the registry drops the session; a turn still running must not attach new state to it
    issues: IssueBook = field(default_factory=IssueBook)
    clarification: list[tuple[datetime, str]] = field(default_factory=list)
    choice: Optional[ChoiceState] = None
    pending_id: Optional[str] = None
    draft: Optional[dict[str, Any]] = None
    want_ticket: bool = False
    # What the customer was last asked while ``want_ticket`` is set: "" (what is the issue), "scope" (what went wrong)
    # or "service" (write the details of a service request such as a change of address).
    ticket_ask: str = ""
    # An offer of a support ticket made in text after a refused return: {"order_id", "item_id",
    # "stage": "offered" | "details", "expires"}. It creates nothing; a ticket still needs its own Confirm.
    ticket_offer: Optional[dict[str, Any]] = None
    committed: dict[str, int] = field(default_factory=lambda: {"return_request": 0, "support_ticket": 0})
    reserved: dict[str, int] = field(default_factory=lambda: {"return_request": 0, "support_ticket": 0})
    turn_lock: threading.Lock = field(default_factory=threading.Lock)


def _ref(handle: str) -> str:
    return hashlib.sha256(handle.encode()).hexdigest()[:10]


class SessionRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._pending: dict[str, PendingAction] = {}
        self._listeners: list = []
        self._sweeper: Optional[threading.Thread] = None
        self._sweeper_stop = threading.Event()
        self._create_client = SlidingWindowLimiter(settings.session_creates_per_client_per_minute, 60,
                                                   max_keys=max(1000, settings.max_active_sessions * 5))
        self._create_all = SlidingWindowLimiter(settings.session_creates_per_minute, 60, max_keys=4)
        self._requests = SlidingWindowLimiter(settings.session_requests_per_minute, 60,
                                              max_keys=settings.max_active_sessions * 2 + 10)

    # -- sessions -------------------------------------------------------

    def create(self, customer_id: str, display_name: str, client_key: Optional[str] = None) -> Session:
        """Create a session, or raise ``SessionLimitError``. Cap, rates and storage are one atomic step.

        ``client_key`` is whatever the caller can attribute the request to: the TCP peer address, or the browser identifier of a
        front end that authenticated itself (never a customer ID taken from the UI, never a forwarded header). The per-client
        rate needs an identity. A caller that cannot be attributed passes ``None`` and is NOT exempt: it counts against the
        GLOBAL creation rate and the cap. A refused attempt consumes no rate budget.
        """
        now = clock.now()
        dropped: list = []
        try:
            with self._lock:
                dropped = self._collect_expired_locked(now)
                if len(self._sessions) >= self.settings.max_active_sessions:
                    raise SessionLimitError("capacity", retry_after=30)
                if client_key is not None and not self._create_client.allow(client_key):
                    raise SessionLimitError("creation_rate", self._create_client.retry_after(client_key))
                if not self._create_all.allow("all"):
                    if client_key is not None:
                        self._create_client.release(client_key)
                    raise SessionLimitError("creation_rate", self._create_all.retry_after("all"))
                handle = secrets.token_urlsafe(32)
                session = Session(handle=handle, customer_id=customer_id, display_name=display_name,
                                  created=now, last_active=now, session_ref=_ref(handle))
                self._sessions[handle] = session
                return session
        finally:
            self._notify(dropped)

    def get(self, handle: Any) -> Optional[Session]:
        if not isinstance(handle, str):
            return None
        now = clock.now()
        dropped: list = []
        try:
            with self._lock:
                session = self._sessions.get(handle)
                if session is None:
                    return None
                reason = self._expiry_reason(session, now)
                if reason is not None and not self._busy_locked(session):
                    dropped = [self._pop_locked(handle, reason)]
                    return None
                return session  # a lookup is not activity: only ``touch`` (an accepted request) refreshes ``last_active``
        finally:
            self._notify(dropped)

    def touch(self, session: Session) -> None:
        """Mark the session active now. Called only for accepted activity, never for a refused or rate-limited request."""
        with self._lock:
            if not session.closed:
                session.last_active = clock.now()

    def end(self, handle: Any) -> bool:
        dropped: list = []
        with self._lock:
            if isinstance(handle, str) and handle in self._sessions:
                dropped = [self._pop_locked(handle, "ended")]
        self._notify(dropped)
        return bool(dropped)

    def sweep(self) -> int:
        """Remove every expired session that is not busy. Returns how many were removed."""
        with self._lock:
            dropped = self._collect_expired_locked(clock.now())
        self._notify(dropped)
        return len(dropped)

    def purge(self) -> None:
        self.sweep()

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def shutdown(self) -> None:
        """End every session (the application is stopping) and stop the sweeper."""
        self.stop_sweeper()
        with self._lock:
            dropped = [self._pop_locked(h, "shutdown") for h in list(self._sessions)]
        self._notify(dropped)

    # -- listeners and the background sweeper ----------------------------

    def add_drop_listener(self, listener) -> None:
        """``listener(session, reason)`` runs after a session is dropped (``ended``, ``idle``, ``age`` or ``shutdown``).

        It runs outside the registry lock and its failure is logged, never raised.
        """
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def start_sweeper(self, interval: Optional[float] = None) -> None:
        with self._lock:
            if self._sweeper is not None:
                return
            seconds = float(interval if interval is not None else self.settings.sweep_interval_seconds)
            self._sweeper_stop.clear()

            def loop() -> None:
                while not self._sweeper_stop.wait(seconds):
                    try:
                        self.sweep()
                    except Exception:
                        log.exception("session sweep failed")

            self._sweeper = threading.Thread(target=loop, name="session-sweeper", daemon=True)
            self._sweeper.start()

    def stop_sweeper(self) -> None:
        with self._lock:
            thread, self._sweeper = self._sweeper, None
        self._sweeper_stop.set()
        if thread is not None:
            thread.join(timeout=5)

    # -- per-session request admission ------------------------------------

    def admit_request(self, session: Session) -> tuple[Optional[str], int]:
        """``(None, 0)`` if the session may make another request, else ``("limit", 0)`` or ``("rate", seconds_to_wait)``."""
        with self._lock:
            if session.request_count >= self.settings.max_requests_per_session:
                return "limit", 0
            if not self._requests.allow(session.handle):
                return "rate", self._requests.retry_after(session.handle)
            session.request_count += 1
            return None, 0

    # -- internals ----------------------------------------------------------

    def _expiry_reason(self, session: Session, now: datetime) -> Optional[str]:
        if now - session.created > self.settings.max_session_age:
            return "age"
        if now - session.last_active > self.settings.idle_timeout:
            return "idle"
        return None

    @staticmethod
    def _busy_locked(session: Session) -> bool:
        """A turn is running, or a confirmed action is executing: the session must not be removed underneath it."""
        return session.turn_lock.locked() or any(n > 0 for n in session.reserved.values())

    def _collect_expired_locked(self, now: datetime) -> list:
        out = []
        for handle, session in list(self._sessions.items()):
            reason = self._expiry_reason(session, now)
            if reason is not None and not self._busy_locked(session):
                out.append(self._pop_locked(handle, reason))
        return out

    def _pop_locked(self, handle: str, reason: str) -> tuple:
        session = self._sessions.pop(handle)
        session.closed = True
        for action_id in [a for a, p in self._pending.items() if p.session_handle == handle]:
            del self._pending[action_id]
        self._requests.forget(handle)
        return session, reason

    def _notify(self, dropped: list) -> None:
        for session, reason in dropped:
            for listener in list(self._listeners):
                try:
                    listener(session, reason)
                except Exception:
                    log.exception("session drop listener failed (session %s)", session.session_ref)

    def _prune_pending_locked(self, session: Session) -> None:
        finished = [p for p in self._pending.values()
                    if p.session_handle == session.handle and p.state not in ("open", "claimed")]
        finished.sort(key=lambda p: p.created)
        for action in finished[: max(0, len(finished) - _KEEP_FINISHED_PENDING)]:
            self._pending.pop(action.action_id, None)

    # -- pending actions ------------------------------------------------

    def put_pending(self, session: Session, kind: str, payload: dict[str, Any],
                    summary: str) -> PendingAction:
        """Create the session's single open pending action (cancels any older one)."""
        now = clock.now()
        action = PendingAction(
            action_id=uuid.uuid4().hex, session_handle=session.handle,
            customer_id=session.customer_id, kind=kind, payload=payload, summary=summary,
            created=now, expires=now + self.settings.pending_ttl)
        with self._lock:
            if session.closed or self._sessions.get(session.handle) is not session:
                raise SessionClosedError()  # never recreate an entry for an ended session (it could not be reclaimed)
            if session.pending_id and session.pending_id in self._pending:
                old = self._pending[session.pending_id]
                if old.state == "open":
                    old.state = "cancelled"
            self._pending[action.action_id] = action
            session.pending_id = action.action_id
            self._prune_pending_locked(session)
        return action

    def open_pending(self, session: Session) -> Optional[PendingAction]:
        """The session's open, unexpired pending action, if any."""
        now = clock.now()
        with self._lock:
            action = self._pending.get(session.pending_id) if session.pending_id else None
            if action is None or action.state != "open":
                return None
            if now >= action.expires:
                action.state = "expired"
                return None
            return action

    def cancel(self, session: Session, action_id: Any) -> str:
        """Cancel an open action. Returns ``cancelled`` or a refusal reason."""
        now = clock.now()
        with self._lock:
            action = self._lookup_locked(session, action_id)
            if action is None:
                return "unknown"
            if action.state == "open" and now >= action.expires:
                action.state = "expired"
            if action.state != "open":
                return action.state
            action.state = "cancelled"
            return "cancelled"

    def claim(self, session: Session, action_id: Any) -> tuple[Optional[PendingAction], str]:
        """Atomically claim an open action for execution and reserve capacity.

        Returns ``(action, "claimed")`` or ``(None, reason)`` where reason is
        ``unknown``, ``expired``, ``claimed``, ``done``, ``cancelled`` or
        ``cap_reached``. Exactly one caller can ever receive the action.
        """
        now = clock.now()
        with self._lock:
            action = self._lookup_locked(session, action_id)
            if action is None:
                return None, "unknown"
            if action.state == "open" and now >= action.expires:
                action.state = "expired"
            if action.state != "open":
                return None, action.state
            used = session.committed[action.kind] + session.reserved[action.kind]
            cap = self.settings.cap_for(action.kind)
            if cap is not None and used >= cap:
                action.state = "cancelled"
                return None, "cap_reached"
            action.state = "claimed"
            session.reserved[action.kind] += 1
            return action, "claimed"

    def finish(self, session: Session, action: PendingAction, committed: bool) -> None:
        with self._lock:
            session.reserved[action.kind] = max(0, session.reserved[action.kind] - 1)
            if committed:
                session.committed[action.kind] += 1
            action.state = "done"

    def capacity_left(self, session: Session, kind: str) -> int:
        with self._lock:
            cap = self.settings.cap_for(kind)
            if cap is None:  # no per-session cap for this kind (support tickets)
                return sys.maxsize
            return cap - session.committed[kind] - session.reserved[kind]

    def _lookup_locked(self, session: Session, action_id: Any) -> Optional[PendingAction]:
        if not isinstance(action_id, str):
            return None
        action = self._pending.get(action_id)
        if action is None or action.session_handle != session.handle:
            return None
        if action.customer_id != session.customer_id:
            return None
        return action
