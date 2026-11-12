from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.db.connection import (
    get_connection,
    get_initialization_connection,
    get_readonly_connection,
)
from scripts.init_demo_db import (
    build_parser,
    initialize_database,
    parse_reference_time,
    resolve_safe_db_path,
)

PROJECT_ROOT = _PROJECT_ROOT
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "init_demo_db.py"
REFERENCE_TIME = "2026-11-07T05:30:00+05:30"


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--db-path", "/tmp/demo.sqlite"],
        ["--reference-time", REFERENCE_TIME],
    ],
)
def test_required_cli_arguments(arguments):
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(arguments)

    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-07T00:00:00",
        "2026-11-07",
        "not-a-timestamp",
        "2026-02-30T00:00:00Z",
    ],
)
def test_reference_time_requires_valid_timezone_aware_iso8601(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_reference_time(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-07T00:00:00Z", datetime(2026, 11, 7, tzinfo=UTC)),
        (REFERENCE_TIME, datetime(2026, 11, 7, tzinfo=UTC)),
        (
            "2026-11-07T01:00:00.500000+01:00",
            datetime(2026, 11, 7, 0, 0, 0, 500000, tzinfo=UTC),
        ),
    ],
)
def test_reference_time_is_normalized_to_utc(value, expected):
    assert parse_reference_time(value) == expected


def test_help_includes_required_demo_window_warning(capsys):
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--help"])

    assert exc_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "ORD1005" in help_text
    assert "12 hours after the reference time" in help_text
    assert "about 4 to 5.5 days" in help_text


@pytest.mark.parametrize(
    "path",
    [
        "app/db/data/app.db",
        "app/db/data/./app.db",
        "app/db/data/nested/../app.db",
    ],
)
def test_real_database_path_and_aliases_are_rejected(path):
    with pytest.raises(argparse.ArgumentTypeError, match="real project database"):
        resolve_safe_db_path(path, project_root=PROJECT_ROOT)


def test_blank_database_path_is_rejected():
    with pytest.raises(argparse.ArgumentTypeError, match="must not be empty"):
        resolve_safe_db_path("  ", project_root=PROJECT_ROOT)


def test_initialize_database_creates_schema_and_seed_data(tmp_path):
    db_path = tmp_path / "demo.sqlite"
    reference_time = parse_reference_time(REFERENCE_TIME)

    initialize_database(db_path, reference_time)

    connection = get_connection(str(db_path))
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        customer_count = connection.execute(
            "SELECT COUNT(*) FROM customers"
        ).fetchone()[0]
        assert {
            "customers",
            "orders",
            "order_items",
            "return_requests",
            "support_tickets",
        } <= tables
        assert customer_count > 0
    finally:
        connection.close()


@pytest.mark.parametrize("object_type", ["table", "view"])
def test_initialize_database_refuses_existing_tables_or_views(tmp_path, object_type):
    db_path = tmp_path / f"existing-{object_type}.sqlite"
    connection = get_initialization_connection(str(db_path))
    try:
        if object_type == "table":
            connection.execute("CREATE TABLE existing_data (value TEXT)")
        else:
            connection.execute("CREATE VIEW existing_data AS SELECT 1 AS value")
    finally:
        connection.close()

    with pytest.raises(ValueError, match="database is not empty"):
        initialize_database(db_path, parse_reference_time(REFERENCE_TIME))

    connection = get_readonly_connection(str(db_path), check_version=False)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='existing_data'"
        ).fetchone() is not None
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='customers'"
        ).fetchone() is None
    finally:
        connection.close()


def test_cli_refuses_non_empty_database_without_changing_existing_data(tmp_path):
    db_path = tmp_path / "existing.sqlite"
    connection = get_initialization_connection(str(db_path))
    try:
        connection.execute("CREATE TABLE keep_me (value TEXT)")
        connection.execute("INSERT INTO keep_me VALUES ('preserve')")
        connection.commit()
    finally:
        connection.close()

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--db-path",
            str(db_path),
            "--reference-time",
            REFERENCE_TIME,
        ],
        cwd=PROJECT_ROOT / "scripts",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "database is not empty" in completed.stderr
    connection = get_readonly_connection(str(db_path), check_version=False)
    try:
        assert connection.execute("SELECT value FROM keep_me").fetchone()[0] == "preserve"
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='customers'"
        ).fetchone() is None
    finally:
        connection.close()


@pytest.mark.parametrize(
    "db_path",
    [
        str(PROJECT_ROOT / "app" / "db" / "data" / "app.db"),
        "app/db/data/./app.db",
        str(PROJECT_ROOT / "app" / "db" / "data" / "nested" / ".." / "app.db"),
    ],
)
def test_cli_refuses_real_database_path_before_connecting(db_path):
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--db-path",
            db_path,
            "--reference-time",
            REFERENCE_TIME,
        ],
        cwd=PROJECT_ROOT / "scripts",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "refusing to use the real project database" in completed.stderr


def test_non_empty_database_query_ignores_sqlite_internal_tables(tmp_path):
    db_path = tmp_path / "empty.sqlite"
    connection = get_initialization_connection(str(db_path))
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "LIMIT 1"
        ).fetchone() is None
    finally:
        connection.close()
