"""Customer-scoped database repository (docs/01 Sections E to H, D2 contracts).

This module is the only place in the application permitted to run SQL
against customer-owned data. Every function that touches customer data
takes ``authenticated_customer_id`` and uses it inside the SQL ``WHERE``
clause, never as a post-fetch filter. The ID must come from trusted
application context, never from a message or an LLM argument.

Connections
-----------
Every function opens its own connection through
``get_connection()`` (``mode=rw``, never creates a database, checks the
schema version) and closes it before returning. No connection or cursor
is ever returned to a caller.

Time
----
All times come from ``app.db.clock.utc_now()``, captured **once** per
operation (after ``BEGIN IMMEDIATE`` for writes). Callers and LLM tools
cannot supply timestamps. Persisted timestamps are validated whenever
the relevant rows are read or written.

Errors (docs/01 Section F item 8)
---------------------------------
Business outcomes are returned in result dictionaries. Infrastructure
failures raise typed errors from ``app.db.errors``: ``ConnectionError_``
(and subclasses), ``DatabaseOperationalError``,
``DatabaseConstraintError`` and ``DatabaseDataIntegrityError``. Wrong
argument types raise ``TypeError`` and invalid values ``ValueError``,
before any database work. Non-database exceptions are rolled back and
re-raised unchanged. A rollback or close failure never replaces the
original error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from app.db import clock
from app.db.connection import get_connection
from app.db.errors import (
    DatabaseConstraintError,
    DatabaseDataIntegrityError,
    DatabaseOperationalError,
    DatabaseOutcomeUnknownError,
)
from app.db.validation import (
    RETURN_REASON_MAX,
    TICKET_TEXT_MAX,
    validate_category,
    validate_identifier,
    validate_issue_type,
    validate_text,
)

_ORDER_COLUMNS = (
    "order_id, customer_id, status, payment_status, refund_status, "
    "refund_amount, order_date, delivered_at, total_amount"
)
_ORDER_ITEM_COLUMNS = "item_id, order_id, product_id, quantity, price"
_RETURN_REQUEST_COLUMNS = (
    "return_id, customer_id, order_id, item_id, reason, reason_category, "
    "status, requested_at"
)
_SUPPORT_TICKET_COLUMNS = (
    "ticket_id, customer_id, order_id, issue_type, description, "
    "conversation_summary, status, priority, created_at"
)

# reason_category -> window length in hours, measured from delivered_at.
_RETURN_WINDOW_HOURS = {"damaged": 48, "wrong_product": 48, "standard": 168}

ELIGIBLE = "eligible"
ORDER_NOT_FOUND = "order_not_found"
ORDER_STATUS_INELIGIBLE = "order_status_ineligible"
ITEM_NOT_FOUND = "item_not_found"
ACTIVE_RETURN_EXISTS = "active_return_exists"
OUTSIDE_RETURN_WINDOW = "outside_return_window"
DELIVERY_TIMESTAMP_MISSING = "delivery_timestamp_missing"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _close_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001
        pass


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:  # noqa: BLE001
        pass


_log = logging.getLogger(__name__)


def _new_id(prefix: str) -> str:
    """``PREFIX_`` plus 32 lowercase hex characters from one UUID4."""
    return f"{prefix}_{uuid.uuid4().hex}"


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _run_read(work: Callable[[sqlite3.Connection, datetime], Any]) -> Any:
    """Run *work* on a fresh connection with one captured ``now``."""
    connection = get_connection()
    try:
        return work(connection, clock.utc_now())
    except sqlite3.Error as exc:
        raise DatabaseOperationalError("database read failed") from exc
    finally:
        _close_quietly(connection)


def _check_order_row(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    clock.validate_persisted_timestamp(row["order_date"], "orders.order_date", now)
    clock.validate_persisted_timestamp(
        row["delivered_at"], "orders.delivered_at", now, nullable=True
    )
    return row


def _is_active_return_violation(exc: sqlite3.IntegrityError) -> bool:
    return (
        getattr(exc, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_UNIQUE"
        and "return_requests.item_id" in str(exc)
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_customers_for_login() -> list[dict[str, Any]]:
    """Return ``customer_id`` and ``name`` of every customer.

    The one read that is not scoped to an authenticated customer: it
    exists only for the simulated login selector and is never an LLM
    tool. Ordered by ``name`` then ``customer_id``. Returns no other
    field.
    """
    def work(connection: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT customer_id, name FROM customers ORDER BY name ASC, customer_id ASC"
        ).fetchall()
        return _rows_to_dicts(rows)

    return _run_read(work)


def list_customer_orders(authenticated_customer_id: str) -> list[dict[str, Any]]:
    """Return every order owned by the authenticated customer."""
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")

    def work(connection: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders WHERE customer_id = ? "
            "ORDER BY order_id ASC",
            (authenticated_customer_id,),
        ).fetchall()
        return [_check_order_row(dict(row), now) for row in rows]

    return _run_read(work)


def get_order(
    authenticated_customer_id: str, order_id: str
) -> Optional[dict[str, Any]]:
    """Return one order if it exists and is owned, otherwise ``None``.

    A nonexistent order and another customer's order are
    indistinguishable.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    validate_identifier(order_id, "order_id")

    def work(connection: sqlite3.Connection, now: datetime) -> Optional[dict[str, Any]]:
        row = connection.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders "
            "WHERE customer_id = ? AND order_id = ?",
            (authenticated_customer_id, order_id),
        ).fetchone()
        return _check_order_row(dict(row), now) if row is not None else None

    return _run_read(work)


def get_order_items(
    authenticated_customer_id: str, order_id: str
) -> list[dict[str, Any]]:
    """Return an owned order's items; empty for missing or unowned orders.

    One query joins ``order_items`` to ``orders`` and filters on
    ``orders.customer_id``, so ownership is enforced inside it.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    validate_identifier(order_id, "order_id")

    def work(connection: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
        columns = ", ".join(
            f"order_items.{c.strip()}" for c in _ORDER_ITEM_COLUMNS.split(",")
        )
        rows = connection.execute(
            f"SELECT {columns} FROM order_items "
            "JOIN orders ON orders.order_id = order_items.order_id "
            "WHERE orders.customer_id = ? AND order_items.order_id = ? "
            "ORDER BY order_items.item_id ASC",
            (authenticated_customer_id, order_id),
        ).fetchall()
        return _rows_to_dicts(rows)

    return _run_read(work)


def get_customer_returns(authenticated_customer_id: str) -> list[dict[str, Any]]:
    """Return the customer's return requests, including ``reason_category``.

    Ordered by ``requested_at`` descending, then ``return_id`` descending.
    This is the full owner-scoped record. LLM-visible projections are
    built by the backend, not here.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")

    def work(connection: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"SELECT {_RETURN_REQUEST_COLUMNS} FROM return_requests "
            "WHERE customer_id = ? ORDER BY requested_at DESC, return_id DESC",
            (authenticated_customer_id,),
        ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            clock.validate_persisted_timestamp(
                row["requested_at"], "return_requests.requested_at", now
            )
        return result

    return _run_read(work)


def get_customer_tickets(authenticated_customer_id: str) -> list[dict[str, Any]]:
    """Return the customer's support tickets, most recent first.

    Ordered by ``created_at`` descending, then ``ticket_id`` ascending.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")

    def work(connection: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"SELECT {_SUPPORT_TICKET_COLUMNS} FROM support_tickets "
            "WHERE customer_id = ? ORDER BY created_at DESC, ticket_id ASC",
            (authenticated_customer_id,),
        ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            clock.validate_persisted_timestamp(
                row["created_at"], "support_tickets.created_at", now
            )
        return result

    return _run_read(work)


def list_customer_tickets_page(authenticated_customer_id: str, offset: int = 0, limit: int = 10) -> dict[str, Any]:
    """One page of the customer's tickets, most recent first (``created_at`` descending, then ``ticket_id`` ascending).

    Returns ``{"tickets": [...], "total": n, "skipped": k}``. A row whose stored timestamp fails validation (for example one
    dated in the future after a clock correction) is SKIPPED and counted instead of making the whole list unavailable;
    ``total`` counts only the valid rows. ``get_customer_tickets`` is unchanged and still fails on such a row."""
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("'offset' must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("'limit' must be a positive integer")

    def work(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        rows = connection.execute(
            f"SELECT {_SUPPORT_TICKET_COLUMNS} FROM support_tickets "
            "WHERE customer_id = ? ORDER BY created_at DESC, ticket_id ASC",
            (authenticated_customer_id,),
        ).fetchall()
        valid: list[dict[str, Any]] = []
        skipped = 0
        for row in rows:
            record = dict(row)
            try:
                clock.validate_persisted_timestamp(record["created_at"], "support_tickets.created_at", now)
            except DatabaseDataIntegrityError:
                skipped += 1
                # opaque references only: a hashed customer reference and the ticket's own opaque ID, never any ticket text
                _log.warning("support ticket row skipped (invalid stored timestamp) customer_ref=%s ticket=%s",
                             hashlib.sha256(authenticated_customer_id.encode()).hexdigest()[:10], record.get("ticket_id"))
                continue
            valid.append(record)
        return {"tickets": valid[offset:offset + limit], "total": len(valid), "skipped": skipped}

    return _run_read(work)


# ---------------------------------------------------------------------------
# Return eligibility (shared by the advisory read and the authoritative write)
# ---------------------------------------------------------------------------

def _eligibility_result(outcome: str, category: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "outcome": outcome,
        "reason_category": category,
        "order_status": None,
        "window_hours": None,
        "delivered_at": None,
        "window_closes_at": None,
        "evaluated_at": None,
        "window_reason": None,
        "existing_return_status": None,
    }
    result.update(fields)
    return result


def _evaluate_return(
    connection: sqlite3.Connection,
    customer_id: str,
    order_id: str,
    item_id: str,
    category: str,
    now: datetime,
) -> dict[str, Any]:
    """Apply the D2 eligibility precedence on *connection* at instant *now*.

    Never begins or commits a transaction. Precedence: order ownership,
    order status, item, active return, delivery timestamp and window.
    """
    row = connection.execute(
        f"SELECT {_ORDER_COLUMNS} FROM orders WHERE customer_id = ? AND order_id = ?",
        (customer_id, order_id),
    ).fetchone()
    if row is None:
        return _eligibility_result(ORDER_NOT_FOUND, category)
    order = _check_order_row(dict(row), now)
    status = order["status"]
    if status != "Delivered":
        return _eligibility_result(ORDER_STATUS_INELIGIBLE, category, order_status=status)

    item = connection.execute(
        "SELECT 1 FROM order_items WHERE order_id = ? AND item_id = ?",
        (order_id, item_id),
    ).fetchone()
    if item is None:
        return _eligibility_result(ITEM_NOT_FOUND, category, order_status=status)

    active = connection.execute(
        "SELECT status FROM return_requests "
        "WHERE customer_id = ? AND item_id = ? AND status IN ('Requested', 'Approved')",
        (customer_id, item_id),
    ).fetchone()
    if active is not None:
        return _eligibility_result(
            ACTIVE_RETURN_EXISTS,
            category,
            order_status=status,
            existing_return_status=active["status"],
        )

    window_hours = _RETURN_WINDOW_HOURS[category]
    evaluated_at = clock.format_timestamp(now)
    if order["delivered_at"] is None:
        return _eligibility_result(
            DELIVERY_TIMESTAMP_MISSING,
            category,
            order_status=status,
            window_hours=window_hours,
            evaluated_at=evaluated_at,
            window_reason="delivery_not_recorded",
        )
    delivered = clock.parse_timestamp(order["delivered_at"])
    closes = delivered + timedelta(hours=window_hours)
    outcome = ELIGIBLE if now <= closes else OUTSIDE_RETURN_WINDOW
    return _eligibility_result(
        outcome,
        category,
        order_status=status,
        window_hours=window_hours,
        delivered_at=order["delivered_at"],
        window_closes_at=clock.format_timestamp(closes),
        evaluated_at=evaluated_at,
        window_reason=None if outcome == ELIGIBLE else "expired",
    )


def check_return_eligibility(
    authenticated_customer_id: str,
    order_id: str,
    item_id: str,
    *,
    reason_category: str,
) -> dict[str, Any]:
    """Advisory, read-only return pre-check (never writes).

    Runs in one short read transaction with one captured ``now``. The
    result's ``order_status`` is set only for an owned order. ``eligible``
    is a preliminary result and not a guarantee: ``create_return_request``
    rechecks everything and is authoritative. It does not know whether a
    product is returnable under every policy condition.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    validate_identifier(order_id, "order_id")
    validate_identifier(item_id, "item_id")
    validate_category(reason_category)

    connection = get_connection()
    try:
        connection.execute("BEGIN")
        now = clock.utc_now()
        result = _evaluate_return(
            connection, authenticated_customer_id, order_id, item_id,
            reason_category, now,
        )
        return result
    except sqlite3.Error as exc:
        raise DatabaseOperationalError("eligibility check failed") from exc
    finally:
        _rollback_quietly(connection)
        _close_quietly(connection)


def check_return_windows(
    authenticated_customer_id: str,
    order_id: str,
    item_id: str,
) -> dict[str, dict[str, Any]]:
    """Advisory, read-only: the standard and the damaged/wrong-product verdicts at ONE instant.

    ``damaged`` and ``wrong_product`` share the same 48 hour window, so ``damaged`` stands for both. Both
    verdicts come from the same read transaction and the same captured ``now``, so their ``evaluated_at``
    values are identical and the two windows can never disagree about the moment they were judged.
    Order ownership, status, item and active-return outcomes do not depend on the category, so they are
    the same in both verdicts. Never writes. ``create_return_request`` stays authoritative: it rechecks
    everything at confirmation time.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    validate_identifier(order_id, "order_id")
    validate_identifier(item_id, "item_id")

    connection = get_connection()
    try:
        connection.execute("BEGIN")
        now = clock.utc_now()
        return {
            category: _evaluate_return(connection, authenticated_customer_id, order_id, item_id, category, now)
            for category in ("standard", "damaged")
        }
    except sqlite3.Error as exc:
        raise DatabaseOperationalError("eligibility check failed") from exc
    finally:
        _rollback_quietly(connection)
        _close_quietly(connection)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _run_write(
    work: Callable[[sqlite3.Connection, datetime], tuple[dict[str, Any], bool]],
) -> dict[str, Any]:
    """Run *work* inside ``BEGIN IMMEDIATE`` with one captured ``now``.

    *work* returns ``(result, commit)``. The result is returned only
    after a successful COMMIT when ``commit`` is true.
    """
    connection = get_connection()
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = clock.utc_now()
            result, commit = work(connection, now)
            if commit:
                connection.commit()
            else:
                connection.rollback()
            return result
        except sqlite3.IntegrityError as exc:
            _rollback_quietly(connection)
            raise DatabaseConstraintError("a database constraint was violated") from exc
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise DatabaseOperationalError("database write failed") from exc
        except BaseException:
            _rollback_quietly(connection)
            raise
    finally:
        _close_quietly(connection)


def create_return_request(
    authenticated_customer_id: str,
    order_id: str,
    item_id: str,
    reason: str,
    *,
    reason_category: str,
) -> dict[str, Any]:
    """Create a return request after rechecking every rule in the transaction.

    ``reason`` is the customer's own explanation, stored exactly as
    accepted (no prefix, no stripping). ``reason_category`` is selected
    by the customer, never by the LLM. Returns
    ``{"outcome": "created", "return_id": "RET_<32 lowercase hex>"}`` or
    ``{"outcome": <ineligible outcome>, "return_id": None}``. The result
    never carries ``order_status``. One captured timestamp serves both
    the eligibility rule and ``requested_at``.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    validate_identifier(order_id, "order_id")
    validate_identifier(item_id, "item_id")
    validate_text(reason, "reason", RETURN_REASON_MAX)
    validate_category(reason_category)

    def work(connection: sqlite3.Connection, now: datetime) -> tuple[dict[str, Any], bool]:
        verdict = _evaluate_return(
            connection, authenticated_customer_id, order_id, item_id,
            reason_category, now,
        )
        if verdict["outcome"] != ELIGIBLE:
            return {"outcome": verdict["outcome"], "return_id": None}, False
        return_id = _new_id("RET")
        try:
            connection.execute(
                "INSERT INTO return_requests "
                "(return_id, customer_id, order_id, item_id, reason, "
                "reason_category, status, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'Requested', ?)",
                (
                    return_id, authenticated_customer_id, order_id, item_id,
                    reason, reason_category, clock.format_timestamp(now),
                ),
            )
        except sqlite3.IntegrityError as exc:
            if _is_active_return_violation(exc):
                return {"outcome": ACTIVE_RETURN_EXISTS, "return_id": None}, False
            raise
        return {"outcome": "created", "return_id": return_id}, True

    return _run_write(work)


def _validate_rate_limit(rate_limit: Optional[tuple[int, int]]) -> Optional[tuple[int, int]]:
    if rate_limit is None:
        return None
    try:
        maximum, window = rate_limit
    except (TypeError, ValueError):
        raise ValueError("'rate_limit' must be (maximum, window_seconds)") from None
    for value in (maximum, window):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("'rate_limit' values must be positive integers")
    return maximum, window


def _rate_state(connection: sqlite3.Connection, now: datetime, customer_id: str,
                limit: tuple[int, int]) -> dict[str, Any]:
    """The customer's rolling-window count against ``limit``, read on ``connection`` (inside the caller's transaction)."""
    maximum, window = limit
    cutoff = clock.format_timestamp(now - timedelta(seconds=window))
    count = connection.execute(
        "SELECT COUNT(*) FROM support_tickets WHERE customer_id = ? AND created_at > ?",
        (customer_id, cutoff),
    ).fetchone()[0]
    if count < maximum:
        return {"allowed": True, "retry_at": None, "count": count}
    oldest = connection.execute(
        "SELECT created_at FROM support_tickets WHERE customer_id = ? AND created_at > ? "
        "ORDER BY created_at ASC LIMIT 1 OFFSET ?",
        (customer_id, cutoff, count - maximum),
    ).fetchone()[0]
    retry_at = clock.format_timestamp(clock.parse_timestamp(oldest) + timedelta(seconds=window))
    return {"allowed": False, "retry_at": retry_at, "count": count}


def get_ticket_rate_state(authenticated_customer_id: str, rate_limit: tuple[int, int]) -> dict[str, Any]:
    """Read-only view of the account-level limit for the customer: ``{"allowed", "retry_at", "count"}``.

    Used by the engine as a courtesy check before it shows a confirmation card; the authority is the check inside
    ``create_support_ticket``'s transaction."""
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    limit = _validate_rate_limit(rate_limit)
    assert limit is not None
    return _run_read(lambda connection, now: _rate_state(connection, now, authenticated_customer_id, limit))


def create_support_ticket(
    authenticated_customer_id: str,
    issue_type: str,
    description: str,
    conversation_summary: str,
    order_id: Optional[str] = None,
    rate_limit: Optional[tuple[int, int]] = None,
) -> dict[str, Any]:
    """Create a support ticket, optionally linked to an owned order.

    ``rate_limit`` is ``(maximum, window_seconds)`` or ``None`` (no account-level limit). When given, the customer's tickets
    created in the rolling UTC window ``(now - window, now]`` are counted INSIDE this transaction (``BEGIN IMMEDIATE``), so
    concurrent sessions and processes on one database file cannot both pass the check; at or above ``maximum`` nothing is
    written and ``{"outcome": "rate_limited", "retry_at": <UTC text>}`` is returned. Every committed ticket counts, whatever
    its status; a row exactly one window old no longer counts. ``retry_at`` is when the count first drops below the maximum.

    ``issue_type`` must be one of ``order``, ``return``, ``payment``,
    ``product``, ``general``. ``description`` and ``conversation_summary``
    are nonblank strings of at most 2,000 code points, stored exactly as
    accepted. Status is ``Open`` and priority ``Normal``, set here and
    not overridable. A nonexistent or unowned ``order_id`` produces a
    general ticket with ``order_id`` NULL, with the same result, so the
    existence of another customer's order is never disclosed.

    Returns ``{"outcome": "created", "ticket_id": "TKT_<32 lowercase hex>"}``.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    validate_issue_type(issue_type)
    validate_text(description, "description", TICKET_TEXT_MAX)
    validate_text(conversation_summary, "conversation_summary", TICKET_TEXT_MAX)
    if order_id is not None:
        validate_identifier(order_id, "order_id")
    limit = _validate_rate_limit(rate_limit)

    def work(connection: sqlite3.Connection, now: datetime) -> tuple[dict[str, Any], bool]:
        linked: Optional[str] = None
        if order_id is not None:
            owned = connection.execute(
                "SELECT 1 FROM orders WHERE customer_id = ? AND order_id = ?",
                (authenticated_customer_id, order_id),
            ).fetchone()
            if owned is not None:
                linked = order_id
        if limit is not None:
            state = _rate_state(connection, now, authenticated_customer_id, limit)
            if not state["allowed"]:
                return {"outcome": "rate_limited", "retry_at": state["retry_at"]}, False
        ticket_id = _new_id("TKT")
        stamp = clock.format_timestamp(now)
        # [R] deviation from the design wording (U-1): this gate-OFF legacy path keeps its signature and its summary
        # semantics. It fills the Phase 2 columns itself: a server-generated request key, no idempotency hash, an ``n:``
        # issue key (never deduplicated by the database) and a ``created`` event.
        connection.execute(
            "INSERT INTO support_tickets "
            "(ticket_id, customer_id, order_id, item_id, issue_type, issue_key, description, "
            "conversation_summary, status, priority, parent_ticket_id, request_key, request_hash, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 'Open', 'Normal', NULL, ?, 'legacy', ?, ?)",
            (
                ticket_id, authenticated_customer_id, linked, issue_type, f"n:{ticket_id}",
                description, conversation_summary, f"legacy:{ticket_id}", stamp, stamp,
            ),
        )
        _insert_event(connection, ticket_id=ticket_id, customer_id=authenticated_customer_id, kind="created",
                      actor_type="customer", actor_ref=f"customer:{authenticated_customer_id}",
                      request_key=f"legacy:{ticket_id}", request_hash="legacy", created_at=stamp)
        return {"outcome": "created", "ticket_id": ticket_id}, True

    return _run_write(work)


# ---------------------------------------------------------------------------
# Phase 2: tickets with events, idempotency, duplicate detection and durable recovery
# ---------------------------------------------------------------------------

TICKET_STATUSES = ("New", "Open", "In Progress", "Pending", "Waiting for customer", "On hold",
                   "Resolved", "Reopened", "Closed", "Cancelled")
TERMINAL_STATUSES = ("Resolved", "Closed", "Cancelled")
COMMENTS_MAX_DEFAULT = 100                 # OD-09 proposed starting value (configuration, not a constant of the schema)
REOPEN_WINDOW_SECONDS_DEFAULT = 604800     # OD-03 proposed starting value: seven days
REQUEST_KEY_MAX = 200                      # leaves room for the '#status' suffix inside the 256-character column check
COMMENT_TEXT_MAX = 2000

_TERMINAL_SQL = "('Resolved', 'Closed', 'Cancelled')"


def _validate_request_key(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("'request_key' must be a string")
    if not value or len(value) > REQUEST_KEY_MAX or "\x00" in value:
        raise ValueError("'request_key' must be 1 to 200 characters without NUL")
    return value


def _canonical_payload(issue_type: str, order_id: Optional[str], item_id: Optional[str],
                       parent_ticket_id: Optional[str], description: str) -> str:
    """The immutable request payload: exactly the fields the design hashes. No summary and no chat content."""
    return json.dumps({"description": description, "issue_type": issue_type, "item_id": item_id,
                       "order_id": order_id, "parent_ticket_id": parent_ticket_id},
                      sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _issue_key(issue_type: str, order_id: Optional[str], item_id: Optional[str], ticket_id: str) -> str:
    """The one place an issue identity is built (design 3.1). Never supplied by the model or the customer."""
    if issue_type in ("product", "return") and order_id is not None and item_id is not None:
        return f"i:{order_id}:{item_id}"
    if issue_type in ("order", "payment") and order_id is not None and item_id is None:
        return f"o:{order_id}"
    return f"n:{ticket_id}"


def _next_seq(connection: sqlite3.Connection, ticket_id: str) -> int:
    return connection.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM ticket_events WHERE ticket_id = ?", (ticket_id,)).fetchone()[0]


def _insert_event(connection: sqlite3.Connection, *, ticket_id: str, customer_id: str, kind: str, actor_type: str,
                  actor_ref: str, request_key: str, request_hash: str, created_at: str, body: Optional[str] = None,
                  intent: Optional[str] = None, staff_review_required: int = 0, from_status: Optional[str] = None,
                  to_status: Optional[str] = None, transition_reason: Optional[str] = None,
                  trigger_event_id: Optional[str] = None) -> str:
    event_id = _new_id("EVT")
    connection.execute(
        "INSERT INTO ticket_events (event_id, ticket_id, customer_id, seq, kind, actor_type, actor_ref, body, intent, "
        "staff_review_required, from_status, to_status, transition_reason, trigger_event_id, request_key, request_hash, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, ticket_id, customer_id, _next_seq(connection, ticket_id), kind, actor_type, actor_ref, body, intent,
         staff_review_required, from_status, to_status, transition_reason, trigger_event_id, request_key, request_hash,
         created_at),
    )
    return event_id


def _run_write_tracked(
    work: Callable[[sqlite3.Connection, datetime], tuple[dict[str, Any], bool]],
) -> dict[str, Any]:
    """Like ``_run_write`` but a failure of the COMMIT itself raises ``DatabaseOutcomeUnknownError``.

    An error before the COMMIT is sent means nothing was written (the transaction is rolled back). An error while the
    COMMIT is running means the outcome cannot be established, so the caller must neither claim success nor claim that
    nothing was saved. Business outcomes are still returned as values.
    """
    connection = get_connection()
    committing = False
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = clock.utc_now()
            result, commit = work(connection, now)
            if commit:
                committing = True
                connection.commit()
                committing = False
            else:
                connection.rollback()
            return result
        except sqlite3.Error as exc:
            if committing:
                raise DatabaseOutcomeUnknownError("the outcome of a database write could not be established") from exc
            _rollback_quietly(connection)
            if isinstance(exc, sqlite3.IntegrityError):
                raise DatabaseConstraintError("a database constraint was violated") from exc
            raise DatabaseOperationalError("database write failed") from exc
        except BaseException:
            _rollback_quietly(connection)
            raise
    finally:
        _close_quietly(connection)


def _ticket_by_key(connection: sqlite3.Connection, customer_id: str, request_key: str) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT ticket_id, request_hash, status FROM support_tickets WHERE customer_id = ? AND request_key = ?",
        (customer_id, request_key)).fetchone()


def _prepare_intent(customer_id: str, request_key: str, payload_json: str, request_hash: str) -> dict[str, Any]:
    """Transaction 1: persist the request key and the immutable payload BEFORE the insert is attempted (durable half of
    idempotency). Returns ``prepared``, or ``already_created`` / ``idempotency_conflict`` without writing."""
    def work(connection: sqlite3.Connection, now: datetime) -> tuple[dict[str, Any], bool]:
        existing = _ticket_by_key(connection, customer_id, request_key)
        if existing is not None:
            if existing["request_hash"] == request_hash:
                return {"outcome": "already_created", "ticket_id": existing["ticket_id"]}, False
            return {"outcome": "idempotency_conflict"}, False
        intent = connection.execute(
            "SELECT request_hash, state, ticket_id FROM ticket_write_intents WHERE customer_id = ? AND request_key = ?",
            (customer_id, request_key)).fetchone()
        if intent is not None:
            if intent["request_hash"] != request_hash:
                return {"outcome": "idempotency_conflict"}, False
            if intent["state"] == "committed":
                return {"outcome": "already_created", "ticket_id": intent["ticket_id"]}, False
            return {"outcome": "prepared"}, False
        connection.execute(
            "INSERT INTO ticket_write_intents (customer_id, request_key, payload_json, request_hash, state, created_at) "
            "VALUES (?, ?, ?, ?, 'prepared', ?)",
            (customer_id, request_key, payload_json, request_hash, clock.format_timestamp(now)))
        return {"outcome": "prepared"}, True

    return _run_write(work)


def _insert_ticket_attempt(customer_id: str, request_key: str, payload: dict[str, Any], payload_json: str,
                           request_hash: str, limit: Optional[tuple[int, int]]) -> dict[str, Any]:
    """Transaction 2: ONE ``BEGIN IMMEDIATE`` holding the idempotency re-check, order and item ownership, duplicate
    detection, the rolling rate-limit count and the INSERT (with its ``created`` event and the intent's move to
    ``committed``). The check order is a requirement (design 4.1)."""
    issue_type = payload["issue_type"]
    order_id = payload["order_id"]
    item_id = payload["item_id"]
    parent_id = payload["parent_ticket_id"]
    description = payload["description"]

    def work(connection: sqlite3.Connection, now: datetime) -> tuple[dict[str, Any], bool]:
        stamp = clock.format_timestamp(now)
        existing = _ticket_by_key(connection, customer_id, request_key)
        if existing is not None:  # 1. a retry always gets the original answer, even at the limit
            if existing["request_hash"] == request_hash:
                return {"outcome": "already_created", "ticket_id": existing["ticket_id"]}, False
            return {"outcome": "idempotency_conflict"}, False
        linked: Optional[str] = None  # 2. ownership; an unowned order silently becomes NULL (existence never disclosed)
        if order_id is not None:
            if connection.execute("SELECT 1 FROM orders WHERE customer_id = ? AND order_id = ?",
                                  (customer_id, order_id)).fetchone() is not None:
                linked = order_id
        if item_id is not None:
            owned_item = linked is not None and connection.execute(
                "SELECT 1 FROM order_items WHERE item_id = ? AND order_id = ?", (item_id, linked)).fetchone()
            if not owned_item:
                _mark_intent(connection, customer_id, request_key, "item_not_found", stamp)
                return {"outcome": "item_not_found"}, True
        if parent_id is not None and connection.execute(
                "SELECT 1 FROM support_tickets WHERE ticket_id = ? AND customer_id = ?",
                (parent_id, customer_id)).fetchone() is None:
            _mark_intent(connection, customer_id, request_key, "parent_not_found", stamp)
            return {"outcome": "parent_not_found"}, True
        ticket_id = _new_id("TKT")
        issue_key = _issue_key(issue_type, linked, item_id, ticket_id)
        if not issue_key.startswith("n:"):  # 3. duplicate detection: a new key for an issue that already has an active ticket
            active = connection.execute(
                "SELECT ticket_id, status FROM support_tickets WHERE customer_id = ? AND issue_type = ? AND issue_key = ? "
                f"AND status NOT IN {_TERMINAL_SQL}", (customer_id, issue_type, issue_key)).fetchone()
            if active is not None:
                _mark_intent(connection, customer_id, request_key, "duplicate_active", stamp)
                return {"outcome": "duplicate_active", "ticket_id": active["ticket_id"], "status": active["status"]}, True
        if limit is not None:  # 4. the rolling account-level count, in the same transaction as the INSERT
            state = _rate_state(connection, now, customer_id, limit)
            if not state["allowed"]:
                _mark_intent(connection, customer_id, request_key, "rate_limited", stamp)
                return {"outcome": "rate_limited", "retry_at": state["retry_at"]}, True
        connection.execute(  # 5. the INSERT: conversation_summary is NULL for every new ticket (OD-15)
            "INSERT INTO support_tickets (ticket_id, customer_id, order_id, item_id, issue_type, issue_key, description, "
            "conversation_summary, status, priority, parent_ticket_id, request_key, request_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'Open', 'Normal', ?, ?, ?, ?, ?)",
            (ticket_id, customer_id, linked, item_id, issue_type, issue_key, description, parent_id, request_key,
             request_hash, stamp, stamp))
        _insert_event(connection, ticket_id=ticket_id, customer_id=customer_id, kind="created", actor_type="customer",
                      actor_ref=f"customer:{customer_id}", request_key=request_key, request_hash=request_hash,
                      created_at=stamp)
        updated = connection.execute(
            "UPDATE ticket_write_intents SET state = 'committed', ticket_id = ?, last_outcome = NULL, resolved_at = ? "
            "WHERE customer_id = ? AND request_key = ? AND state = 'prepared'", (ticket_id, stamp, customer_id, request_key))
        if updated.rowcount != 1:
            raise sqlite3.DatabaseError("the prepared intent for this request is missing")
        return {"outcome": "created", "ticket_id": ticket_id}, True

    try:
        return _run_write_tracked(work)
    except DatabaseConstraintError:
        # another writer bypassed steps 1 to 3 (the unique indexes are the backstop): repeat once to report the typed outcome
        return _run_write_tracked(work)


def _mark_intent(connection: sqlite3.Connection, customer_id: str, request_key: str, outcome: str, stamp: str) -> None:
    connection.execute(
        "UPDATE ticket_write_intents SET last_outcome = ?, resolved_at = ? WHERE customer_id = ? AND request_key = ? "
        "AND state = 'prepared'", (outcome, stamp, customer_id, request_key))


def close_ticket_request(authenticated_customer_id: str, request_key: str) -> dict[str, Any]:
    """Record that the customer was told a creation request definitely did NOT complete (``last_outcome = 'unavailable'``), so
    reconciliation never replays it silently later. Only a still-unresolved ``prepared`` intent of this customer is touched; a
    committed request is never changed (the answer stays ``already_created``). The same confirmation may still be retried with
    its key: a successful retry clears the marker. Returns ``{"closed": bool}``."""
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    _validate_request_key(request_key)

    def work(connection: sqlite3.Connection, now: datetime) -> tuple[dict[str, Any], bool]:
        cursor = connection.execute(
            "UPDATE ticket_write_intents SET last_outcome = 'unavailable', resolved_at = ? "
            "WHERE customer_id = ? AND request_key = ? AND state = 'prepared' AND last_outcome IS NULL",
            (clock.format_timestamp(now), authenticated_customer_id, request_key))
        return {"closed": cursor.rowcount == 1}, cursor.rowcount == 1

    return _run_write(work)


def _create_from_payload(customer_id: str, request_key: str, payload: dict[str, Any],
                         limit: Optional[tuple[int, int]]) -> dict[str, Any]:
    payload_json = _canonical_payload(payload["issue_type"], payload["order_id"], payload["item_id"],
                                      payload["parent_ticket_id"], payload["description"])
    request_hash = _payload_hash(payload_json)
    prepared = _prepare_intent(customer_id, request_key, payload_json, request_hash)
    if prepared["outcome"] != "prepared":
        return prepared
    return _insert_ticket_attempt(customer_id, request_key, payload, payload_json, request_hash, limit)


def create_ticket_from_description(
    authenticated_customer_id: str,
    issue_type: str,
    description: str,
    request_key: str,
    order_id: Optional[str] = None,
    item_id: Optional[str] = None,
    parent_ticket_id: Optional[str] = None,
    rate_limit: Optional[tuple[int, int]] = None,
) -> dict[str, Any]:
    """Create a ticket from the customer's own description (the fresh-description contract, TD-23 and OD-14).

    ``conversation_summary`` is NULL for every ticket created here (OD-15); the function never receives, generates or stores a
    summary. ``description`` is validated and stored exactly as given (verbatim, at most 2,000 code points; never modified).
    ``request_key`` is stable per confirmation; the request hash covers ``issue_type``, ``order_id``, ``item_id``,
    ``parent_ticket_id`` and the exact description (no summary or chat content). The request key and the immutable payload are
    persisted BEFORE the insert (``ticket_write_intents``), so an uncertain outcome is recoverable (see
    ``retry_ticket_request``). Business outcomes are returned as values:

    * ``created`` + ``ticket_id``
    * ``already_created`` + ``ticket_id``: the same key and payload (idempotency; a retry never writes again)
    * ``idempotency_conflict``: the same key with a different payload
    * ``duplicate_active`` + ``ticket_id`` + ``status``: a NEW key for an issue that already has an active ticket (duplicate
      detection; nothing written; no offer to add information)
    * ``rate_limited`` + ``retry_at``
    * ``item_not_found``, ``parent_not_found``

    An error while the COMMIT of the insert runs raises ``DatabaseOutcomeUnknownError``. Idempotency, duplicate detection and
    adding information are three different operations (OD-27) and never turn into one another.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    validate_issue_type(issue_type)
    validate_text(description, "description", TICKET_TEXT_MAX)
    _validate_request_key(request_key)
    for name, value in (("order_id", order_id), ("item_id", item_id), ("parent_ticket_id", parent_ticket_id)):
        if value is not None:
            validate_identifier(value, name)
    if item_id is not None and order_id is None:
        raise ValueError("an item needs its order")
    limit = _validate_rate_limit(rate_limit)
    payload = {"issue_type": issue_type, "order_id": order_id, "item_id": item_id,
               "parent_ticket_id": parent_ticket_id, "description": description}
    return _create_from_payload(authenticated_customer_id, request_key, payload, limit)


def get_ticket_request_state(authenticated_customer_id: str, request_key: str) -> dict[str, Any]:
    """What the database knows about a creation request of this customer: ``unknown``, ``prepared`` (persisted, no ticket
    yet; ``last_outcome`` may name a definite refusal) or ``committed`` (``ticket_id``). Customer-scoped in SQL."""
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    _validate_request_key(request_key)

    def work(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        row = connection.execute(
            "SELECT state, last_outcome, ticket_id FROM ticket_write_intents WHERE customer_id = ? AND request_key = ?",
            (authenticated_customer_id, request_key)).fetchone()
        if row is None:
            return {"state": "unknown"}
        return {"state": row["state"], "last_outcome": row["last_outcome"], "ticket_id": row["ticket_id"]}

    return _run_read(work)


def list_unresolved_ticket_requests(authenticated_customer_id: str) -> list[dict[str, Any]]:
    """The customer's persisted creation requests that have neither produced a ticket nor received a definite refusal.

    Survives session expiry and process restart (it reads the database, not memory). Each entry carries the request key and
    the immutable payload so ``retry_ticket_request`` can replay it unchanged. Entries whose payload was erased are omitted."""
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")

    def work(connection: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT request_key, request_hash, payload_json, created_at FROM ticket_write_intents "
            "WHERE customer_id = ? AND state = 'prepared' AND last_outcome IS NULL AND payload_json IS NOT NULL "
            "ORDER BY created_at ASC, request_key ASC", (authenticated_customer_id,)).fetchall()
        return [{"request_key": r["request_key"], "request_hash": r["request_hash"],
                 "payload": json.loads(r["payload_json"]), "created_at": r["created_at"]} for r in rows]

    return _run_read(work)


def retry_ticket_request(authenticated_customer_id: str, request_key: str,
                         rate_limit: Optional[tuple[int, int]] = None) -> dict[str, Any]:
    """Replay a persisted creation request with its stored key and immutable payload (recovery after an uncertain outcome).

    Returns the same outcome vocabulary as ``create_ticket_from_description`` (``already_created`` when the ticket exists, so
    a request never creates two tickets), ``unknown_request`` for a key this customer never persisted, or
    ``payload_erased`` when the audited erasure removed the payload. It never accepts a new payload: the stored one is final."""
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    _validate_request_key(request_key)
    limit = _validate_rate_limit(rate_limit)

    def work(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json, state, ticket_id FROM ticket_write_intents WHERE customer_id = ? AND request_key = ?",
            (authenticated_customer_id, request_key)).fetchone()
        return dict(row) if row is not None else {}

    stored = _run_read(work)
    if not stored:
        return {"outcome": "unknown_request"}
    if stored["state"] == "committed":
        return {"outcome": "already_created", "ticket_id": stored["ticket_id"]}
    if stored["payload_json"] is None:
        return {"outcome": "payload_erased"}
    return _create_from_payload(authenticated_customer_id, request_key, json.loads(stored["payload_json"]), limit)


# -- events -----------------------------------------------------------------

def get_ticket_events(authenticated_customer_id: str, ticket_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """The events of one of the customer's tickets in ``seq`` order. A foreign and a fabricated ticket ID give the same
    empty answer. Redacted comments have a NULL body."""
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    validate_identifier(ticket_id, "ticket_id")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("'offset' must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("'limit' must be a positive integer")

    def work(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        total = connection.execute(
            "SELECT COUNT(*) FROM ticket_events WHERE ticket_id = ? AND customer_id = ?",
            (ticket_id, authenticated_customer_id)).fetchone()[0]
        rows = connection.execute(
            "SELECT event_id, ticket_id, seq, kind, actor_type, body, intent, staff_review_required, from_status, "
            "to_status, transition_reason, trigger_event_id, created_at, redacted_at FROM ticket_events "
            "WHERE ticket_id = ? AND customer_id = ? ORDER BY seq ASC LIMIT ? OFFSET ?",
            (ticket_id, authenticated_customer_id, limit, offset)).fetchall()
        return {"events": _rows_to_dicts(rows), "total": total}

    return _run_read(work)


def add_ticket_event(
    authenticated_customer_id: str,
    ticket_id: str,
    request_key: str,
    body: str,
    intent: str = "information",
    *,
    comments_max: int = COMMENTS_MAX_DEFAULT,
    reopen_window_seconds: int = REOPEN_WINDOW_SECONDS_DEFAULT,
) -> dict[str, Any]:
    """Append information to one of the customer's tickets and apply ONLY the owner-approved status rules (OD-13).

    This is the repository half of "add information" (design 4.2); the customer conversation that offers it is Phase 3 and
    is not part of this change. There is no status argument: ``intent`` is the closed enum ``information`` or
    ``issue_persists``. One ``BEGIN IMMEDIATE`` transaction holds idempotency, ownership, the comment bound, the rule lookup,
    the comment event, the status update and the audit event. It never touches ``description`` and never counts toward the
    creation limit. Outcomes (values): ``added`` (``event_id``, ``status``, ``transition``, ``staff_review_required``),
    ``already_applied``, ``idempotency_conflict``, ``not_found`` (foreign and fabricated IDs alike), ``comment_limit_reached``,
    ``reopen_window_expired``, ``reopen_unavailable``, ``active_ticket_exists``, ``intent_not_applicable``, ``ticket_closed``
    and ``status_rule_unspecified``.

    ``status_rule_unspecified`` is returned (nothing written) for tickets in ``New``, ``Pending`` or ``Reopened``: the owner
    approved no rule for them (open decision B-5), and none is invented here.
    """
    validate_identifier(authenticated_customer_id, "authenticated_customer_id")
    validate_identifier(ticket_id, "ticket_id")
    _validate_request_key(request_key)
    validate_text(body, "body", COMMENT_TEXT_MAX)
    if intent not in ("information", "issue_persists"):
        raise ValueError("'intent' must be 'information' or 'issue_persists'")
    for name, value in (("comments_max", comments_max), ("reopen_window_seconds", reopen_window_seconds)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"'{name}' must be a positive integer")
    payload_json = json.dumps({"body": body, "intent": intent, "ticket_id": ticket_id}, sort_keys=True,
                              ensure_ascii=True, separators=(",", ":"))
    request_hash = _payload_hash(payload_json)

    def work(connection: sqlite3.Connection, now: datetime) -> tuple[dict[str, Any], bool]:
        stamp = clock.format_timestamp(now)
        prior = connection.execute(
            "SELECT event_id, request_hash FROM ticket_events WHERE customer_id = ? AND request_key = ?",
            (authenticated_customer_id, request_key)).fetchone()
        if prior is not None:  # 1. idempotency
            if prior["request_hash"] != request_hash:
                return {"outcome": "idempotency_conflict"}, False
            ticket = connection.execute("SELECT status FROM support_tickets WHERE ticket_id = ? AND customer_id = ?",
                                        (ticket_id, authenticated_customer_id)).fetchone()
            return {"outcome": "already_applied", "event_id": prior["event_id"],
                    "status": ticket["status"] if ticket else None}, False
        ticket = connection.execute(  # 2. ownership in SQL
            "SELECT ticket_id, status, issue_type, issue_key FROM support_tickets WHERE ticket_id = ? AND customer_id = ?",
            (ticket_id, authenticated_customer_id)).fetchone()
        if ticket is None:
            return {"outcome": "not_found"}, False
        comments = connection.execute(  # 3. the comment bound (redacted comments count)
            "SELECT COUNT(*) FROM ticket_events WHERE ticket_id = ? AND kind = 'comment'", (ticket_id,)).fetchone()[0]
        if comments >= comments_max:
            return {"outcome": "comment_limit_reached"}, False
        status = ticket["status"]
        transition: Optional[tuple[str, str]] = None
        review = 0
        unavailable = False
        if status in ("Closed", "Cancelled"):  # 4. the predefined rule lookup (business rules 7.2)
            return {"outcome": "ticket_closed"}, False
        if status in ("New", "Pending", "Reopened"):
            return {"outcome": "status_rule_unspecified"}, False  # B-5: no owner-approved rule, none invented
        if status == "Waiting for customer":
            if intent == "information":
                transition = ("In Progress", "customer_supplied_requested_information")
            else:
                return {"outcome": "intent_not_applicable"}, False
        elif status == "Resolved":
            if intent == "issue_persists":
                resolved = connection.execute(
                    "SELECT created_at FROM ticket_events WHERE ticket_id = ? AND kind = 'status_change' "
                    "AND to_status = 'Resolved' ORDER BY seq DESC LIMIT 1", (ticket_id,)).fetchone()
                if resolved is None:  # fails closed: the window cannot be proven without a trusted event
                    review = 1
                    unavailable = True
                else:
                    resolved_at = clock.parse_timestamp(resolved["created_at"])
                    if now >= resolved_at + timedelta(seconds=reopen_window_seconds):
                        return {"outcome": "reopen_window_expired"}, False
                    if not ticket["issue_key"].startswith("n:") and connection.execute(
                            "SELECT 1 FROM support_tickets WHERE customer_id = ? AND issue_type = ? AND issue_key = ? "
                            f"AND ticket_id <> ? AND status NOT IN {_TERMINAL_SQL}",
                            (authenticated_customer_id, ticket["issue_type"], ticket["issue_key"], ticket_id)).fetchone():
                        return {"outcome": "active_ticket_exists"}, False
                    transition = ("Reopened", "customer_reported_issue_persists")
        elif status == "On hold":
            if intent == "information":
                review = 1
            else:
                return {"outcome": "intent_not_applicable"}, False
        else:  # Open, In Progress
            if intent == "issue_persists":
                return {"outcome": "intent_not_applicable"}, False
        comment_id = _insert_event(  # 5. the comment, then the transition and its audit event, atomically
            connection, ticket_id=ticket_id, customer_id=authenticated_customer_id, kind="comment",
            actor_type="customer", actor_ref=f"customer:{authenticated_customer_id}", request_key=request_key,
            request_hash=request_hash, created_at=stamp, body=body, intent=intent, staff_review_required=review)
        new_status = status
        if transition is not None:
            new_status, reason = transition
            connection.execute("UPDATE support_tickets SET status = ?, updated_at = ? WHERE ticket_id = ? AND customer_id = ?",
                               (new_status, stamp, ticket_id, authenticated_customer_id))
            _insert_event(connection, ticket_id=ticket_id, customer_id=authenticated_customer_id, kind="status_change",
                          actor_type="system", actor_ref="system", request_key=f"{request_key}#status",
                          request_hash=request_hash, created_at=stamp, from_status=status, to_status=new_status,
                          transition_reason=reason, trigger_event_id=comment_id)
        else:
            connection.execute("UPDATE support_tickets SET updated_at = ? WHERE ticket_id = ? AND customer_id = ?",
                               (stamp, ticket_id, authenticated_customer_id))
        result = {"outcome": "reopen_unavailable" if unavailable else "added", "event_id": comment_id,
                  "status": new_status, "staff_review_required": bool(review),
                  "transition": {"from": status, "to": new_status} if transition is not None else None}
        return result, True

    return _run_write(work)


# -- audited erasure (operator only) ---------------------------------------

ERASURE_REASONS = ("privacy_request", "retention_expiry", "legal_order")
ERASED_MARKER = "[erased]"


def erase_ticket_content(
    operator_ref: str,
    authorization_ref: str,
    reason_code: str,
    customer_id: str,
    request_key: str,
    ticket_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Operator-run, audited redaction of ticket content (OD-09, design 4.5). Never reachable from a customer tool, a
    model-visible tool, an HTTP route or the voice agent token (a source test enforces that no application module calls it).

    In ONE transaction: ticket descriptions become ``[erased]``, summaries NULL, ``redacted_at`` is set; the selected tickets'
    comment bodies become NULL with ``redacted_at``; write-intent payloads of the scope are removed; and one
    ``ticket_erasure_log`` row records counts only (no text, no customer data). Rows are redacted, never deleted. Idempotent by
    ``request_key``. No retention period exists and nothing erases automatically.
    """
    for name, value in (("operator_ref", operator_ref), ("authorization_ref", authorization_ref)):
        validate_text(value, name, 200)
    if reason_code not in ERASURE_REASONS:
        raise ValueError("'reason_code' is not an approved erasure reason")
    validate_identifier(customer_id, "customer_id")
    _validate_request_key(request_key)
    if ticket_ids is not None:
        if not isinstance(ticket_ids, list) or not ticket_ids:
            raise ValueError("'ticket_ids' must be a non-empty list or None")
        for value in ticket_ids:
            validate_identifier(value, "ticket_id")

    def work(connection: sqlite3.Connection, now: datetime) -> tuple[dict[str, Any], bool]:
        stamp = clock.format_timestamp(now)
        prior = connection.execute(
            "SELECT erasure_id, tickets_redacted, events_redacted FROM ticket_erasure_log WHERE request_key = ?",
            (request_key,)).fetchone()
        if prior is not None:
            return {"outcome": "already_applied", "erasure_id": prior["erasure_id"],
                    "tickets_redacted": prior["tickets_redacted"], "events_redacted": prior["events_redacted"]}, False
        if ticket_ids is None:
            scope = [r[0] for r in connection.execute(
                "SELECT ticket_id FROM support_tickets WHERE customer_id = ?", (customer_id,))]
        else:
            scope = [r[0] for r in connection.execute(
                "SELECT ticket_id FROM support_tickets WHERE customer_id = ? AND ticket_id IN (%s)" %
                ",".join("?" * len(ticket_ids)), (customer_id, *ticket_ids))]
        tickets = events = 0
        for tid in scope:
            tickets += connection.execute(
                "UPDATE support_tickets SET description = ?, conversation_summary = NULL, redacted_at = ? "
                "WHERE ticket_id = ? AND customer_id = ? AND redacted_at IS NULL",
                (ERASED_MARKER, stamp, tid, customer_id)).rowcount
            events += connection.execute(
                "UPDATE ticket_events SET body = NULL, redacted_at = ? WHERE ticket_id = ? AND customer_id = ? "
                "AND redacted_at IS NULL", (stamp, tid, customer_id)).rowcount
            connection.execute(
                "UPDATE ticket_write_intents SET payload_json = NULL, redacted_at = ? WHERE customer_id = ? "
                "AND ticket_id = ? AND redacted_at IS NULL", (stamp, customer_id, tid))
        if ticket_ids is None:  # a whole-customer erasure also removes unresolved payloads that never became a ticket
            connection.execute(
                "UPDATE ticket_write_intents SET payload_json = NULL, redacted_at = ? WHERE customer_id = ? "
                "AND redacted_at IS NULL", (stamp, customer_id))
        erasure_id = _new_id("ERS")
        connection.execute(
            "INSERT INTO ticket_erasure_log (erasure_id, performed_at, operator_ref, authorization_ref, reason_code, "
            "scope_customer_id, tickets_redacted, events_redacted, request_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (erasure_id, stamp, operator_ref, authorization_ref, reason_code, customer_id, tickets, events, request_key))
        return {"outcome": "erased", "erasure_id": erasure_id, "tickets_redacted": tickets,
                "events_redacted": events}, True

    return _run_write(work)
