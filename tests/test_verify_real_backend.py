"""Tests for scripts/verify_real_backend.py: it passes on the real backend, fails on a wrong one, never
leaks state, and its tripwires work. Offline, temporary databases only."""

from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
import urllib.request

import pytest

from app.db import connection as db_connection
from app.llm.base import LLMResult
from scripts import verify_real_backend as vrb
from tests.backend_helpers import ScriptedLLM


def test_the_script_passes_on_the_real_backend(capsys):
    assert vrb.main() == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out and "[FAIL]" not in out
    assert "support-eval-" not in out  # no temporary paths in the output
    assert "api_key" not in out.lower()


def test_each_run_uses_a_fresh_database_and_leaves_nothing_behind(monkeypatch, capsys):
    folders: list[str] = []

    class Recording(tempfile.TemporaryDirectory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            folders.append(self.name)

    monkeypatch.setattr(vrb.real_eval.tempfile, "TemporaryDirectory", Recording)
    monkeypatch.setenv(db_connection.ENV_DB_PATH, "sentinel-before")
    assert vrb.main() == 0 and vrb.main() == 0
    assert len(folders) == 2 and folders[0] != folders[1]
    assert not any(os.path.exists(f) for f in folders)
    assert os.environ[db_connection.ENV_DB_PATH] == "sentinel-before"


def test_a_wrong_backend_makes_the_script_exit_nonzero(monkeypatch, capsys):
    # A model that never proposes tools: the business flows cannot happen, so checks must fail.
    monkeypatch.setattr(vrb, "RuleBasedLLM", lambda: ScriptedLLM(chat=lambda s, m, t: LLMResult(text="Sure!")))
    assert vrb.main() != 0
    out = capsys.readouterr().out
    assert "[FAIL]" in out or "could not complete" in out
    assert "RESULT: PASS" not in out


def test_a_failure_during_the_run_restores_state_and_exits_with_2(monkeypatch, capsys):
    folders: list[str] = []

    class Recording(tempfile.TemporaryDirectory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            folders.append(self.name)

    def boom(*args, **kwargs):
        raise ValueError("path-like detail that must not be printed: " + os.getcwd())

    monkeypatch.setattr(vrb.real_eval.tempfile, "TemporaryDirectory", Recording)
    monkeypatch.setattr(vrb, "run_checks", boom)
    monkeypatch.setenv(db_connection.ENV_DB_PATH, "sentinel-before")
    original = (sqlite3.connect, socket.socket.connect, urllib.request.urlopen)
    assert vrb.main() == 2
    out = capsys.readouterr().out
    assert "ValueError" in out and os.getcwd() not in out  # class only, never the message
    assert os.environ[db_connection.ENV_DB_PATH] == "sentinel-before"
    assert folders and not os.path.exists(folders[0])
    assert (sqlite3.connect, socket.socket.connect, urllib.request.urlopen) == original  # tripwires removed


def test_a_failing_check_is_reported_with_expected_and_actual(capsys):
    report = vrb.Report()
    assert report.check("works", True) is True
    assert report.check("broken", False, "one row", "two rows") is False
    out = capsys.readouterr().out
    assert "[PASS] works" in out and "[FAIL] broken" in out and "one row" in out and "two rows" in out
    assert report.failed == 1


def test_tripwires_refuse_and_count_network_use(monkeypatch):
    tripwires = vrb.Tripwires()
    with tripwires.active():
        with pytest.raises(AssertionError):
            urllib.request.urlopen("http://example.invalid/")
        with pytest.raises(AssertionError):
            socket.create_connection(("example.invalid", 80))
        with pytest.raises(AssertionError):
            socket.getaddrinfo("example.invalid", 80)
    assert tripwires.network_attempts == 3


@pytest.mark.parametrize("path", [db_connection.PROTECTED_DB_PATH, db_connection.DEFAULT_DB_PATH])
def test_tripwires_refuse_the_protected_and_default_databases(path):
    tripwires = vrb.Tripwires()
    with tripwires.active():
        with pytest.raises(AssertionError, match="protected or default"):
            sqlite3.connect(path)
    assert tripwires.sqlite_opened == set()


def test_tripwires_record_a_temporary_database(tmp_path):
    tripwires = vrb.Tripwires()
    target = tmp_path / "scratch.db"
    with tripwires.active():
        sqlite3.connect(str(target)).close()
        sqlite3.connect(":memory:").close()
    assert tripwires.sqlite_opened == {vrb._norm(str(target))}
