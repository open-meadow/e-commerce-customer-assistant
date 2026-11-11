"""Read-only database health check (docs/03 Section 9.4).

Never raises, never creates or modifies a database, takes no customer
identity. Statuses: ``not_initialized``, ``ready``,
``schema_incompatible`` (reason ``outdated`` or ``future``) and
``unavailable``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from app.db.connection import get_readonly_connection, read_schema_state
from app.db.errors import ConnectionError_, DatabaseNotInitializedError

NOT_INITIALIZED = "not_initialized"
READY = "ready"
SCHEMA_INCOMPATIBLE = "schema_incompatible"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class HealthResult:
    """Outcome of ``check_database_health``."""

    status: str
    reason: Optional[str] = None


def check_database_health(db_path: Optional[str] = None) -> HealthResult:
    """Classify the configured database without changing anything."""
    try:
        connection = get_readonly_connection(db_path, check_version=False)
    except DatabaseNotInitializedError:
        return HealthResult(NOT_INITIALIZED)
    except ConnectionError_:
        return HealthResult(UNAVAILABLE)
    try:
        state = read_schema_state(connection)
    except sqlite3.Error:
        return HealthResult(UNAVAILABLE)
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
    if state == "empty":
        return HealthResult(NOT_INITIALIZED)
    if state == "legacy":
        return HealthResult(SCHEMA_INCOMPATIBLE, "outdated")
    if state == "future":
        return HealthResult(SCHEMA_INCOMPATIBLE, "future")
    return HealthResult(READY)
