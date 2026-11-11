"""Typed database errors (docs/01 Section F item 8, Section I).

Four kinds of outcome are kept distinct (master handover Section 8.9):

1. Business outcomes (for example an ineligible return) are *returned*
   through documented result dictionaries. They are never exceptions.
2. Infrastructure and operational database errors: ``DatabaseError``
   subclasses ``ConnectionError_`` and ``DatabaseOperationalError``.
3. Persisted-data-integrity errors: ``DatabaseDataIntegrityError``.
4. Database constraint violations: ``DatabaseConstraintError``.

Messages are safe to log. They never contain customer text, SQL, or
filesystem paths beyond what the caller supplied. Customer-facing code
must not show them to customers.
"""

from __future__ import annotations


class DatabaseError(Exception):
    """Base class for every typed database error."""


class ConnectionError_(DatabaseError):
    """A connection could not be acquired or is unsafe to use.

    The trailing underscore avoids shadowing the builtin
    ``ConnectionError``. The original exception, when there is one, is
    chained as ``__cause__``.
    """


class DatabaseNotInitializedError(ConnectionError_):
    """The database file is missing, or it has no schema yet."""


class SchemaIncompatibleError(ConnectionError_):
    """The database schema version is not the one this code supports.

    ``reason`` is ``"outdated"`` (existing tables at ``user_version`` 0,
    a legacy pre-D2 database) or ``"future"`` (``user_version`` greater
    than the supported version).
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class DatabaseOperationalError(DatabaseError):
    """An unexpected ``sqlite3.Error`` during a database operation."""


class DatabaseConstraintError(DatabaseError):
    """A database constraint was violated (for example an ID collision)."""


class DatabaseDataIntegrityError(DatabaseError):
    """Persisted data violates an invariant (for example a bad timestamp)."""
