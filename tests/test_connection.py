"""Connection factories, schema-version checks, health (docs/01 Section I)."""

from __future__ import annotations

import os
import sqlite3

import pytest

from app.db import connection as connection_module
from app.db import health
from app.db.connection import (
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    PROTECTED_DB_PATH,
    get_connection,
    get_initialization_connection,
    get_readonly_connection,
    resolve_db_path,
)
from app.db.errors import (
    ConnectionError_,
    DatabaseNotInitializedError,
    SchemaIncompatibleError,
)
from app.db.schema import create_schema
from conftest import build_database, dir_entries, fingerprint


def make_legacy(path: str) -> str:
    """A pre-D2 style file: tables exist, user_version is 0."""
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE customers (customer_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    return path


def set_version(path: str, version: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {version}")
    connection.commit()
    connection.close()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestResolvePath:
    def test_default_is_the_d2_file_under_project_root(self):
        assert resolve_db_path() == os.path.normpath(
            str(PROJECT_ROOT / "app" / "db" / "data" / "app_d2.db")
        )
        assert os.path.isabs(DEFAULT_DB_PATH)

    def test_default_is_not_the_protected_legacy_file(self):
        assert os.path.normcase(DEFAULT_DB_PATH) != os.path.normcase(PROTECTED_DB_PATH)
        assert os.path.basename(PROTECTED_DB_PATH) == "app.db"

    def test_default_applies_when_env_var_is_empty(self, monkeypatch):
        monkeypatch.setenv("APP_DB_PATH", "")
        assert resolve_db_path() == os.path.normpath(DEFAULT_DB_PATH)

    def test_env_var_overrides_the_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "other.db"))
        assert resolve_db_path() == os.path.normpath(str(tmp_path / "other.db"))

    def test_explicit_argument_beats_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "env.db"))
        assert resolve_db_path(str(tmp_path / "arg.db")) == os.path.normpath(str(tmp_path / "arg.db"))

    def test_missing_default_database_is_not_created_by_normal_startup(self, monkeypatch, tmp_path):
        target = tmp_path / "app_d2.db"
        monkeypatch.setattr(connection_module, "DEFAULT_DB_PATH", str(target))
        with pytest.raises(DatabaseNotInitializedError):
            get_connection()
        with pytest.raises(DatabaseNotInitializedError):
            get_readonly_connection()
        assert health.check_database_health().status == "not_initialized"
        assert not target.exists() and os.listdir(tmp_path) == []

    def test_absolute_env_path_used_as_given(self, monkeypatch, tmp_path):
        target = str(tmp_path / "x.db")
        monkeypatch.setenv("APP_DB_PATH", target)
        assert resolve_db_path() == os.path.normpath(target)

    def test_relative_env_path_resolves_from_project_root(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APP_DB_PATH", os.path.join("app", "db", "data", "other.db"))
        assert resolve_db_path() == os.path.normpath(
            str(PROJECT_ROOT / "app" / "db" / "data" / "other.db")
        )

    def test_explicit_argument_beats_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "env.db"))
        explicit = str(tmp_path / "arg.db")
        assert resolve_db_path(explicit) == os.path.normpath(explicit)

    def test_memory_path_is_unchanged(self):
        assert resolve_db_path(":memory:") == ":memory:"

    @pytest.mark.parametrize("bad", ["", "   ", "a\x00b"])
    def test_invalid_paths_rejected(self, bad):
        with pytest.raises(ConnectionError_):
            resolve_db_path(bad)


# ---------------------------------------------------------------------------
# get_connection
# ---------------------------------------------------------------------------

class TestNormalConnection:
    def test_missing_file_fails_and_creates_nothing(self, tmp_path):
        target = tmp_path / "newdir" / "missing.db"
        with pytest.raises(DatabaseNotInitializedError):
            get_connection(str(target))
        assert not target.exists()
        assert not target.parent.exists()

    def test_empty_file_is_not_initialized_and_unchanged(self, tmp_path):
        target = tmp_path / "empty.db"
        target.write_bytes(b"")
        with pytest.raises(DatabaseNotInitializedError):
            get_connection(str(target))
        assert target.stat().st_size == 0

    def test_legacy_database_rejected_unchanged(self, tmp_path):
        path = make_legacy(str(tmp_path / "legacy.db"))
        before = fingerprint(path)
        with pytest.raises(SchemaIncompatibleError) as info:
            get_connection(path)
        assert info.value.reason == "outdated"
        assert fingerprint(path) == before

    def test_future_version_rejected(self, tmp_path):
        path = build_database(str(tmp_path / "f.db"), seed=False)
        set_version(path, 2)
        with pytest.raises(SchemaIncompatibleError) as info:
            get_connection(path)
        assert info.value.reason == "future"

    def test_current_database_opens_with_required_settings(self, temp_db):
        connection = get_connection(temp_db)
        try:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            row = connection.execute("SELECT customer_id FROM customers").fetchone()
            assert row["customer_id"]  # sqlite3.Row access by name
        finally:
            connection.close()

    def test_version_is_checked_on_every_connection(self, temp_db):
        first = get_connection(temp_db)
        first.close()
        set_version(temp_db, 7)
        with pytest.raises(SchemaIncompatibleError):
            get_connection(temp_db)

    def test_uses_environment_path(self, temp_db):
        connection = get_connection()  # APP_DB_PATH set by the fixture
        connection.close()

    def test_memory_cannot_be_opened_by_name(self):
        with pytest.raises(ConnectionError_):
            get_connection(":memory:")

    def test_never_creates_a_database(self, tmp_path):
        target = tmp_path / "a.db"
        with pytest.raises(ConnectionError_):
            get_connection(str(target))
        assert dir_entries(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# get_readonly_connection
# ---------------------------------------------------------------------------

class TestReadonlyConnection:
    def test_reads_and_refuses_writes(self, temp_db):
        connection = get_readonly_connection(temp_db)
        try:
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM customers").fetchone()[0] == 6
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("DELETE FROM customers")
        finally:
            connection.close()

    def test_changes_nothing_on_disk(self, tmp_path, temp_db):
        before = (fingerprint(temp_db), dir_entries(str(tmp_path)))
        get_readonly_connection(temp_db).close()
        assert (fingerprint(temp_db), dir_entries(str(tmp_path))) == before

    def test_missing_file_creates_nothing(self, tmp_path):
        target = tmp_path / "sub" / "x.db"
        with pytest.raises(DatabaseNotInitializedError):
            get_readonly_connection(str(target))
        assert not target.parent.exists()

    def test_empty_file_is_not_initialized(self, tmp_path):
        target = tmp_path / "e.db"
        target.write_bytes(b"")
        with pytest.raises(DatabaseNotInitializedError):
            get_readonly_connection(str(target))
        assert target.stat().st_size == 0

    def test_wal_database_refused_without_creating_sidecars(self, tmp_path):
        path = str(tmp_path / "wal.db")
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE t (x)")
        connection.commit()
        connection.close()
        before = (fingerprint(path), dir_entries(str(tmp_path)))
        assert not os.path.exists(path + "-wal") and not os.path.exists(path + "-shm")
        with pytest.raises(ConnectionError_):
            get_readonly_connection(path)
        assert (fingerprint(path), dir_entries(str(tmp_path))) == before

    @pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
    def test_existing_sidecar_refused_and_untouched(self, temp_db, tmp_path, suffix):
        sidecar = temp_db + suffix
        with open(sidecar, "wb") as handle:
            handle.write(b"leftover")
        before = (fingerprint(temp_db), fingerprint(sidecar))
        with pytest.raises(ConnectionError_):
            get_readonly_connection(temp_db)
        assert (fingerprint(temp_db), fingerprint(sidecar)) == before

    def test_not_a_sqlite_file_refused(self, tmp_path):
        target = tmp_path / "x.db"
        target.write_bytes(b"this is not sqlite" * 10)
        with pytest.raises(ConnectionError_):
            get_readonly_connection(str(target))

    def test_version_enforced_unless_disabled(self, tmp_path):
        path = make_legacy(str(tmp_path / "l.db"))
        with pytest.raises(SchemaIncompatibleError):
            get_readonly_connection(path)
        connection = get_readonly_connection(path, check_version=False)
        connection.close()


# ---------------------------------------------------------------------------
# get_initialization_connection
# ---------------------------------------------------------------------------

class TestInitializationConnection:
    def test_explicit_path_required(self):
        with pytest.raises(ConnectionError_):
            get_initialization_connection(None)

    def test_protected_path_refused_before_opening(self, protected_sentinel):
        with pytest.raises(ConnectionError_):
            get_initialization_connection(protected_sentinel.path)
        assert protected_sentinel.unchanged()

    def test_protected_path_alias_refused_before_opening(self, protected_sentinel):
        folder = os.path.dirname(protected_sentinel.path)
        alias = os.path.join(folder, "..", os.path.basename(folder), os.path.basename(protected_sentinel.path))
        with pytest.raises(ConnectionError_):
            get_initialization_connection(alias)
        assert protected_sentinel.unchanged()

    def test_protected_path_refused_by_runtime_and_readonly_factories(self, protected_sentinel):
        with pytest.raises(ConnectionError_):
            get_connection(protected_sentinel.path)
        with pytest.raises(ConnectionError_):
            get_readonly_connection(protected_sentinel.path)
        assert health.check_database_health(protected_sentinel.path).status == "unavailable"
        assert protected_sentinel.unchanged()

    def test_missing_parent_directory_refused(self, tmp_path):
        with pytest.raises(ConnectionError_):
            get_initialization_connection(str(tmp_path / "nope" / "x.db"))
        assert not (tmp_path / "nope").exists()

    @pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
    def test_sidecar_refused(self, tmp_path, suffix):
        target = str(tmp_path / "x.db")
        with open(target + suffix, "wb") as handle:
            handle.write(b"x")
        with pytest.raises(ConnectionError_):
            get_initialization_connection(target)

    def test_not_a_sqlite_file_refused(self, tmp_path):
        target = tmp_path / "x.db"
        target.write_bytes(b"not sqlite" * 20)
        with pytest.raises(ConnectionError_):
            get_initialization_connection(str(target))

    def test_creates_a_new_file_with_foreign_keys_on(self, tmp_path):
        target = str(tmp_path / "new.db")
        connection = get_initialization_connection(target)
        try:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            connection.close()
        assert os.path.exists(target)

    def test_memory_allowed(self):
        connection = get_initialization_connection(":memory:")
        connection.close()

    def test_initialization_then_normal_connection(self, tmp_path):
        path = build_database(str(tmp_path / "ok.db"), seed=False)
        get_connection(path).close()

    def test_second_initialization_refused(self, tmp_path):
        path = build_database(str(tmp_path / "ok.db"), seed=False)
        connection = get_initialization_connection(path)
        try:
            with pytest.raises(ConnectionError_):
                create_schema(connection)
        finally:
            connection.close()

    def test_legacy_database_rejected_unchanged(self, tmp_path):
        path = make_legacy(str(tmp_path / "legacy.db"))
        before = fingerprint(path)
        connection = get_initialization_connection(path)
        try:
            with pytest.raises(SchemaIncompatibleError) as info:
                create_schema(connection)
            assert info.value.reason == "outdated"
        finally:
            connection.close()
        assert fingerprint(path) == before


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_missing_file_is_not_initialized_and_creates_nothing(self, tmp_path):
        target = tmp_path / "sub" / "x.db"
        assert health.check_database_health(str(target)).status == health.NOT_INITIALIZED
        assert not target.parent.exists()

    def test_empty_file_is_not_initialized(self, tmp_path):
        target = tmp_path / "e.db"
        target.write_bytes(b"")
        assert health.check_database_health(str(target)).status == health.NOT_INITIALIZED

    def test_current_database_is_ready_and_unchanged(self, temp_db, tmp_path):
        before = (fingerprint(temp_db), dir_entries(str(tmp_path)))
        assert health.check_database_health(temp_db) == health.HealthResult(health.READY)
        assert (fingerprint(temp_db), dir_entries(str(tmp_path))) == before

    def test_legacy_is_schema_incompatible_outdated(self, tmp_path):
        path = make_legacy(str(tmp_path / "l.db"))
        assert health.check_database_health(path) == health.HealthResult(
            health.SCHEMA_INCOMPATIBLE, "outdated"
        )

    def test_future_is_schema_incompatible_future(self, tmp_path):
        path = build_database(str(tmp_path / "f.db"), seed=False)
        set_version(path, 9)
        assert health.check_database_health(path) == health.HealthResult(
            health.SCHEMA_INCOMPATIBLE, "future"
        )

    def test_sidecar_or_wal_is_unavailable(self, temp_db):
        with open(temp_db + "-wal", "wb") as handle:
            handle.write(b"x")
        assert health.check_database_health(temp_db).status == health.UNAVAILABLE

    def test_garbage_file_is_unavailable(self, tmp_path):
        target = tmp_path / "g.db"
        target.write_bytes(b"zzzz" * 50)
        assert health.check_database_health(str(target)).status == health.UNAVAILABLE

    def test_uses_environment_path(self, temp_db):
        assert health.check_database_health().status == health.READY

    def test_never_raises_on_memory(self):
        assert health.check_database_health(":memory:").status == health.UNAVAILABLE
