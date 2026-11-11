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


def create_support_ticket(
    authenticated_customer_id: str,
    issue_type: str,
    description: str,
    conversation_summary: str,
    order_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a support ticket, optionally linked to an owned order.

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

    def work(connection: sqlite3.Connection, now: datetime) -> tuple[dict[str, Any], bool]:
        linked: Optional[str] = None
        if order_id is not None:
            owned = connection.execute(
                "SELECT 1 FROM orders WHERE customer_id = ? AND order_id = ?",
                (authenticated_customer_id, order_id),
            ).fetchone()
            if owned is not None:
                linked = order_id
        ticket_id = _new_id("TKT")
        connection.execute(
            "INSERT INTO support_tickets "
            "(ticket_id, customer_id, order_id, issue_type, description, "
            "conversation_summary, status, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'Open', 'Normal', ?)",
            (
                ticket_id, authenticated_customer_id, linked, issue_type,
                description, conversation_summary, clock.format_timestamp(now),
            ),
        )
        return {"outcome": "created", "ticket_id": ticket_id}, True

    return _run_write(work)
