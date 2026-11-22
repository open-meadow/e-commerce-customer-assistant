"""Phase 2: the operator-run version-1 to version-2 migration, on scratch databases only (NT-74..81, NT-141..143)."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from app.db import migrate as migrate_cli
from app.db import migrations
from app.db.connection import (
    APP_D2_DB_PATH,
    PROTECTED_DB_PATH,
    SCHEMA_VERSION,
    get_connection,
    get_migration_connection,
    get_readonly_connection,
)
from app.db.errors import MigrationError, SchemaIncompatibleError
from conftest import build_database, dir_entries, fingerprint
from phase2_support import build_v1_database

SUMMARY_UNICODE = "Customer Issue\n  उत्पाद \U0001F600 broken \t\n"
LEGACY = [
    ("TKT_" + "a" * 32, "C101", "ORD1001", "payment", "charged twice", "Customer Issue\ncharged twice", "Open",
     "2026-10-01T00:00:00Z"),
    ("TKT_" + "b" * 32, "C101", None, "general", "d  with  spaces ", SUMMARY_UNICODE, "Resolved", "2026-10-02T00:00:00Z"),
    ("TKT_" + "c" * 32, "C102", None, "product", "x" * 2000, "s" * 2000, "In Progress", "2026-10-03T00:00:00Z"),
]


@pytest.fixture
def v1(tmp_path):
    return build_v1_database(str(tmp_path / "v1.db"), LEGACY)


def run(v1_path, tmp_path, **kwargs):
    kwargs.setdefault("scratch", True)
    return migrations.migrate(v1_path, str(tmp_path / "backup.db"), "scratch-test-reference", **kwargs)


class TestMigrationFromVersionOne:
    def test_a_version_one_database_is_outdated_not_usable(self, v1):
        with pytest.raises(SchemaIncompatibleError) as info:
            get_connection(v1)
        assert info.value.reason == "outdated"

    def test_migrates_preserves_legacy_rows_and_summaries_byte_for_byte(self, v1, tmp_path):
        before = sqlite3.connect(v1)
        rows_before = before.execute(
            "SELECT ticket_id, customer_id, order_id, issue_type, description, conversation_summary, status, priority, "
            "created_at FROM support_tickets ORDER BY rowid").fetchall()
        before.close()
        report = run(v1, tmp_path)
        assert report["result"] == "migrated" and report["from_version"] == 1 and report["to_version"] == SCHEMA_VERSION
        post = report["post_check"]
        assert post["legacy_rows_preserved"] and post["legacy_summaries_byte_identical"]
        assert post["parity_with_fresh_schema"] and post["integrity"] == [] and post["user_version"] == SCHEMA_VERSION
        connection = get_connection(v1)
        try:
            rows_after = connection.execute(
                "SELECT ticket_id, customer_id, order_id, issue_type, description, conversation_summary, status, priority, "
                "created_at FROM support_tickets ORDER BY rowid").fetchall()
            assert [tuple(r) for r in rows_after] == rows_before  # byte-identical text, same order
            assert rows_after[1]["conversation_summary"] == SUMMARY_UNICODE
        finally:
            connection.close()

    def test_backfill_values_and_no_markers_or_fabricated_events(self, v1, tmp_path):
        run(v1, tmp_path)
        connection = get_connection(v1)
        try:
            for row in connection.execute("SELECT * FROM support_tickets"):
                assert row["issue_key"] == "n:" + row["ticket_id"]
                assert row["request_key"] == "legacy:" + row["ticket_id"] and row["request_hash"] == "legacy"
                assert row["item_id"] is None and row["parent_ticket_id"] is None and row["redacted_at"] is None
                assert row["updated_at"] == row["created_at"]
            assert connection.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0] == 0  # no fabricated history
            assert connection.execute("SELECT COUNT(*) FROM ticket_write_intents").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM ticket_erasure_log").fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM support_tickets WHERE conversation_summary LIKE '%marker%'").fetchone()[0] == 0
        finally:
            connection.close()

    def test_the_backup_is_verified_and_the_rehearsal_file_is_removed(self, v1, tmp_path):
        report = run(v1, tmp_path)
        backup = report["backup"]
        assert backup["verified"] and backup["restore_rehearsal"] == "passed"
        assert os.path.isfile(backup["backup_path"]) and not os.path.exists(backup["backup_path"] + ".restore-rehearsal")
        copy = get_readonly_connection(backup["backup_path"], check_version=False)
        try:
            assert copy.execute("PRAGMA user_version").fetchone()[0] == 1  # the backup is the pre-migration database
            assert copy.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == len(LEGACY)
        finally:
            copy.close()

    def test_twenty_identical_legacy_active_tickets_migrate_cleanly_and_stay_unblocked(self, tmp_path):
        many = [(f"TKT_{n:032x}", "C101", "ORD1001", "payment", "same", "same summary", "Open", "2026-10-01T00:00:00Z")
                for n in range(20)]
        path = build_v1_database(str(tmp_path / "dup.db"), many)
        report = run(path, tmp_path)
        assert len(report["preflight"]["legacy_active_duplicates"]) == 1
        connection = get_connection(path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'Open'").fetchone()[0] == 20
        finally:
            connection.close()

    def test_a_dry_run_writes_nothing_and_creates_no_backup(self, v1, tmp_path):
        before = fingerprint(v1)
        report = run(v1, tmp_path, dry_run=True)
        assert report["result"] == "dry_run_ok" and fingerprint(v1) == before
        assert not os.path.exists(str(tmp_path / "backup.db"))

    def test_user_version_participates_in_the_transaction(self, v1, tmp_path, monkeypatch):
        def boom(point):
            if point == "before_commit":  # after ``PRAGMA user_version = 2`` ran inside the transaction
                raise sqlite3.OperationalError("injected")
        monkeypatch.setattr(migrations, "_FAULT_HOOK", boom)
        with pytest.raises(MigrationError):
            run(v1, tmp_path)
        connection = sqlite3.connect(v1)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1  # rolled back with everything else
        finally:
            connection.close()

    @pytest.mark.parametrize("point", ["after_copy", "after_drop", "after_migration_2", "before_commit"])
    def test_a_failure_inside_the_transaction_leaves_the_file_byte_identical(self, v1, tmp_path, monkeypatch, point):
        before = fingerprint(v1)

        def boom(name):
            if name == point:
                raise RuntimeError("injected failure")
        monkeypatch.setattr(migrations, "_FAULT_HOOK", boom)
        with pytest.raises(RuntimeError):
            run(v1, tmp_path)
        assert fingerprint(v1) == before
        assert [e for e in dir_entries(str(tmp_path)) if e.endswith("-journal")] == []


class TestAfterMigration:
    def test_the_migrated_database_serves_both_creation_paths_and_legacy_rows_count_toward_the_limit(self, v1, tmp_path,
                                                                                                       monkeypatch, set_now):
        from datetime import datetime, timezone
        from app.db import repository
        run(v1, tmp_path)
        monkeypatch.setenv("APP_DB_PATH", v1)
        # at 2026-10-02T12:00Z the window is (2026-10-01T12:00Z, now]: C101's legacy ticket of 2026-10-02T00:00Z counts and the
        # one of 2026-10-01T00:00Z no longer does, so a limit of 2 leaves room for exactly one more ticket
        set_now(datetime(2026, 10, 2, 12, tzinfo=timezone.utc))
        first = repository.create_ticket_from_description("C101", "general", "fresh", "k1", rate_limit=(2, 86400))
        assert first["outcome"] == "created"
        again = repository.create_ticket_from_description("C101", "general", "second", "k2", rate_limit=(2, 86400))
        assert again["outcome"] == "rate_limited"
        legacy_path = repository.create_support_ticket("C102", "general", "d", "s")
        assert legacy_path["outcome"] == "created"


class TestRollbackProcedure:
    def test_restoring_the_verified_backup_returns_the_exact_pre_migration_database(self, v1, tmp_path):
        import shutil
        before = sqlite3.connect(v1)
        digests_before = migrations.content_digests(before)
        before.close()
        report = run(v1, tmp_path)
        restored = str(tmp_path / "restored.db")
        shutil.copyfile(report["backup"]["backup_path"], restored)  # the operator's rollback step: restore, then start the previous release
        connection = get_readonly_connection(restored, check_version=False)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert migrations.content_digests(connection) == digests_before
            assert migrations.schema_signature(connection)["tables"].keys() == migrations.schema_signature(
                get_readonly_connection(v1, check_version=False))["tables"].keys() - {
                    "ticket_events", "ticket_erasure_log", "ticket_write_intents"}
        finally:
            connection.close()
        with pytest.raises(SchemaIncompatibleError):  # the new code refuses the restored version-1 file as outdated
            get_connection(restored)


class TestParity:
    def test_a_fresh_and_a_migrated_database_are_structurally_identical(self, v1, tmp_path):
        run(v1, tmp_path)
        fresh = build_database(str(tmp_path / "fresh.db"), seed=False)
        migrated = get_connection(v1)
        other = get_connection(fresh)
        try:
            assert migrations.schema_signature(migrated) == migrations.schema_signature(other)
        finally:
            migrated.close()
            other.close()

    def test_the_new_indexes_are_used_by_their_queries(self, v1, tmp_path):
        run(v1, tmp_path)
        connection = get_connection(v1)
        try:
            plan = " ".join(r[3] for r in connection.execute(
                "EXPLAIN QUERY PLAN SELECT ticket_id FROM support_tickets WHERE customer_id = 'C101' AND issue_type = 'payment' "
                "AND issue_key = 'o:ORD1001' AND status NOT IN ('Resolved', 'Closed', 'Cancelled')"))
            assert "uq_active_ticket_per_issue" in plan
            plan = " ".join(r[3] for r in connection.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM support_tickets WHERE customer_id = 'C101' AND created_at > 'x'"))
            assert "idx_support_tickets_customer_created" in plan
        finally:
            connection.close()


class TestRefusals:
    def test_scratch_must_be_asserted(self, v1, tmp_path):
        before = fingerprint(v1)
        with pytest.raises(MigrationError, match="scratch"):
            migrations.migrate(v1, str(tmp_path / "b.db"), "ref")
        assert fingerprint(v1) == before

    def test_an_authorization_reference_is_required(self, v1, tmp_path):
        for bad in ("", "  ", None):
            with pytest.raises(MigrationError, match="authorization"):
                migrations.migrate(v1, str(tmp_path / "b.db"), bad, scratch=True)

    def test_an_already_current_database_is_an_error_never_a_second_rebuild(self, tmp_path):
        current = build_database(str(tmp_path / "cur.db"))
        before = fingerprint(current)
        with pytest.raises(MigrationError, match="already"):
            migrations.migrate(current, str(tmp_path / "b.db"), "ref", scratch=True)
        assert fingerprint(current) == before and not os.path.exists(str(tmp_path / "b.db"))

    def test_a_legacy_unversioned_database_is_not_migratable(self, tmp_path):
        path = str(tmp_path / "legacy.db")
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE customers (customer_id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        with pytest.raises(MigrationError, match="legacy"):
            migrations.migrate(path, str(tmp_path / "b.db"), "ref", scratch=True)

    def test_a_newer_database_is_refused(self, tmp_path):
        current = build_database(str(tmp_path / "cur.db"))
        connection = sqlite3.connect(current)
        connection.execute("PRAGMA user_version = 9")
        connection.commit()
        connection.close()
        with pytest.raises(MigrationError, match="newer"):
            migrations.migrate(current, str(tmp_path / "b.db"), "ref", scratch=True)

    def test_the_protected_legacy_database_is_refused_before_anything_is_opened(self, protected_sentinel, tmp_path):
        with pytest.raises(MigrationError):
            migrations.migrate(protected_sentinel.path, str(tmp_path / "b.db"), "ref", scratch=True)
        assert protected_sentinel.unchanged()

    def test_the_default_demo_database_path_is_refused_without_opening_it(self, tmp_path):
        with pytest.raises(MigrationError, match="refused"):
            migrations.migrate(APP_D2_DB_PATH, str(tmp_path / "b.db"), "ref", scratch=True)
        with pytest.raises(MigrationError, match="refused"):
            migrations.migrate(PROTECTED_DB_PATH, str(tmp_path / "b.db"), "ref", scratch=True)

    def test_backup_path_rules(self, v1, tmp_path):
        before = fingerprint(v1)
        existing = tmp_path / "existing.db"
        existing.write_bytes(b"x")
        with pytest.raises(MigrationError, match="already exists"):
            migrations.migrate(v1, str(existing), "ref", scratch=True)
        with pytest.raises(MigrationError, match="parent"):
            migrations.migrate(v1, str(tmp_path / "missing_dir" / "b.db"), "ref", scratch=True)
        with pytest.raises(MigrationError, match="differ"):
            migrations.migrate(v1, v1, "ref", scratch=True)
        with pytest.raises(MigrationError, match="required"):
            migrations.migrate(v1, "", "ref", scratch=True)
        assert fingerprint(v1) == before

    def test_a_database_in_use_elsewhere_is_refused(self, v1, tmp_path, monkeypatch):
        import app.db.connection as connection_module
        monkeypatch.setattr(connection_module, "CONNECT_TIMEOUT_SECONDS", 0.2)
        holder = sqlite3.connect(v1)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(MigrationError, match="in use"):
                run(v1, tmp_path)
        finally:
            holder.rollback()
            holder.close()
        assert get_migration_connection(v1).execute("PRAGMA user_version").fetchone()[0] == 1

    def test_a_failed_backup_verification_stops_before_any_change(self, v1, tmp_path, monkeypatch):
        monkeypatch.setattr(migrations, "_compare", lambda *a, **k: ["checksums differ (simulated)"])
        before = fingerprint(v1)
        with pytest.raises(MigrationError, match="backup verification failed"):
            run(v1, tmp_path)
        assert fingerprint(v1) == before

    def test_preflight_problems_stop_the_migration(self, tmp_path):
        path = build_v1_database(str(tmp_path / "bad.db"))
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO support_tickets (ticket_id, customer_id, order_id, issue_type, description, conversation_summary, "
            "status, created_at) VALUES ('TKT_" + "d" * 32 + "', 'C101', 'ORD:1', 'general', 'd', 's', 'Open', "
            "'2026-10-01T00:00:00Z')")
        connection.commit()
        connection.close()
        before = fingerprint(path)
        with pytest.raises(MigrationError, match="pre-flight"):
            migrations.migrate(path, str(tmp_path / "b.db"), "ref", scratch=True)
        assert fingerprint(path) == before


class TestCommandLine:
    def test_scratch_flag_is_mandatory_and_a_scratch_run_succeeds(self, v1, tmp_path, capsys):
        args = ["--db-path", v1, "--backup-path", str(tmp_path / "cli-backup.db"), "--authorization-reference", "ref"]
        assert migrate_cli.main(args) == 2
        assert "scratch" in json.loads(capsys.readouterr().out)["reason"]
        assert migrate_cli.main(args + ["--scratch"]) == 0
        assert json.loads(capsys.readouterr().out)["result"] == "migrated"

    def test_the_protected_paths_are_refused_with_exit_code_two(self, tmp_path, capsys):
        for path in (PROTECTED_DB_PATH, APP_D2_DB_PATH):
            code = migrate_cli.main(["--db-path", path, "--backup-path", str(tmp_path / "x.db"),
                                     "--authorization-reference", "ref", "--scratch"])
            assert code == 2
            capsys.readouterr()


def test_no_application_code_calls_the_migration():
    """Nothing migrates at import, startup, health check or first request: only the operator entry point uses it."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name in ("migrations.py", "migrate.py"):
            continue
        pattern = r"^\s*(from app\.db\.migrations import|import app\.db\.migrations|from app\.db import .*migrat)"
        if re.search(pattern, text, flags=re.M) or "migrations.migrate(" in text:
            offenders.append(str(path))
    assert offenders == []
