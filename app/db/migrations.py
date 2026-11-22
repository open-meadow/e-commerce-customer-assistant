"""Operator-run schema migrations (Phase 2; design TICKET_SCHEMA_AND_MIGRATION_DESIGN.md section 5).

Nothing in this module runs automatically: it is not called at import, startup, health check or first request.
``migrate`` is invoked by ``python -m app.db.migrate`` and by tests on scratch databases.

Authorised scope (owner decision OD-06): design and **scratch-database** testing only. ``migrate`` refuses to run unless the
caller asserts ``scratch=True``; a later run on any other database needs a separate explicit approval, a verified backup,
compatibility checks and a tested restoration, and enabling it is a deliberate code change, not a flag.

Registry: ``MIGRATIONS`` maps a target version to the function that upgrades from ``target - 1``. ``SCHEMA_VERSION`` equals
the highest registered number. Each migration runs inside ONE ``BEGIN IMMEDIATE`` transaction with foreign keys OFF (SQLite's
documented table-rebuild procedure) and verifies ``PRAGMA foreign_key_check`` before it commits; any failure rolls back and the
original file is untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from typing import Any, Callable, Optional

from app.db import clock
from app.db.connection import (
    PROTECTED_DB_PATH,
    SCHEMA_VERSION,
    _same_path,
    get_initialization_connection,
    get_migration_connection,
    get_readonly_connection,
    resolve_db_path,
)
from app.db.errors import ConnectionError_, DatabaseError, MigrationError
from app.db.schema import create_schema, ticket_rebuild_statements

# Test-only fault injection: a callable invoked at the named point inside the migration transaction. Production code
# never sets it; tests use it to prove that a failure after the DROP leaves the file byte-identical.
_FAULT_HOOK: Optional[Callable[[str], None]] = None

_ACTIVE_LEGACY = ("Open", "In Progress")


def _fault(point: str) -> None:
    if _FAULT_HOOK is not None:
        _FAULT_HOOK(point)


# ---------------------------------------------------------------------------
# Inspection helpers (read-only)
# ---------------------------------------------------------------------------

def user_tables(connection: sqlite3.Connection) -> list[str]:
    return [r[0] for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {t: connection.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in user_tables(connection)}


def table_digest(connection: sqlite3.Connection, table: str, columns: Optional[list[str]] = None) -> str:
    """SHA-256 over the rows of ``table`` in rowid order (all columns, or the named ones)."""
    cols = ", ".join(f'"{c}"' for c in columns) if columns else "*"
    digest = hashlib.sha256()
    for row in connection.execute(f'SELECT {cols} FROM "{table}" ORDER BY rowid'):
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def content_digests(connection: sqlite3.Connection, tables: Optional[list[str]] = None) -> dict[str, str]:
    return {t: table_digest(connection, t) for t in (tables if tables is not None else user_tables(connection))}


def schema_signature(connection: sqlite3.Connection) -> dict[str, Any]:
    """A whitespace-normalised description of the schema, for the fresh-versus-migrated parity comparison."""
    objects = sorted(
        (r[0], r[1], r[2], " ".join(r[3].split()))
        for r in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
    )
    detail: dict[str, Any] = {}
    for table in user_tables(connection):
        detail[table] = {
            "columns": [tuple(r) for r in connection.execute(f'PRAGMA table_info("{table}")')],
            "foreign_keys": sorted(tuple(r)[2:] for r in connection.execute(f'PRAGMA foreign_key_list("{table}")')),
            "indexes": sorted((r[1], r[2], r[3], r[4]) for r in connection.execute(f'PRAGMA index_list("{table}")')),
        }
    return {"user_version": connection.execute("PRAGMA user_version").fetchone()[0], "objects": objects, "tables": detail}


def fresh_schema_signature() -> dict[str, Any]:
    """The signature of a freshly created database at ``SCHEMA_VERSION`` (built in memory, never on disk)."""
    connection = get_initialization_connection(":memory:")
    try:
        create_schema(connection)
        return schema_signature(connection)
    finally:
        connection.close()


def _integrity(connection: sqlite3.Connection) -> list[str]:
    problems = []
    result = [r[0] for r in connection.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        problems.append("integrity_check: " + "; ".join(result[:3]))
    fk = connection.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems.append(f"foreign_key_check reports {len(fk)} violation(s)")
    return problems


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def preflight_v1(connection: sqlite3.Connection, db_path: str) -> dict[str, Any]:
    """Read-only report for the version-1 to version-2 migration."""
    report: dict[str, Any] = {"problems": _integrity(connection), "counts": table_counts(connection)}
    report["legacy_active_duplicates"] = [
        dict(customer_id=r[0], issue_type=r[1], order_id=r[2], tickets=r[3])
        for r in connection.execute(
            "SELECT customer_id, issue_type, order_id, COUNT(*) FROM support_tickets "
            "WHERE order_id IS NOT NULL AND status IN ('Open', 'In Progress') "
            "GROUP BY customer_id, issue_type, order_id HAVING COUNT(*) > 1")
    ]
    violating = connection.execute(
        "SELECT COUNT(*) FROM support_tickets WHERE order_id IS NOT NULL AND instr(order_id, ':') > 0").fetchone()[0]
    report["rows_violating_new_checks"] = violating
    if violating:
        report["problems"].append(f"{violating} ticket row(s) hold an order id containing ':' (refused by the new CHECK)")
    size = os.path.getsize(db_path)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free
    report["database_bytes"] = size
    report["free_bytes"] = free
    if free < 2 * size:
        report["problems"].append("free disk space is below twice the database size")
    return report


# ---------------------------------------------------------------------------
# Backup, verification and restore rehearsal
# ---------------------------------------------------------------------------

def _check_backup_path(db_path: str, backup_path: str) -> str:
    backup = resolve_db_path(backup_path)
    if backup == ":memory:":
        raise MigrationError("the backup path must be a file")
    if _same_path(backup, PROTECTED_DB_PATH):
        raise MigrationError("the protected legacy database is never a backup target")
    if _same_path(backup, db_path):
        raise MigrationError("the backup path must differ from the database path")
    if not os.path.isdir(os.path.dirname(backup)):
        raise MigrationError("the backup parent directory does not exist")
    if os.path.exists(backup) or any(os.path.exists(backup + s) for s in ("-wal", "-shm", "-journal")):
        raise MigrationError("the backup path already exists; it is never overwritten")
    return backup


def _compare(source_counts, source_digests, other: sqlite3.Connection, label: str) -> list[str]:
    problems = _integrity(other)
    if table_counts(other) != source_counts:
        problems.append(f"{label}: row counts differ from the source")
    if content_digests(other, list(source_counts)) != source_digests:
        problems.append(f"{label}: content checksums differ from the source")
    return problems


def make_verified_backup(connection: sqlite3.Connection, backup: str) -> dict[str, Any]:
    """``VACUUM INTO`` a new file, verify it, then rehearse a restore to a second scratch path."""
    counts = table_counts(connection)
    digests = content_digests(connection)
    connection.execute("VACUUM INTO ?", (backup,))
    copy = get_readonly_connection(backup, check_version=False)
    try:
        problems = _compare(counts, digests, copy, "backup")
    finally:
        copy.close()
    rehearsal = backup + ".restore-rehearsal"
    if os.path.exists(rehearsal):
        raise MigrationError("a restore-rehearsal file already exists next to the backup path")
    shutil.copyfile(backup, rehearsal)
    try:
        restored = get_readonly_connection(rehearsal, check_version=False)
        try:
            problems += _compare(counts, digests, restored, "restore rehearsal")
        finally:
            restored.close()
    finally:
        try:
            os.remove(rehearsal)
        except OSError:
            pass
    if problems:
        raise MigrationError("backup verification failed: " + "; ".join(problems))
    return {"backup_path": backup, "backup_bytes": os.path.getsize(backup), "counts": counts, "verified": True,
            "restore_rehearsal": "passed"}


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def _migrate_1_to_2(connection: sqlite3.Connection) -> None:
    """Rebuild ``support_tickets`` and add ``ticket_events``, ``ticket_erasure_log`` and ``ticket_write_intents``.

    Runs inside the caller's transaction with foreign keys OFF. The old table is renamed aside, the final table is created
    with the exact text a fresh database uses (parity), rows are copied with the design's backfill (5.3), and the old table
    is dropped. Legacy summaries and descriptions are copied unchanged; no event is fabricated for legacy tickets.
    """
    statements = ticket_rebuild_statements()
    for index in ("idx_support_tickets_customer_id", "idx_support_tickets_order_id"):
        connection.execute(f"DROP INDEX IF EXISTS {index}")
    connection.execute("ALTER TABLE support_tickets RENAME TO support_tickets_v1")
    for ddl in statements["support_tickets"]:
        connection.execute(ddl)
    connection.execute(
        "INSERT INTO support_tickets (ticket_id, customer_id, order_id, item_id, issue_type, issue_key, description, "
        "conversation_summary, status, priority, parent_ticket_id, request_key, request_hash, created_at, updated_at, "
        "redacted_at) "
        "SELECT ticket_id, customer_id, order_id, NULL, issue_type, 'n:' || ticket_id, description, "
        "conversation_summary, status, priority, NULL, 'legacy:' || ticket_id, 'legacy', created_at, created_at, NULL "
        "FROM support_tickets_v1 ORDER BY rowid")
    _fault("after_copy")
    connection.execute("DROP TABLE support_tickets_v1")
    _fault("after_drop")
    for ddl in statements["side_tables"]:
        connection.execute(ddl)
    for ddl in statements["indexes"]:
        connection.execute(ddl)
    for ddl in statements["triggers"]:
        connection.execute(ddl)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {2: _migrate_1_to_2}


# ---------------------------------------------------------------------------
# The operator entry point
# ---------------------------------------------------------------------------

def _clean_reference(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 200:
        raise MigrationError("an authorization reference (non-empty text, at most 200 characters) is required")
    return value


def migrate(db_path: str, backup_path: str, authorization_reference: str, *, scratch: bool = False,
            dry_run: bool = False) -> dict[str, Any]:
    """Upgrade ``db_path`` to ``SCHEMA_VERSION`` after a verified backup. Returns a report dictionary.

    Refuses (raising ``MigrationError``, changing nothing): ``scratch`` not asserted; no authorization reference; a protected
    or default demo path; a missing backup path, an existing backup, a missing parent directory; a database that is already
    current, legacy, or newer; a database in use elsewhere; any pre-flight problem. ``dry_run`` runs the pre-flight only and
    writes nothing (no backup is created).
    """
    if scratch is not True:
        raise MigrationError("only a scratch-database migration is authorised (OD-06); a real migration needs a separate "
                             "approval and a deliberate code change")
    reference = _clean_reference(authorization_reference)
    if not isinstance(backup_path, str) or not backup_path.strip():
        raise MigrationError("--backup-path is required")
    try:
        connection = get_migration_connection(db_path)
    except ConnectionError_ as exc:
        raise MigrationError(f"the database path is refused: {exc}") from exc
    path = resolve_db_path(db_path)
    try:
        backup = _check_backup_path(path, backup_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        except sqlite3.Error as exc:
            raise MigrationError("the database is in use elsewhere (lock probe failed)") from exc
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = user_tables(connection)
        if version == 0:
            raise MigrationError("a legacy or empty database is not migratable by this tool")
        if version == SCHEMA_VERSION:
            raise MigrationError("the database is already at the current schema version; nothing to do")
        if version > SCHEMA_VERSION:
            raise MigrationError("the database holds a newer schema version")
        if version + 1 not in MIGRATIONS or any(v not in MIGRATIONS for v in range(version + 1, SCHEMA_VERSION + 1)):
            raise MigrationError(f"no migration path from version {version}")
        if version == 1 and "support_tickets" not in tables:
            raise MigrationError("the version-1 database has no support_tickets table")
        report: dict[str, Any] = {"authorization_reference": reference, "from_version": version,
                                  "to_version": SCHEMA_VERSION, "dry_run": bool(dry_run)}
        report["preflight"] = preflight_v1(connection, path)
        if report["preflight"]["problems"]:
            raise MigrationError("pre-flight failed: " + "; ".join(report["preflight"]["problems"]))
        if dry_run:
            report["result"] = "dry_run_ok"
            return report
        report["backup"] = make_verified_backup(connection, backup)
        before_counts = table_counts(connection)
        before_summaries = table_digest(connection, "support_tickets",
                                        ["ticket_id", "description", "conversation_summary"])
        try:
            connection.execute("BEGIN IMMEDIATE")
            for target in range(version + 1, SCHEMA_VERSION + 1):
                MIGRATIONS[target](connection)
                _fault(f"after_migration_{target}")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MigrationError(f"foreign_key_check reports {len(violations)} violation(s); rolled back")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            _fault("before_commit")
            connection.execute("COMMIT")
        except BaseException as exc:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, MigrationError):
                raise
            if isinstance(exc, (sqlite3.Error, DatabaseError)):
                raise MigrationError(f"the migration failed and was rolled back: {type(exc).__name__}") from exc
            raise
        connection.execute("PRAGMA foreign_keys = ON")
        after_counts = table_counts(connection)
        report["post_check"] = {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "counts_before": before_counts,
            "counts_after": after_counts,
            "legacy_rows_preserved": after_counts["support_tickets"] == before_counts["support_tickets"],
            "legacy_summaries_byte_identical": table_digest(
                connection, "support_tickets", ["ticket_id", "description", "conversation_summary"]) == before_summaries,
            "integrity": _integrity(connection),
            "parity_with_fresh_schema": schema_signature(connection) == fresh_schema_signature(),
            "completed_at": clock.format_timestamp(clock.utc_now()),
        }
        pc = report["post_check"]
        if not (pc["legacy_rows_preserved"] and pc["legacy_summaries_byte_identical"] and not pc["integrity"]
                and pc["parity_with_fresh_schema"]):
            raise MigrationError("post-migration verification failed; restore the verified backup: "
                                 + json.dumps(pc, default=str))
        report["result"] = "migrated"
        return report
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
