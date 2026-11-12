"""The only module allowed to open SQLite connections (docs/01 Section I).

Three factories, each with one job (master handover Section 8.7):

``get_connection()``
    Normal runtime. Refuses the protected legacy ``app/db/data/app.db``. SQLite URI ``mode=rw``: it can never create a
    database. Enables and verifies ``PRAGMA foreign_keys = ON``, checks
    ``PRAGMA user_version`` on **every** connection, and raises a typed
    error for an incompatible database.

``get_readonly_connection()``
    Health and readiness. ``mode=ro`` plus ``PRAGMA query_only = ON``.
    Never creates or modifies a database. Refuses databases whose
    read-only safety cannot be established (WAL mode, or leftover
    ``-wal``/``-shm``/``-journal`` files) instead of using
    ``immutable=1``.

``get_initialization_connection()``
    The only creation path (``mode=rwc``). Requires an explicit path,
    refuses the protected ``app/db/data/app.db``, refuses unsafe sidecar and
    journal states, and never opens a legacy database for changes.
    ``schema.create_schema`` then re-checks eligibility inside
    ``BEGIN IMMEDIATE`` before any DDL.

Nothing else in the project may call ``sqlite3.connect()``.

Database path: ``APP_DB_PATH`` if set, otherwise
``<project-root>/data/app_d2.db``. A relative value resolves from the
project root, never from the working directory. The legacy
``app/db/data/app.db`` is protected: no factory opens it, and nothing migrates
it. The default file is never created by normal startup; it is created
only by the explicit seed command (``python -m app.db.seed_data``).

Schema versions (``PRAGMA user_version``): 0 means uninitialized when no
tables exist and a legacy pre-D2 database when tables exist; 1 is the
current schema; greater than 1 is a future schema. There is no migration
(M2: fresh database only).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from app.db.errors import (
    ConnectionError_,
    DatabaseNotInitializedError,
    SchemaIncompatibleError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = str(PROJECT_ROOT / "app" / "db" / "data" / "app_d2.db")
PROTECTED_DB_PATH = str(PROJECT_ROOT / "app" / "db" / "data" / "app.db")
ENV_DB_PATH = "APP_DB_PATH"
SCHEMA_VERSION = 1
CONNECT_TIMEOUT_SECONDS = 5.0
MEMORY_PATH = ":memory:"

_SQLITE_MAGIC = b"SQLite format 3\x00"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

__all__ = [
    "ConnectionError_",
    "DEFAULT_DB_PATH",
    "ENV_DB_PATH",
    "PROTECTED_DB_PATH",
    "SCHEMA_VERSION",
    "get_connection",
    "get_initialization_connection",
    "get_readonly_connection",
    "read_schema_state",
    "resolve_db_path",
]


def resolve_db_path(db_path: Optional[str] = None) -> str:
    """Resolve the database path (explicit argument, then ``APP_DB_PATH``).

    ``None`` uses ``APP_DB_PATH`` or the default. Absolute paths are
    used as given. Relative paths resolve from the project root.
    ``":memory:"`` is returned unchanged.

    Raises:
        ConnectionError_: if the path is empty or contains NUL.
    """
    if db_path is None:
        db_path = os.environ.get(ENV_DB_PATH) or DEFAULT_DB_PATH
    if not isinstance(db_path, str) or not db_path.strip() or "\x00" in db_path:
        raise ConnectionError_("invalid database path")
    if db_path == MEMORY_PATH:
        return db_path
    path = Path(db_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return os.path.normpath(str(path))


def _same_path(first: str, second: str) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(
        os.path.realpath(second)
    )


def _refuse_protected(path: str) -> None:
    """Raise before anything is opened if *path* is the protected legacy file."""
    if path != MEMORY_PATH and _same_path(path, PROTECTED_DB_PATH):
        raise ConnectionError_("the protected legacy database cannot be opened")


def _uri(path: str, mode: str) -> str:
    return f"{Path(path).as_uri()}?mode={mode}"


def _sidecars_present(path: str) -> bool:
    return any(os.path.exists(path + suffix) for suffix in _SIDECAR_SUFFIXES)


def _check_file_header(path: str) -> bool:
    """Return True if the file is empty; raise if it is unsafe or not SQLite.

    A plain binary read of the first 100 bytes. Accepts only the
    rollback-journal format (version bytes 18 and 19 both equal to 1).
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(100)
    except OSError as exc:
        raise ConnectionError_("database file is not readable") from exc
    if len(header) == 0:
        return True
    if len(header) < 100 or not header.startswith(_SQLITE_MAGIC):
        raise ConnectionError_("file is not a SQLite database")
    if header[18] != 1 or header[19] != 1:
        raise ConnectionError_("database journal mode is not supported")
    return False


def _close_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001 - must never mask the original error
        pass


def _open(uri: str, *, uri_mode: bool = True) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            uri, uri=uri_mode, timeout=CONNECT_TIMEOUT_SECONDS
        )
    except sqlite3.Error as exc:
        raise ConnectionError_("failed to open the database") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _enable_foreign_keys(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("PRAGMA foreign_keys = ON;")
        value = connection.execute("PRAGMA foreign_keys;").fetchone()[0]
    except sqlite3.Error as exc:
        _close_quietly(connection)
        raise ConnectionError_("failed to enable foreign keys") from exc
    if value != 1:
        _close_quietly(connection)
        raise ConnectionError_(
            "foreign-key enforcement is not active; refusing the connection"
        )


def read_schema_state(connection: sqlite3.Connection) -> str:
    """Classify the database: ``empty``, ``legacy``, ``current`` or ``future``.

    Reads ``PRAGMA user_version`` and whether any user table exists.
    ``user_version`` 0 with no tables is ``empty``; with tables it is
    ``legacy`` (a pre-D2 database). Version 1 is ``current``. Any other
    value is ``future``.
    """
    version = connection.execute("PRAGMA user_version;").fetchone()[0]
    tables = connection.execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    if version == 0:
        return "empty" if tables == 0 else "legacy"
    if version == SCHEMA_VERSION:
        return "current"
    return "future"


def _require_current(connection: sqlite3.Connection) -> None:
    try:
        state = read_schema_state(connection)
    except sqlite3.Error as exc:
        _close_quietly(connection)
        raise ConnectionError_("failed to read the schema version") from exc
    if state == "current":
        return
    _close_quietly(connection)
    if state == "empty":
        raise DatabaseNotInitializedError("the database has no schema")
    if state == "legacy":
        raise SchemaIncompatibleError(
            "legacy pre-D2 database (user_version 0 with tables)", "outdated"
        )
    raise SchemaIncompatibleError("unsupported newer schema version", "future")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a normal runtime connection to an existing, current database.

    Never creates a database or a directory. Foreign keys are on and
    verified, rows are ``sqlite3.Row``, the lock-wait timeout is 5.0
    seconds, and ``user_version`` is checked on every call.

    Raises:
        DatabaseNotInitializedError: the file is missing or has no schema.
        SchemaIncompatibleError: legacy (``outdated``) or newer
            (``future``) schema.
        ConnectionError_: any other failure to open the database.
    """
    path = resolve_db_path(db_path)
    if path == MEMORY_PATH:
        raise ConnectionError_("an in-memory database cannot be opened by name")
    _refuse_protected(path)
    if not os.path.isfile(path):
        raise DatabaseNotInitializedError("the database file does not exist")
    connection = _open(_uri(path, "rw"))
    _enable_foreign_keys(connection)
    _require_current(connection)
    return connection


def get_readonly_connection(
    db_path: Optional[str] = None, *, check_version: bool = True
) -> sqlite3.Connection:
    """Open a read-only connection for health checks.

    Uses ``mode=ro`` and ``PRAGMA query_only = ON`` (verified). Before
    SQLite opens anything, the file header is read in binary and sidecar
    files are looked for. A WAL-mode database, or any ``-wal``, ``-shm``
    or ``-journal`` file, is refused, because read-only safety cannot
    be established. ``immutable=1`` is never used. A change made by
    another process after these checks is a residual race that this
    function cannot close.

    Args:
        check_version: when False the schema version is not enforced, so
            the health check can classify an incompatible database.

    Raises:
        DatabaseNotInitializedError: missing or empty file.
        SchemaIncompatibleError: incompatible version (when checked).
        ConnectionError_: any other unsafe or failed state.
    """
    path = resolve_db_path(db_path)
    if path == MEMORY_PATH:
        raise ConnectionError_("an in-memory database cannot be opened by name")
    _refuse_protected(path)
    if not os.path.isfile(path):
        raise DatabaseNotInitializedError("the database file does not exist")
    if _sidecars_present(path):
        raise ConnectionError_("sidecar files present; read-only safety unknown")
    if _check_file_header(path):
        raise DatabaseNotInitializedError("the database file is empty")
    connection = _open(_uri(path, "ro"))
    try:
        connection.execute("PRAGMA query_only = ON;")
        flag = connection.execute("PRAGMA query_only;").fetchone()[0]
    except sqlite3.Error as exc:
        _close_quietly(connection)
        raise ConnectionError_("failed to set query_only") from exc
    if flag != 1:
        _close_quietly(connection)
        raise ConnectionError_("query_only is not active; refusing the connection")
    if check_version:
        _require_current(connection)
    return connection


def get_initialization_connection(db_path: str) -> sqlite3.Connection:
    """Open the only connection that may create a database.

    The path is required and explicit. The protected ``app/db/data/app.db`` is
    always refused. The parent directory must already exist, sidecar
    files must be absent, and an existing non-empty file must be a
    rollback-journal SQLite database. ``":memory:"`` is allowed for
    tests. This function does not change the schema: ``create_schema``
    takes ``BEGIN IMMEDIATE`` and repeats the eligibility checks before
    any DDL.

    Raises:
        ConnectionError_: unsafe, protected or unopenable path.
    """
    if db_path is None:
        raise ConnectionError_("an explicit database path is required")
    path = resolve_db_path(db_path)
    if path == MEMORY_PATH:
        connection = _open(MEMORY_PATH, uri_mode=False)
        _enable_foreign_keys(connection)
        return connection
    _refuse_protected(path)
    if not os.path.isdir(os.path.dirname(path)):
        raise ConnectionError_("the parent directory does not exist")
    if _sidecars_present(path):
        raise ConnectionError_("sidecar files present; refusing to initialize")
    if os.path.exists(path):
        if not os.path.isfile(path):
            raise ConnectionError_("the database path is not a file")
        _check_file_header(path)
    connection = _open(_uri(path, "rwc"))
    _enable_foreign_keys(connection)
    return connection
