"""Deterministic seed data (docs/01 Section J) on the D2 schema."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import app.db.seed_data as seed_module
from app.db import connection as connection_module
from app.db.connection import (
    PROJECT_ROOT,
    get_connection,
    get_initialization_connection,
    resolve_db_path,
)
from app.db.schema import create_schema
from app.db.seed_data import seed_database
from conftest import build_database, fingerprint

UTC = timezone.utc
STR = "%Y-%m-%dT%H:%M:%SZ"

DELIVERED_OFFSET_HOURS = {  # order -> hours before the reference time
    "ORD1001": 72, "ORD1003": 240, "ORD1005": 36,
    "ORD1010": 288, "ORD1012": 360, "ORD1013": 60,
}
STATUS = {
    "ORD1001": "Delivered", "ORD1002": "Shipped", "ORD1003": "Delivered",
    "ORD1005": "Delivered", "ORD1006": "Cancelled", "ORD1010": "Returned",
    "ORD1012": "Delivered", "ORD1013": "Delivered",
}


def query(path, sql):
    connection = get_connection(path)
    try:
        return [dict(r) for r in connection.execute(sql)]
    finally:
        connection.close()


@pytest.fixture
def empty_schema():
    connection = get_initialization_connection(":memory:")
    create_schema(connection)
    yield connection
    connection.close()


class TestSeedFacts:
    def test_counts(self, temp_db):
        for table, count in [("customers", 6), ("orders", 15), ("order_items", 16),
                             ("return_requests", 0), ("support_tickets", 0)]:
            assert query(temp_db, f"SELECT count(*) AS n FROM {table}")[0]["n"] == count

    def test_required_authorization_scenario(self, temp_db):
        owners = {r["order_id"]: r for r in query(temp_db, "SELECT * FROM orders")}
        assert owners["ORD1001"]["customer_id"] == "C101" and owners["ORD1001"]["status"] == "Delivered"
        assert owners["ORD1002"]["customer_id"] == "C101"
        assert owners["ORD1003"]["customer_id"] == "C102"

    def test_statuses_and_delivery_offsets_at_the_default_baseline(self, temp_db):
        base = datetime(2026, 11, 7, tzinfo=UTC)
        orders = {r["order_id"]: r for r in query(temp_db, "SELECT * FROM orders")}
        for order_id, status in STATUS.items():
            assert orders[order_id]["status"] == status
        for order_id, hours in DELIVERED_OFFSET_HOURS.items():
            assert orders[order_id]["delivered_at"] == (base - timedelta(hours=hours)).strftime(STR)
        with_delivery = {o for o, r in orders.items() if r["delivered_at"] is not None}
        assert with_delivery == set(DELIVERED_OFFSET_HOURS)

    def test_every_status_appears(self, temp_db):
        seen = {r["status"] for r in query(temp_db, "SELECT DISTINCT status FROM orders")}
        assert seen == {"Processing", "Packed", "Shipped", "Out for Delivery",
                        "Delivered", "Cancelled", "Returned"}

    def test_all_timestamps_are_canonical(self, temp_db):
        for row in query(temp_db, "SELECT order_date, delivered_at FROM orders"):
            for value in row.values():
                if value is not None:
                    assert len(value) == 20 and value.endswith("Z")


class TestReferenceTime:
    def test_default_is_the_fixed_baseline(self, tmp_path):
        path = build_database(str(tmp_path / "a.db"))
        assert query(path, "SELECT delivered_at FROM orders WHERE order_id='ORD1001'")[0][
            "delivered_at"] == "2026-11-04T00:00:00Z"

    def test_every_timestamp_moves_with_an_explicit_reference_time(self, tmp_path):
        ref = datetime(2030, 1, 10, 6, 30, tzinfo=UTC)
        path = build_database(str(tmp_path / "b.db"), reference_time=ref)
        orders = {r["order_id"]: r for r in query(path, "SELECT * FROM orders")}
        assert orders["ORD1001"]["delivered_at"] == (ref - timedelta(hours=72)).strftime(STR)
        assert orders["ORD1001"]["order_date"] == (ref - timedelta(days=5)).strftime(STR)
        assert orders["ORD1005"]["delivered_at"] == (ref - timedelta(hours=36)).strftime(STR)

    def test_string_reference_time_with_offset(self, tmp_path):
        path = build_database(str(tmp_path / "c.db"), reference_time="2026-11-07T05:30:00+05:30")
        assert query(path, "SELECT delivered_at FROM orders WHERE order_id='ORD1001'")[0][
            "delivered_at"] == "2026-11-04T00:00:00Z"

    @pytest.mark.parametrize("bad", [datetime(2026, 11, 7), "2026-11-07T00:00:00", "nope"])
    def test_naive_or_unparseable_reference_time_rejected(self, empty_schema, bad):
        with pytest.raises(ValueError):
            seed_database(empty_schema, bad)

    def test_wrong_reference_type_rejected(self, empty_schema):
        with pytest.raises(TypeError):
            seed_database(empty_schema, 12345)


class TestSeedGuards:
    def test_refuses_a_non_empty_database_and_leaves_it_unchanged(self, empty_schema):
        seed_database(empty_schema)
        with pytest.raises(RuntimeError):
            seed_database(empty_schema)
        assert empty_schema.execute("SELECT count(*) FROM customers").fetchone()[0] == 6

    def test_unknown_product_fails_loudly_and_writes_nothing(self, empty_schema, monkeypatch):
        monkeypatch.setattr(seed_module, "_load_valid_product_ids", lambda: frozenset({"P1001"}))
        with pytest.raises(ValueError):
            seed_database(empty_schema)
        assert empty_schema.execute("SELECT count(*) FROM customers").fetchone()[0] == 0

    def test_requires_foreign_keys(self):
        import sqlite3
        raw = sqlite3.connect(":memory:")
        try:
            with pytest.raises(Exception):
                seed_database(raw)
        finally:
            raw.close()

    def test_products_json_is_not_modified(self):
        path = PROJECT_ROOT / "data" / "source" / "Products.json"
        before = path.read_bytes()
        seed_database_in_memory()
        assert path.read_bytes() == before


def seed_database_in_memory():
    connection = get_initialization_connection(":memory:")
    try:
        create_schema(connection)
        seed_database(connection)
    finally:
        connection.close()


class TestCommandLine:
    """The seed command, run in-process with ``main``.

    No test launches the CLI with the real protected path, and none
    touches ``data/app.db`` or the default ``data/app_d2.db``.
    """

    def test_explicit_path_initializes_a_new_database(self, tmp_path, capsys):
        target = str(tmp_path / "cli.db")
        assert seed_module.main(["--db-path", target]) == 0
        out = capsys.readouterr().out
        assert "6 customer" in out and target in out
        assert query(target, "SELECT count(*) AS n FROM orders")[0]["n"] == 15

    def test_without_path_uses_the_application_path(self, tmp_path, monkeypatch, capsys):
        target = str(tmp_path / "from_env.db")
        monkeypatch.setenv("APP_DB_PATH", target)
        assert seed_module.main([]) == 0
        # The application resolves the very same file.
        assert resolve_db_path() == os.path.normpath(target)
        assert query(target, "SELECT count(*) AS n FROM customers")[0]["n"] == 6

    def test_without_path_or_env_targets_the_default_without_touching_it(self, tmp_path, monkeypatch):
        target = tmp_path / "app_d2.db"
        monkeypatch.setattr(connection_module, "DEFAULT_DB_PATH", str(target))
        assert seed_module.main([]) == 0
        assert target.exists() and resolve_db_path() == os.path.normpath(str(target))

    def test_missing_parent_directory_is_refused(self, tmp_path, capsys):
        assert seed_module.main(["--db-path", str(tmp_path / "nope" / "x.db")]) == 1
        assert "parent directory" in capsys.readouterr().err
        assert not (tmp_path / "nope").exists()

    def test_existing_database_is_refused_and_unchanged(self, tmp_path):
        target = str(tmp_path / "cli.db")
        assert seed_module.main(["--db-path", target]) == 0
        before = fingerprint(target)
        assert seed_module.main(["--db-path", target]) == 1
        assert fingerprint(target) == before

    def test_protected_path_refused_before_opening(self, protected_sentinel, capsys):
        assert seed_module.main(["--db-path", protected_sentinel.path]) == 1
        assert "protected" in capsys.readouterr().err
        assert protected_sentinel.unchanged()

    def test_protected_path_refused_when_it_is_the_configured_path(self, protected_sentinel, monkeypatch):
        monkeypatch.setenv("APP_DB_PATH", protected_sentinel.path)
        assert seed_module.main([]) == 1
        assert protected_sentinel.unchanged()

    def test_module_entry_point_with_a_temporary_path(self, tmp_path):
        target = str(tmp_path / "entry.db")
        result = subprocess.run(
            [sys.executable, "-m", "app.db.seed_data", "--db-path", target],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "6 customer" in result.stdout
