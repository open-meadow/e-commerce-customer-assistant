"""Customer-scoped database repository (Checkpoints 5 and 7).

This module is the single place in the application permitted to run
SQL queries against customer-owned data. Every function here accepts
``authenticated_customer_id`` as a required, explicit parameter and
uses it directly inside the SQL ``WHERE`` clause (or an ownership-
scoped join) of every query — never as a post-fetch filter applied to
an already-retrieved row.

Where the authenticated customer ID comes from
------------------------------------------------
``authenticated_customer_id`` must be supplied by trusted application
context (in this project's current MVP, the Streamlit session's "log
in as" selector — see docs/01_database_design.md Section E and
docs/00_project_discovery.md). It must never be read from a user
message, an LLM tool-call argument, or any other untrusted input by
this module or any caller of it. This module has no way to enforce
that at the Python level — it can only guarantee that *whatever*
customer ID it is given is the one, and the only one, used to scope
every query.

Scope
-----
Checkpoint 5: five read operations (``list_customer_orders``,
``get_order``, ``get_order_items``, ``get_customer_returns``,
``get_customer_tickets``).

Checkpoint 7: two write operations (``create_return_request``,
``create_support_ticket``), implemented exactly as specified in
docs/01_database_design.md Sections F, G and H and
docs/02_database_implementation_plan.md Sections 4 and 5. Their error
handling differs from the read functions' — see "Write error handling"
below.

Connection lifecycle
----------------------
Every function here opens its own connection via
``app.db.connection.get_connection()`` and closes it before returning,
even if a query or the transaction fails. No connection is held open
across function calls, and no connection is ever exposed to a caller of
this module; callers only ever receive plain Python data (dicts, lists,
or ``None``), never a cursor or connection object. This keeps all
database access centralized in this one module.

Read functions (Checkpoint 5): each runs exactly one query on its own
connection and closes the connection in a ``finally`` block. There is
no explicit transaction.

Write functions (Checkpoint 7): each runs several statements (the
ownership and business checks, then the INSERT) inside a single
explicit ``BEGIN IMMEDIATE`` transaction on its own connection. The
transaction is committed only after every check passes and the INSERT
succeeds, and is rolled back on a business rejection or any failure.
The connection is closed in a ``finally`` block afterwards. See "Write
error handling" below.

Error handling
----------------
Following this project's established convention (see
app/db/schema.py and app/db/seed_data.py): a query failure
(``sqlite3.Error``) is not caught or wrapped here — it propagates to
the caller unmodified, so a real database problem is visible as
exactly what it is, not hidden behind a generic message. The only
validation this module performs itself is on its own input parameters
(e.g. rejecting an empty ``authenticated_customer_id``), which is
ordinary defensive input checking, not a business rule.

Write error handling (Checkpoint 7)
-----------------------------------
The two write functions follow docs/01 Section H instead: a
``ConnectionError_`` from ``get_connection()`` or a ``sqlite3.Error``
during the transaction (after a rollback, if one is open) produces a
generic ``failed`` result carrying no database text; any other
exception is rolled back and re-raised unchanged; a rollback or close
failure never masks the original outcome.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.db.connection import ConnectionError_, get_connection


# ---------------------------------------------------------------------------
# Column lists — kept as named constants so every SELECT in this module
# uses explicit columns (never `SELECT *`), and so a column list is
# defined once per table rather than repeated at each call site.
# ---------------------------------------------------------------------------

_ORDER_COLUMNS = (
    "order_id, customer_id, status, payment_status, refund_status, "
    "refund_amount, order_date, delivered_at, total_amount"
)

_ORDER_ITEM_COLUMNS = "item_id, order_id, product_id, quantity, price"

_RETURN_REQUEST_COLUMNS = (
    "return_id, customer_id, order_id, item_id, reason, status, requested_at"
)

_SUPPORT_TICKET_COLUMNS = (
    "ticket_id, customer_id, order_id, issue_type, description, "
    "conversation_summary, status, priority, created_at"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_non_empty(value: str, parameter_name: str) -> str:
    """Validate that *value* is a non-empty string; return it unchanged.

    This is ordinary input validation (a required identifier must
    actually identify something), not a business rule. An empty or
    non-string identifier is a programming error in the caller, not a
    legitimate "not found" case, so it is rejected before any query
    runs rather than silently returning an empty result.

    Raises:
        ValueError: if *value* is not a non-empty string.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{parameter_name!r} must be a non-empty string; got {value!r}"
        )
    return value


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert a list of ``sqlite3.Row`` objects into plain dicts."""
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_customer_orders(authenticated_customer_id: str) -> list[dict[str, Any]]:
    """Return every order belonging to the authenticated customer.

    Ownership filter: ``WHERE customer_id = ?`` is the entire query —
    there is no order this function could return that the authenticated
    customer does not own, by construction of the query itself.

    Args:
        authenticated_customer_id: the customer ID from trusted
            application context. Must be a non-empty string.

    Returns:
        A list of order dicts (possibly empty if the customer has no
        orders), each with keys: ``order_id``, ``customer_id``,
        ``status``, ``payment_status``, ``refund_status``,
        ``refund_amount``, ``order_date``, ``delivered_at``,
        ``total_amount``. Ordered by ``order_id`` ascending for
        predictable, deterministic output (this ordering is an
        implementation choice, not a requirement from the approved
        design documents).

    Raises:
        ValueError: if ``authenticated_customer_id`` is empty.
        sqlite3.Error: if the query itself fails. Propagated
            unmodified; see module docstring.
    """
    _require_non_empty(authenticated_customer_id, "authenticated_customer_id")

    connection = get_connection()
    try:
        rows = connection.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders "
            "WHERE customer_id = ? "
            "ORDER BY order_id ASC",
            (authenticated_customer_id,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        connection.close()


def get_order(
    authenticated_customer_id: str, order_id: str
) -> Optional[dict[str, Any]]:
    """Return one order if it exists AND belongs to the authenticated customer.

    Ownership filter: ``WHERE customer_id = ? AND order_id = ?`` —
    both conditions must hold in the same row for anything to be
    returned.

    Returns ``None`` for both a nonexistent order ID and an order that
    exists but belongs to a different customer. These two cases are
    deliberately indistinguishable from this function's return value
    alone: there is no way for a caller to tell, from ``None``, which
    situation occurred. This matches docs/01_database_design.md
    Section E's "same not-found result" requirement — this function
    does not disclose whether an order belongs to another customer.

    Args:
        authenticated_customer_id: the customer ID from trusted
            application context. Must be a non-empty string.
        order_id: the requested order ID. Must be a non-empty string.

    Returns:
        A dict with the same keys as ``list_customer_orders()``'s
        entries, or ``None`` if no matching, owned order exists.

    Raises:
        ValueError: if either argument is empty.
        sqlite3.Error: if the query itself fails. Propagated
            unmodified; see module docstring.
    """
    _require_non_empty(authenticated_customer_id, "authenticated_customer_id")
    _require_non_empty(order_id, "order_id")

    connection = get_connection()
    try:
        row = connection.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders "
            "WHERE customer_id = ? AND order_id = ?",
            (authenticated_customer_id, order_id),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def get_order_items(
    authenticated_customer_id: str, order_id: str
) -> list[dict[str, Any]]:
    """Return the line items of an order, scoped to the authenticated customer.

    Ownership filter: a single query joins ``order_items`` to
    ``orders`` and filters on ``orders.customer_id`` directly — this
    function does NOT first call ``get_order()`` (or any other lookup)
    to check ownership and then separately query ``order_items``
    trusting that earlier result. There is exactly one query, and
    ownership is enforced inside it, so there is no window in which a
    caller could race or bypass the ownership check between two
    separate queries.

    Args:
        authenticated_customer_id: the customer ID from trusted
            application context. Must be a non-empty string.
        order_id: the order whose items are requested. Must be a
            non-empty string.

    Returns:
        A list of item dicts (empty if the order does not exist, or
        exists but is not owned by this customer, or is owned but has
        no items), each with keys: ``item_id``, ``order_id``,
        ``product_id``, ``quantity``, ``price``. Ordered by
        ``item_id`` ascending for predictable output.

    Raises:
        ValueError: if either argument is empty.
        sqlite3.Error: if the query itself fails. Propagated
            unmodified; see module docstring.
    """
    _require_non_empty(authenticated_customer_id, "authenticated_customer_id")
    _require_non_empty(order_id, "order_id")

    connection = get_connection()
    try:
        qualified_columns = ", ".join(
            f"order_items.{column.strip()}"
            for column in _ORDER_ITEM_COLUMNS.split(",")
        )
        rows = connection.execute(
            f"SELECT {qualified_columns} FROM order_items "
            "JOIN orders ON orders.order_id = order_items.order_id "
            "WHERE orders.customer_id = ? AND order_items.order_id = ? "
            "ORDER BY order_items.item_id ASC",
            (authenticated_customer_id, order_id),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        connection.close()


def get_customer_returns(authenticated_customer_id: str) -> list[dict[str, Any]]:
    """Return every return request belonging to the authenticated customer.

    Ownership filter: ``WHERE customer_id = ?`` on ``return_requests``
    directly — ``return_requests`` carries its own direct foreign key
    to ``customers`` (see docs/01_database_design.md Section B), so
    this does not need a join through ``orders`` to establish
    ownership.

    Args:
        authenticated_customer_id: the customer ID from trusted
            application context. Must be a non-empty string.

    Returns:
        A list of return-request dicts (possibly empty), each with
        keys: ``return_id``, ``customer_id``, ``order_id``,
        ``item_id``, ``reason``, ``status``, ``requested_at``. Ordered
        by ``requested_at`` descending (most recent first), then by
        ``return_id`` ascending as a deterministic tiebreaker for
        requests sharing the same timestamp.

    Raises:
        ValueError: if ``authenticated_customer_id`` is empty.
        sqlite3.Error: if the query itself fails. Propagated
            unmodified; see module docstring.
    """
    _require_non_empty(authenticated_customer_id, "authenticated_customer_id")

    connection = get_connection()
    try:
        rows = connection.execute(
            f"SELECT {_RETURN_REQUEST_COLUMNS} FROM return_requests "
            "WHERE customer_id = ? "
            "ORDER BY requested_at DESC, return_id ASC",
            (authenticated_customer_id,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        connection.close()


def get_customer_tickets(authenticated_customer_id: str) -> list[dict[str, Any]]:
    """Return every support ticket belonging to the authenticated customer.

    Ownership filter: ``WHERE customer_id = ?`` on ``support_tickets``
    directly.

    Args:
        authenticated_customer_id: the customer ID from trusted
            application context. Must be a non-empty string.

    Returns:
        A list of ticket dicts (possibly empty), each with keys:
        ``ticket_id``, ``customer_id``, ``order_id``, ``issue_type``,
        ``description``, ``conversation_summary``, ``status``,
        ``priority``, ``created_at``. Ordered by ``created_at``
        descending (most recent first), then by ``ticket_id``
        ascending as a deterministic tiebreaker.

    Raises:
        ValueError: if ``authenticated_customer_id`` is empty.
        sqlite3.Error: if the query itself fails. Propagated
            unmodified; see module docstring.
    """
    _require_non_empty(authenticated_customer_id, "authenticated_customer_id")

    connection = get_connection()
    try:
        rows = connection.execute(
            f"SELECT {_SUPPORT_TICKET_COLUMNS} FROM support_tickets "
            "WHERE customer_id = ? "
            "ORDER BY created_at DESC, ticket_id ASC",
            (authenticated_customer_id,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Write operations (Checkpoint 7)
#
# Contracts: docs/01_database_design.md Sections F, G, H and I;
# docs/02_database_implementation_plan.md Sections 4 and 5.
# ---------------------------------------------------------------------------

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_STORED_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)

# reason_category -> maximum elapsed time since delivery (inclusive), in
# seconds. 48 hours: FAQ.md Q3.4. 7 x 24 hours: Policies.md Section 2.
_RETURN_WINDOW_SECONDS = {
    "damaged": 48 * 3600,
    "wrong_product": 48 * 3600,
    "standard": 7 * 24 * 3600,
}

_RETURN_FAILED = {"outcome": "failed", "return_id": None}
_TICKET_FAILED = {
    "outcome": "failed",
    "ticket_id": None,
    "order_id": None,
    "order_linked": False,
}


def _require_string(value: Any, parameter_name: str) -> None:
    """Wrong type -> ``TypeError``; blank/whitespace-only -> ``ValueError``."""
    if not isinstance(value, str):
        raise TypeError(
            f"{parameter_name!r} must be a string; got {type(value).__name__}"
        )
    if not value.strip():
        raise ValueError(f"{parameter_name!r} must not be blank")


def _normalize_reference_time(reference_time: Any) -> datetime:
    """Return the single UTC instant (whole seconds, truncated) for one write.

    ``None`` reads the real clock exactly once. A ``datetime`` must be
    timezone-aware; a string must be ISO-8601 with an explicit UTC
    offset. Naive / unparseable / offset-less input raises
    ``ValueError``; any other type raises ``TypeError``.
    """
    if reference_time is None:
        instant = datetime.now(timezone.utc)
    elif isinstance(reference_time, datetime):
        instant = reference_time
    elif isinstance(reference_time, str):
        try:
            instant = datetime.fromisoformat(reference_time)
        except ValueError:
            raise ValueError(
                "'reference_time' is not a valid ISO-8601 timestamp"
            ) from None
    else:
        raise TypeError(
            "'reference_time' must be None, a datetime, or an ISO-8601 "
            f"string; got {type(reference_time).__name__}"
        )

    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("'reference_time' must be timezone-aware")
    try:
        return instant.astimezone(timezone.utc).replace(microsecond=0)
    except OverflowError:
        raise ValueError("'reference_time' is out of range") from None


def _parse_stored_timestamp(value: Any) -> Optional[datetime]:
    """Parse a stored ``YYYY-MM-DDTHH:MM:SSZ`` value; ``None`` if absent/invalid.

    Only the exact fixed-width representation is accepted. ``strptime``
    alone is too lenient (it accepts non-zero-padded fields such as
    ``2026-1-4T0:0:0Z`` and lowercase ``t``/``z``), so the shape is
    checked first; ``strptime`` then validates the calendar values.
    """
    if not isinstance(value, str) or _STORED_TIMESTAMP_RE.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    """Roll back an open transaction; a rollback failure is discarded."""
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _close_quietly(connection: sqlite3.Connection) -> None:
    """Close the connection; a close failure is discarded."""
    try:
        connection.close()
    except Exception:
        pass


def _run_write_transaction(
    work: Callable[[sqlite3.Connection], tuple[dict[str, Any], bool]],
    failed_result: dict[str, Any],
) -> dict[str, Any]:
    """Run *work* inside ``BEGIN IMMEDIATE`` with the Section H error rules.

    *work* returns ``(result, commit)``: when ``commit`` is true the
    transaction is committed and *result* is returned only after the
    COMMIT succeeded; otherwise it is rolled back and *result* returned.

    * ``ConnectionError_`` on acquisition -> generic ``failed``.
    * ``sqlite3.Error`` (including a failed COMMIT) -> rollback, ``failed``.
    * Any other exception -> rollback, re-raised unchanged.
    * A rollback or close failure never replaces the original outcome.
    """
    try:
        connection = get_connection()
    except ConnectionError_:
        return dict(failed_result)

    try:
        connection.execute("BEGIN IMMEDIATE")
        result, commit = work(connection)
        if commit:
            connection.commit()
        else:
            connection.rollback()
        return result
    except sqlite3.Error:
        _rollback_quietly(connection)
        return dict(failed_result)
    except BaseException:
        _rollback_quietly(connection)
        raise
    finally:
        _close_quietly(connection)


def _has_active_return(connection: sqlite3.Connection, item_id: str) -> bool:
    """Same predicate as the ``uq_active_return_per_item`` partial index."""
    row = connection.execute(
        "SELECT 1 FROM return_requests "
        "WHERE item_id = ? AND status IN ('Requested', 'Approved')",
        (item_id,),
    ).fetchone()
    return row is not None


def _is_active_return_violation(exc: sqlite3.IntegrityError) -> bool:
    """True if *exc* is the unique violation on ``return_requests.item_id``.

    SQLite does not put the index name in the message, so the violating
    column is matched instead. ``item_id`` is only unique through
    ``uq_active_return_per_item``; a ``return_id`` collision or any
    other constraint failure is not matched and becomes ``failed``.
    """
    return (
        getattr(exc, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_UNIQUE"
        and "return_requests.item_id" in str(exc)
    )


def create_return_request(
    authenticated_customer_id: str,
    order_id: str,
    item_id: str,
    reason: str,
    *,
    reason_category: str,
    reference_time: Any = None,
) -> dict[str, Any]:
    """Create a return request for an item of an order the customer owns.

    Contract: docs/01 Sections F, H, I; docs/02 Sections 4 and 5.

    Args:
        authenticated_customer_id: trusted application context only.
        order_id, item_id: the order and item being returned.
        reason: free text, stored as given, never interpreted.
        reason_category: exactly ``damaged``, ``wrong_product`` (48 hours)
            or ``standard`` (7 x 24 hours), measured from
            ``orders.delivered_at``; boundaries inclusive.
        reference_time: ``None`` (current UTC time, read once), a
            timezone-aware ``datetime``, or an ISO-8601 string with an
            explicit offset. Normalized to UTC, truncated to whole
            seconds, and used for both eligibility and ``requested_at``.

    Returns:
        ``{"outcome": ..., "return_id": ...}`` where outcome is
        ``created`` (only after COMMIT; the only case with a non-null
        ``return_id``), ``not_found``, ``item_mismatch``,
        ``outside_window``, ``duplicate`` or ``failed``.

    Raises:
        TypeError: an argument has the wrong type.
        ValueError: an argument has an invalid value (blank string,
            unknown ``reason_category``, naive or unparseable
            ``reference_time``).
        Any non-database exception raised during the write is rolled
        back and re-raised.
    """
    # Step 1: validate inputs (before any transaction or connection).
    _require_string(authenticated_customer_id, "authenticated_customer_id")
    _require_string(order_id, "order_id")
    _require_string(item_id, "item_id")
    _require_string(reason, "reason")
    if not isinstance(reason_category, str):
        raise TypeError(
            "'reason_category' must be a string; got "
            f"{type(reason_category).__name__}"
        )
    if reason_category not in _RETURN_WINDOW_SECONDS:
        raise ValueError(
            "'reason_category' must be one of "
            f"{sorted(_RETURN_WINDOW_SECONDS)}"
        )
    reference = _normalize_reference_time(reference_time)  # clock read once

    def work(connection: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
        # Step 2: ownership. Nonexistent and unowned look identical.
        order_row = connection.execute(
            "SELECT delivered_at FROM orders "
            "WHERE customer_id = ? AND order_id = ?",
            (authenticated_customer_id, order_id),
        ).fetchone()
        if order_row is None:
            return {"outcome": "not_found", "return_id": None}, False

        # Step 3: the item must belong to that order.
        item_row = connection.execute(
            "SELECT 1 FROM order_items WHERE order_id = ? AND item_id = ?",
            (order_id, item_id),
        ).fetchone()
        if item_row is None:
            return {"outcome": "item_mismatch", "return_id": None}, False

        # Step 4: an active return wins over an expired window.
        if _has_active_return(connection, item_id):
            return {"outcome": "duplicate", "return_id": None}, False

        # Step 5: eligibility. NULL, invalid or future delivered_at is
        # ineligible; elapsed time exactly at the limit is eligible.
        delivered = _parse_stored_timestamp(order_row["delivered_at"])
        if delivered is None:
            return {"outcome": "outside_window", "return_id": None}, False
        elapsed = (reference - delivered).total_seconds()
        if not 0 <= elapsed <= _RETURN_WINDOW_SECONDS[reason_category]:
            return {"outcome": "outside_window", "return_id": None}, False

        # Step 6: insert.
        return_id = f"RET-{uuid.uuid4().hex[:12].upper()}"
        try:
            connection.execute(
                "INSERT INTO return_requests "
                "(return_id, customer_id, order_id, item_id, reason, "
                "status, requested_at) "
                "VALUES (?, ?, ?, ?, ?, 'Requested', ?)",
                (
                    return_id,
                    authenticated_customer_id,
                    order_id,
                    item_id,
                    reason,
                    reference.strftime(_TIMESTAMP_FORMAT),
                ),
            )
        except sqlite3.IntegrityError as exc:
            if _is_active_return_violation(exc):
                return {"outcome": "duplicate", "return_id": None}, False
            raise
        return {"outcome": "created", "return_id": return_id}, True

    return _run_write_transaction(work, _RETURN_FAILED)


def create_support_ticket(
    authenticated_customer_id: str,
    issue_type: str,
    description: str,
    order_id: Optional[str] = None,
    conversation_summary: Optional[str] = None,
) -> dict[str, Any]:
    """Create a support ticket, optionally linked to an order the customer owns.

    Contract: docs/01 Sections G and H; docs/02 Sections 4 and 5.

    An ``order_id`` that does not exist and one owned by another
    customer give the same stored row (``order_id`` NULL) and the same
    result, so ticket creation never reveals whether an unowned order
    exists. ``status``, ``priority`` and ``created_at`` use the schema
    defaults.

    Returns:
        ``{"outcome": "created", "ticket_id": ..., "order_id": <owned
        order ID or None>, "order_linked": <bool>}`` after COMMIT, or
        ``{"outcome": "failed", "ticket_id": None, "order_id": None,
        "order_linked": False}``.

    Raises:
        TypeError: wrong argument type (including a non-string,
            non-``None`` ``order_id`` or ``conversation_summary``).
        ValueError: blank or whitespace-only string (never converted to
            ``None``).
        Any non-database exception raised during the write is rolled
        back and re-raised.
    """
    _require_string(authenticated_customer_id, "authenticated_customer_id")
    _require_string(issue_type, "issue_type")
    _require_string(description, "description")
    if order_id is not None:
        _require_string(order_id, "order_id")
    if conversation_summary is not None:
        _require_string(conversation_summary, "conversation_summary")

    def work(connection: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
        linked_order_id: Optional[str] = None
        if order_id is not None:
            owned = connection.execute(
                "SELECT 1 FROM orders WHERE customer_id = ? AND order_id = ?",
                (authenticated_customer_id, order_id),
            ).fetchone()
            if owned is not None:
                linked_order_id = order_id

        ticket_id = f"TKT-{uuid.uuid4().hex[:12].upper()}"
        connection.execute(
            "INSERT INTO support_tickets "
            "(ticket_id, customer_id, order_id, issue_type, description, "
            "conversation_summary) VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                authenticated_customer_id,
                linked_order_id,
                issue_type,
                description,
                conversation_summary,
            ),
        )
        return {
            "outcome": "created",
            "ticket_id": ticket_id,
            "order_id": linked_order_id,
            "order_linked": linked_order_id is not None,
        }, True

    return _run_write_transaction(work, _TICKET_FAILED)
