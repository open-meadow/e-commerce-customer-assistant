"""Shared fixtures for the database tests.

Rules every test follows:

* Databases are built in pytest's ``tmp_path`` (or ``:memory:``) with the
  real ``get_initialization_connection``, ``create_schema`` and
  ``seed_database``. Neither the protected legacy ``app/db/data/app.db`` nor the
  default ``app/db/data/app_d2.db`` is ever used or created.
* No test may launch a subprocess or call a function with the real
  protected path. Protected-path behavior is tested with the
  ``protected_sentinel`` fixture (a temporary stand-in file and a mocked
  low-level opener).
* An autouse guard patches ``sqlite3.connect`` and fails the test if any
  call resolves to the protected path. It compares paths only and never
  opens, reads or stats the real file.
* Time is controlled by monkeypatching ``app.db.clock.utc_now`` through
  the ``set_now`` fixture. The default instant is the seed baseline.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import pytest

# Chroma telemetry must be forced off BEFORE any test module imports ``chromadb`` (its native core reads the
# setting once, at import), so this conftest applies the override immediately. See telemetry_guard.py.
import telemetry_guard

telemetry_guard.apply()


def pytest_sessionfinish(session, exitstatus):
    telemetry_guard.restore()  # put back the developer's original value (or its absence)


from app.db import clock
from app.db import connection as connection_module
from app.db.connection import (
    DEFAULT_DB_PATH,
    PROTECTED_DB_PATH,
    get_connection,
    get_initialization_connection,
)
from app.db.schema import create_schema
from app.db.seed_data import seed_database

UTC = timezone.utc
T0 = datetime(2026, 11, 7, tzinfo=UTC)  # the seed baseline


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


@pytest.fixture(autouse=True)
def _never_open_real_database(monkeypatch):
    """Fail if anything tries to open the legacy ``app/db/data/app.db`` or the
    default ``app/db/data/app_d2.db`` (paths are compared only, never opened)."""
    protected = {_norm(PROTECTED_DB_PATH), _norm(DEFAULT_DB_PATH)}
    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if isinstance(database, (str, bytes, os.PathLike)):
            text = os.fsdecode(database)
            if text.startswith("file:"):
                text = unquote(urlparse(text).path)
                if re.match(r"^/[A-Za-z]:", text):
                    text = text[1:]
            if text != ":memory:" and _norm(text) in protected:
                raise AssertionError("a test tried to open a protected or default database")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    monkeypatch.delenv("APP_DB_PATH", raising=False)


@pytest.fixture
def protected_sentinel(tmp_path, monkeypatch):
    """A temporary stand-in for the protected legacy database.

    ``PROTECTED_DB_PATH`` is pointed at a sentinel file in ``tmp_path`` and
    the low-level opener ``connection._open`` is replaced by a recorder
    that fails the test if it is ever reached. Returns an object with
    ``path``, ``opened`` (list of attempted URIs) and ``unchanged()``.
    """
    path = tmp_path / "protected_legacy.db"
    path.write_bytes(b"SENTINEL-NOT-A-DATABASE")
    before = fingerprint(str(path))
    monkeypatch.setattr(connection_module, "PROTECTED_DB_PATH", str(path))
    opened: list = []

    def recorder(uri, *args, **kwargs):
        opened.append(uri)
        raise AssertionError("the low-level opener was reached for a protected path")

    monkeypatch.setattr(connection_module, "_open", recorder)

    class Sentinel:
        pass

    sentinel = Sentinel()
    sentinel.path = str(path)
    sentinel.opened = opened
    sentinel.unchanged = lambda: fingerprint(str(path)) == before and not opened
    return sentinel


@pytest.fixture
def bnow(monkeypatch):
    """Control the backend clock (sessions, choice and confirmation expiry).

    Returns ``advance(**timedelta_kwargs)``; the clock starts at the seed
    baseline.
    """
    from datetime import timedelta
    from app.backend import clock as backend_clock

    state = {"now": T0}
    monkeypatch.setattr(backend_clock, "now", lambda: state["now"])

    def advance(**kwargs) -> None:
        state["now"] += timedelta(**kwargs)

    return advance


@pytest.fixture
def set_now(monkeypatch):
    """Return ``set_now(dt)``; the clock starts at the seed baseline."""
    state = {"now": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])

    def setter(value: datetime) -> None:
        state["now"] = value

    return setter


def build_database(path: str, *, seed: bool = True, reference_time=None) -> str:
    """Create a fresh D2 database file (optionally seeded) at *path*."""
    connection = get_initialization_connection(path)
    try:
        create_schema(connection)
        if seed:
            seed_database(connection, reference_time)
    finally:
        connection.close()
    return path


@pytest.fixture
def temp_db(tmp_path, monkeypatch, set_now):
    """A seeded temp database. ``APP_DB_PATH`` points at it; clock is T0."""
    path = build_database(str(tmp_path / "test.db"))
    monkeypatch.setenv("APP_DB_PATH", path)
    return path


@pytest.fixture
def raw(temp_db):
    """A normal connection to the temp database for direct SQL in tests."""
    connection = get_connection(temp_db)
    yield connection
    connection.close()


@pytest.fixture
def corrupt():
    """Return ``corrupt(path, sql, params)``: run SQL with CHECKs ignored.

    Simulates persisted data that violates an invariant (for example a
    malformed timestamp) so integrity-error paths can be exercised.
    """

    def run(path: str, sql: str, params: tuple = ()) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    return run


def fingerprint(path: str) -> tuple[int, str]:
    """Size and SHA-256 of a file, for 'unchanged' assertions."""
    with open(path, "rb") as handle:
        data = handle.read()
    return len(data), hashlib.sha256(data).hexdigest()


def dir_entries(path: str) -> list[str]:
    return sorted(os.listdir(path))


# ---------------------------------------------------------------------------
# Retrieval: the real Chroma code path with an injected, local embedding function
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _vector_store_dir(tmp_path_factory):
    """One temporary Chroma storage folder for the whole run, removed at the end."""
    import shutil

    folder = tmp_path_factory.mktemp("vector-store")
    try:
        yield folder
    finally:
        try:  # release Chroma's file handles first, or Windows cannot delete the folder
            from app.rag import release_default_knowledge_base
            from chromadb.api.shared_system_client import SharedSystemClient

            release_default_knowledge_base()
            SharedSystemClient.clear_system_cache()
        except Exception:  # noqa: BLE001 - cleanup must never mask a test failure
            pass
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(autouse=True)
def _local_test_embeddings(monkeypatch, _vector_store_dir):
    """Every test uses the SAME Chroma retrieval code as production, with a deterministic
    local embedding function injected: no API key, no network, no model download.

    The embedding (``app.rag.local_embeddings``) is deterministic and lexical: BM25-weighted term
    vectors over the fitted corpus, so cosine ranking follows the BM25 score. It is NOT a semantic
    embedding and says nothing about the quality of OpenAI embeddings; tests that use it prove the
    wiring (indexing, cut-off, citations, failure handling), not OpenAI retrieval quality.
    Tests of the production configuration use the ``production_embeddings`` fixture.
    """
    from app.rag import configure_embeddings
    from app.rag import local_embeddings

    monkeypatch.setenv("VECTOR_STORE_DIR", str(_vector_store_dir))
    configure_embeddings(local_embeddings.factory, max_distance=local_embeddings.DEFAULT_MAX_DISTANCE,
                         label=local_embeddings.LABEL)
    try:
        yield
    finally:
        configure_embeddings(None)  # restore production configuration and drop the cached index


@pytest.fixture
def production_embeddings(monkeypatch, tmp_path):
    """The real production configuration: OpenAI embeddings, which need OPENAI_API_KEY.

    The key is removed, so initialization must fail honestly; no request is ever made.
    """
    from app.rag import configure_embeddings

    configure_embeddings(None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("VECTOR_STORE_DIR", str(tmp_path / "production-store"))
    return None
