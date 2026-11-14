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
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.backend import clock
from app.backend.config import Settings
from app.orchestrator.issues import IssueBook


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
    history: list[dict[str, str]] = field(default_factory=list)
    message_count: int = 0
    issues: IssueBook = field(default_factory=IssueBook)
    clarification: list[tuple[datetime, str]] = field(default_factory=list)
    choice: Optional[ChoiceState] = None
    pending_id: Optional[str] = None
    draft: Optional[dict[str, Any]] = None
    want_ticket: bool = False
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

    # -- sessions -------------------------------------------------------

    def create(self, customer_id: str, display_name: str) -> Session:
        now = clock.now()
        handle = secrets.token_urlsafe(32)
        session = Session(handle=handle, customer_id=customer_id, display_name=display_name,
                          created=now, last_active=now, session_ref=_ref(handle))
        with self._lock:
            self._purge_locked(now)
            self._sessions[handle] = session
        return session

    def get(self, handle: Any) -> Optional[Session]:
        if not isinstance(handle, str):
            return None
        now = clock.now()
        with self._lock:
            session = self._sessions.get(handle)
            if session is None:
                return None
            if now - session.last_active > self.settings.idle_timeout:
                self._drop_locked(handle)
                return None
            session.last_active = now
            return session

    def end(self, handle: Any) -> bool:
        with self._lock:
            if handle in self._sessions:
                self._drop_locked(handle)
                return True
            return False

    def purge(self) -> None:
        with self._lock:
            self._purge_locked(clock.now())

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _purge_locked(self, now: datetime) -> None:
        for handle in [h for h, s in self._sessions.items()
                       if now - s.last_active > self.settings.idle_timeout]:
            self._drop_locked(handle)

    def _drop_locked(self, handle: str) -> None:
        self._sessions.pop(handle, None)
        for action_id in [a for a, p in self._pending.items() if p.session_handle == handle]:
            del self._pending[action_id]

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
            if session.pending_id and session.pending_id in self._pending:
                old = self._pending[session.pending_id]
                if old.state == "open":
                    old.state = "cancelled"
            self._pending[action.action_id] = action
            session.pending_id = action.action_id
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
            if used >= self.settings.cap_for(action.kind):
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
            return (self.settings.cap_for(kind)
                    - session.committed[kind] - session.reserved[kind])

    def _lookup_locked(self, session: Session, action_id: Any) -> Optional[PendingAction]:
        if not isinstance(action_id, str):
            return None
        action = self._pending.get(action_id)
        if action is None or action.session_handle != session.handle:
            return None
        if action.customer_id != session.customer_id:
            return None
        return action
