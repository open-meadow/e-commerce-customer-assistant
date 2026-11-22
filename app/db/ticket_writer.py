"""The real ticket writer (Phase 2): the concrete implementation of ``app.orchestrator.ticket_writer.TicketWriter``.

Production composition requires an instance of ``RepositoryTicketWriter`` and refuses anything else, so a fake writer can
never be selected from application code, the environment, an HTTP request, the UI or model output. The class is the
"real repository contract" the persistence gate's startup interlock checks for.

Fail closed: constructing the writer verifies, on the configured database, that the schema is at the current migrated
``SCHEMA_VERSION`` and that the contract it depends on is really there (a nullable ``conversation_summary`` and the
``ticket_write_intents`` table). Against an unmigrated (version-1) database the constructor raises
``TicketWriterContractError``; it never probes or migrates anything.

Outcome mapping (``create_ticket``): the repository's value outcomes map one to one; an error before the COMMIT is sent
means nothing was written, so it is ``unavailable``; an error while the COMMIT runs cannot be resolved, so it is
``outcome_unknown`` and the same request key and payload stay valid for a retry (``retry_ticket_request``).
"""
from __future__ import annotations

import logging
import sqlite3

from app.db import repository
from app.db.connection import SCHEMA_VERSION, get_connection
from app.db.errors import DatabaseError, DatabaseOutcomeUnknownError
from app.orchestrator.ticket_writer import (
    CREATED,
    DUPLICATE_ACTIVE,
    IDEMPOTENCY_CONFLICT,
    OUTCOME_UNKNOWN,
    RATE_LIMITED,
    UNAVAILABLE,
    TicketWriteRequest,
    TicketWriteResult,
)

_log = logging.getLogger(__name__)


class TicketWriterContractError(RuntimeError):
    """The database cannot serve the fresh-description write path (unmigrated, unreadable or missing the contract)."""


def verify_write_contract() -> None:
    """Raise ``TicketWriterContractError`` unless the configured database is migrated and carries the contract."""
    try:
        connection = get_connection()
    except DatabaseError as exc:
        raise TicketWriterContractError(
            f"the real ticket writer needs a database at schema version {SCHEMA_VERSION}: {type(exc).__name__}") from exc
    try:
        columns = {r["name"]: r for r in connection.execute("PRAGMA table_info(support_tickets)")}
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if ("conversation_summary" not in columns or columns["conversation_summary"]["notnull"] != 0
                or "request_key" not in columns or "ticket_write_intents" not in tables
                or "ticket_events" not in tables):
            raise TicketWriterContractError("the database lacks the Phase 2 ticket contract")
    except sqlite3.Error as exc:
        raise TicketWriterContractError("the database contract could not be read") from exc
    finally:
        connection.close()


class RepositoryTicketWriter:
    """Persist a confirmed, fresh-description ticket through the repository (atomic count-and-insert)."""

    def __init__(self) -> None:
        verify_write_contract()

    def create_ticket(self, request: TicketWriteRequest) -> TicketWriteResult:
        try:
            result = repository.create_ticket_from_description(
                request.customer_id, request.issue_type, request.description, request.request_key,
                order_id=request.order_id, item_id=request.item_id, rate_limit=request.rate_limit)
        except DatabaseOutcomeUnknownError:
            _log.warning("ticket write outcome unknown (request key kept for a retry)")
            return TicketWriteResult(OUTCOME_UNKNOWN)
        except (DatabaseError, ValueError, TypeError) as exc:
            _log.warning("ticket write refused before any write: %s", type(exc).__name__)
            self._close(request)
            return TicketWriteResult(UNAVAILABLE)
        outcome = result["outcome"]
        if outcome in ("created", "already_created"):
            return TicketWriteResult(CREATED, ticket_id=result["ticket_id"])
        if outcome == "duplicate_active":
            return TicketWriteResult(DUPLICATE_ACTIVE, ticket_id=result["ticket_id"], status=result["status"])
        if outcome == "idempotency_conflict":
            return TicketWriteResult(IDEMPOTENCY_CONFLICT)
        if outcome == "rate_limited":
            return TicketWriteResult(RATE_LIMITED, retry_at=result["retry_at"])
        return TicketWriteResult(UNAVAILABLE)  # item_not_found, parent_not_found: definite, nothing written

    @staticmethod
    def _close(request: TicketWriteRequest) -> None:
        """Best effort: the customer is told nothing was saved, so the stored request must not be replayed later."""
        try:
            repository.close_ticket_request(request.customer_id, request.request_key)
        except Exception:  # the database is the very thing that failed; nothing more can be done here
            _log.warning("could not close an unavailable ticket request")

    def retry_unresolved(self, customer_id: str, request_key: str, rate_limit=None) -> TicketWriteResult:
        """Recovery: replay a persisted request with its stored key and immutable payload (never a new payload)."""
        try:
            result = repository.retry_ticket_request(customer_id, request_key, rate_limit=rate_limit)
        except DatabaseOutcomeUnknownError:
            return TicketWriteResult(OUTCOME_UNKNOWN)
        except (DatabaseError, ValueError, TypeError):
            return TicketWriteResult(UNAVAILABLE)
        outcome = result["outcome"]
        if outcome in ("created", "already_created"):
            return TicketWriteResult(CREATED, ticket_id=result["ticket_id"])
        if outcome == "duplicate_active":
            return TicketWriteResult(DUPLICATE_ACTIVE, ticket_id=result["ticket_id"], status=result["status"])
        if outcome == "idempotency_conflict":
            return TicketWriteResult(IDEMPOTENCY_CONFLICT)
        if outcome == "rate_limited":
            return TicketWriteResult(RATE_LIMITED, retry_at=result["retry_at"])
        return TicketWriteResult(UNAVAILABLE)

    def reconcile(self, customer_id: str, rate_limit=None) -> list[TicketWriteResult]:
        """Recovery after session expiry or a restart: replay the customer's own unresolved requests, oldest first.

        Each stored request is replayed with its ORIGINAL key and immutable payload through ``retry_unresolved`` (so it can never
        create a second ticket). Stops at the first request whose outcome is still uncertain, so nothing later is attempted past
        it. Customer-scoped in SQL; another customer's intents are never listed. An empty list means nothing is unresolved.
        """
        try:
            pending = repository.list_unresolved_ticket_requests(customer_id)
        except (DatabaseError, ValueError, TypeError):
            return [TicketWriteResult(OUTCOME_UNKNOWN)]  # cannot even read the intents: do not assume there are none
        results: list[TicketWriteResult] = []
        for entry in pending:
            result = self.retry_unresolved(customer_id, entry["request_key"], rate_limit=rate_limit)
            results.append(result)
            if result.outcome in (OUTCOME_UNKNOWN, UNAVAILABLE):
                break
        return results
