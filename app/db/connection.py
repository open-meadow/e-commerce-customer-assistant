"""Centralized SQLite connection handling.

Scope (Checkpoint 1 only — see docs/02_database_implementation_plan.md
Section 3, step 1, and Section 7, Checkpoint 1):

    This module is responsible for ONE thing: opening a SQLite connection
    that is guaranteed to have foreign-key enforcement turned on, and
    doing so the same way every single time it is called.

    It does NOT create tables (that is app/db/schema.py, Checkpoint 2).
    It does NOT seed data (app/db/seed_data.py, Checkpoint 4).
    It does NOT contain any business logic or table-specific queries
    (app/db/repository.py, Checkpoints 5 and 7).

Why this module must be the only place that opens a connection
-----------------------------------------------------------------
SQLite does NOT enforce foreign keys by default. Unless
``PRAGMA foreign_keys = ON;`` is executed on a connection, every
composite foreign key approved in docs/01_database_design.md
(Section B / Section C) is silently unenforced on that connection,
with no error raised anywhere. This is flagged as the single
highest-leverage implementation risk in docs/01_database_design.md
Section L, item 7.

The only way to guarantee the pragma is always set is to make sure
there is exactly one function, in exactly one place, that is ever
used to open a connection, and to make every other part of the
application go through it. If a second code path somewhere else in
the project called ``sqlite3.connect()`` directly, it would silently
bypass this protection. For that reason:

    Every other module in this project MUST call ``get_connection()``
    from this file to obtain a database connection. No other module
    should call ``sqlite3.connect()`` directly.
"""

from __future__ import annotations

import os
import sqlite3


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default location of the SQLite database file for this project, relative
# to the project root. Schema creation (Checkpoint 2) and seeding
# (Checkpoint 4) will write into this same file; this checkpoint does not
# create any tables in it.
#
# Per docs/02_database_implementation_plan.md Section 2, this file is not
# checked into version control.
DEFAULT_DB_PATH = os.path.join("data", "app.db")


class ConnectionError_(Exception):
    """Raised when a SQLite connection cannot be opened or configured.

    A dedicated exception type (rather than letting a raw lower-level
    error propagate unchanged) is used so that callers of
    ``get_connection()`` have one predictable exception to catch. What
    is chained as this exception's ``__cause__`` depends on which kind
    of failure raised it, and is not the same in every case:

    * **Filesystem failures** (the parent directory for a file-based
      database cannot be created) chain the underlying ``OSError`` as
      the cause.
    * **SQLite failures** (the connection cannot be opened, the
      foreign-key pragma cannot be executed, or it cannot be read
      back) chain the underlying ``sqlite3.Error`` as the cause.
    * **The foreign-key verification failure** (the pragma executed
      without error but read back as something other than ``1``) has
      **no underlying exception at all** — it is an application-level
      check on a returned value, not a wrapped lower-level error, so
      in this case ``__cause__`` is ``None``.

    In every case, whichever underlying exception exists (if any) is
    never silently swallowed — callers can inspect ``__cause__`` to
    distinguish these situations if needed.

    Named ``ConnectionError_`` with a trailing underscore to avoid
    shadowing Python's built-in ``ConnectionError``.
    """


def _close_quietly(connection: sqlite3.Connection) -> None:
    """Close a connection during failure cleanup without masking the error.

    This is only ever called on a failure path, immediately before
    ``get_connection()`` raises ``ConnectionError_`` for some other,
    already-known reason. If ``connection.close()`` itself raised here
    and that exception were allowed to propagate, it would replace the
    original, more informative error (e.g. "failed to enable foreign
    keys") with a less useful one about failing to close a connection
    that never finished setting up correctly. The original exception is
    what the caller needs to see and is what gets chained as the cause
    of ``ConnectionError_`` immediately after this call returns, so any
    failure here is deliberately discarded rather than raised.

    Any ordinary exception from ``close()`` is suppressed here, not
    just ``sqlite3.Error`` — a close failure is not expected to be a
    SQLite-specific error, and narrowing the catch to one exception
    type would risk letting an unanticipated kind of close failure
    still mask the original error this helper exists to protect. This
    broad suppression is intentionally confined to this one cleanup
    helper; no other part of this module catches exceptions this
    broadly.
    """
    try:
        connection.close()
    except Exception:
        pass


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with foreign-key enforcement guaranteed on.

    This is the single, centralized entry point for obtaining a database
    connection anywhere in this project (see module docstring).

    Steps performed, in order, every time this function is called:

      1. Ensure the directory containing the database file exists, so
         that opening a brand-new database on a clean checkout does not
         fail with "unable to open database file". This does not create
         the database file itself or any tables — SQLite creates the
         file automatically on connect if it does not already exist.
      2. Open the connection.
      3. Execute ``PRAGMA foreign_keys = ON;`` immediately — before any
         other statement is allowed to run on this connection.
      4. Read the pragma back with ``PRAGMA foreign_keys;`` and verify it
         reports ``1``. This step exists because *executing* the pragma
         statement and it actually *taking effect* are not the same
         thing to simply assume — verifying the read-back value is what
         docs/01_database_design.md Section K, test 12 (corrected) and
         docs/02_database_implementation_plan.md Section 6 require, and
         this function performs that verification on every single call,
         not only in a separate test.
      5. Set ``row_factory = sqlite3.Row`` so that rows returned by any
         future query (schema tests, repository functions in later
         checkpoints) can be accessed by column name. This is a
         standard-library feature, not an ORM or an added abstraction
         layer — it changes how a row is read, not how queries are
         written or how tables are modeled.

    Args:
        db_path: Path to the SQLite database file. Defaults to
            ``DEFAULT_DB_PATH``. Passing ``":memory:"`` opens a
            private in-memory database instead, which later
            checkpoints' automated tests are expected to use so that
            each test run starts from a clean, isolated database.

    Returns:
        An open ``sqlite3.Connection`` with foreign-key enforcement
        confirmed on and ``row_factory`` set to ``sqlite3.Row``.

    Raises:
        ConnectionError_: raised for two distinct kinds of failure,
            which do not carry the same kind of cause:

            * Directory creation, connection opening, pragma
              execution, or pragma read-back fails at the SQLite or
              filesystem level. In these cases the underlying
              ``OSError`` (directory creation) or ``sqlite3.Error``
              (everything else) is preserved as the raised
              exception's ``__cause__`` via exception chaining
              (``raise ... from exc``).
            * The pragma's read-back value is not ``1``, meaning
              enforcement did not actually take effect even though no
              SQLite or filesystem call failed. This is an
              application-level check on the returned value, not a
              wrapped lower-level error, so this branch raises
              ``ConnectionError_`` directly with no underlying
              exception — its ``__cause__`` is ``None``.
    """
    # Step 1: ensure the containing directory exists (skip for ":memory:",
    # which has no directory component). Wrapped in its own try/except so
    # a filesystem-level failure here (e.g. a read-only mount, a
    # permissions problem) is translated into the same ConnectionError_
    # every other failure in this function raises, rather than leaking
    # out as a raw, uncaught OSError.
    if db_path != ":memory:":
        directory = os.path.dirname(db_path)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                raise ConnectionError_(
                    f"Failed to create directory '{directory}' for SQLite "
                    f"database at '{db_path}': {exc}"
                ) from exc

    # Step 2: open the connection. Any failure here (e.g. a permissions
    # problem, a locked file, a malformed path) is caught and re-raised
    # as ConnectionError_ with context, never swallowed.
    try:
        connection = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        raise ConnectionError_(
            f"Failed to open SQLite connection at '{db_path}': {exc}"
        ) from exc

    # Step 3: turn on foreign-key enforcement immediately, before any
    # other statement is allowed to run on this connection.
    try:
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as exc:
        _close_quietly(connection)
        raise ConnectionError_(
            f"Failed to execute 'PRAGMA foreign_keys = ON;' on connection "
            f"to '{db_path}': {exc}"
        ) from exc

    # Step 4: verify the pragma actually took effect. This does not
    # trust that step 3 succeeding silently means enforcement is on —
    # it reads the setting back and checks the value explicitly.
    try:
        cursor = connection.execute("PRAGMA foreign_keys;")
        result = cursor.fetchone()
    except sqlite3.Error as exc:
        _close_quietly(connection)
        raise ConnectionError_(
            f"Failed to verify 'PRAGMA foreign_keys' on connection to "
            f"'{db_path}': {exc}"
        ) from exc

    enforced_value = result[0] if result is not None else None
    if enforced_value != 1:
        _close_quietly(connection)
        raise ConnectionError_(
            f"Foreign-key enforcement is not active on connection to "
            f"'{db_path}'. Expected 'PRAGMA foreign_keys' to read back 1, "
            f"got {enforced_value!r}. Refusing to return an unenforced "
            f"connection, since every composite foreign key in the "
            f"approved schema (docs/01_database_design.md Section C) "
            f"depends on this being on."
        )

    # Step 5: consistent row access for every connection this project
    # ever opens.
    connection.row_factory = sqlite3.Row

    return connection
