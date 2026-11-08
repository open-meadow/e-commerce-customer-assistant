"""Read-only database repository (Checkpoint 5).

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

Scope (Checkpoint 5 only)
--------------------------
Only the five read operations below are implemented. No write
operations (``create_return_request``, ``create_support_ticket``) are
implemented here — those belong to a later checkpoint, per
docs/02_database_implementation_plan.md Section 4.

Connection lifecycle
----------------------
Each function here opens its own connection via
``app.db.connection.get_connection()``, runs exactly one query, and
closes the connection in a ``finally`` block before returning — even
if the query raises. No connection is held open across function
calls, and no connection is ever exposed to a caller of this module;
callers only ever receive plain Python data (dicts, lists, or
``None``), never a cursor or connection object. This keeps all
database access centralized in this one module, per the Checkpoint 5
scope restrictions.

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
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from app.db.connection import get_connection


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
