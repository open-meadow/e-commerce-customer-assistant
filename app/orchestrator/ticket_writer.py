"""The ticket writer seam (Phase 1.1 interface, U-4).

The engine's fresh-description flow persists a confirmed ticket ONLY through a ``TicketWriter``. This module holds the
interface and the result vocabulary and nothing else: it imports no database code. The real implementation is
``app.db.ticket_writer.RepositoryTicketWriter`` (Phase 2); tests inject a fake directly into an ``Engine``. Production
composition (``app.backend.api.Backend``) accepts only the real class, so a fake can never be selected from application
code, the environment, an HTTP request, the UI or model output.

Result outcomes (``TicketWriteResult.outcome``):

* ``created``              the only success; ``ticket_id`` is the writer's own, verified ID
* ``duplicate_active``     a new request for an issue that already has an active ticket; nothing was written and
                           ``ticket_id`` and ``status`` name the existing ticket (idempotency and duplicate detection are
                           different operations; there is no offer to add information in Phase 2)
* ``idempotency_conflict`` the same request key arrived with a different payload; nothing was written
* ``rate_limited``         the account-level limit applies; nothing was written; ``retry_at`` is set
* ``not_enabled``          defensive failure: the flow has no real writer; nothing was written
* ``unavailable``          the writer can establish that NO ticket was created
* ``outcome_unknown``      the writer cannot establish whether a ticket was created; the request key and payload stay
                           valid for a retry of the same confirmation
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

CREATED = "created"
DUPLICATE_ACTIVE = "duplicate_active"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
RATE_LIMITED = "rate_limited"
NOT_ENABLED = "not_enabled"
UNAVAILABLE = "unavailable"
OUTCOME_UNKNOWN = "outcome_unknown"

OUTCOMES = frozenset({CREATED, DUPLICATE_ACTIVE, IDEMPOTENCY_CONFLICT, RATE_LIMITED, NOT_ENABLED, UNAVAILABLE,
                      OUTCOME_UNKNOWN})


@dataclass(frozen=True)
class TicketWriteRequest:
    """One confirmed ticket creation. Immutable: a retry of the same confirmation sends an identical request."""
    customer_id: str                      # the authenticated customer; never taken from customer text or the model
    issue_type: str                       # one of the five approved categories
    description: str                      # exactly what the customer entered, verbatim (OD-24)
    request_key: str                      # stable per confirmation card; reused for every retry of that confirmation
    order_id: Optional[str] = None        # only an explicitly selected, server-verified order of this customer
    rate_limit: Optional[tuple[int, int]] = None   # (maximum, window_seconds) or None when the policy is off
    item_id: Optional[str] = None         # Phase 2 only; the Phase 1.1 engine never sets it (U-5)


@dataclass(frozen=True)
class TicketWriteResult:
    outcome: str
    ticket_id: Optional[str] = None
    status: Optional[str] = None
    retry_at: Optional[str] = None


class TicketWriter(Protocol):
    def create_ticket(self, request: TicketWriteRequest) -> TicketWriteResult:
        """Create the ticket atomically (rate-limit count and insert in one transaction) or report why not."""
        ...
