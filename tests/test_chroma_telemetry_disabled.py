"""Chroma usage telemetry is an outbound network call; the test session must force it off.

``tests/conftest.py`` does this at import time through ``tests/telemetry_guard.py`` (before any test module imports
``chromadb``, whose native core reads the setting once), overriding a developer's ``ANONYMIZED_TELEMETRY=True`` with
monkeypatch semantics and restoring the original value in ``pytest_sessionfinish``. Production telemetry configuration
is not touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from chromadb.config import Settings

import telemetry_guard
from telemetry_guard import chroma_telemetry_disabled

ROOT = Path(__file__).resolve().parents[1]
NAME = "ANONYMIZED_TELEMETRY"


def test_the_helper_overrides_a_true_value_and_restores_it(monkeypatch):
    monkeypatch.setenv(NAME, "True")
    assert Settings().anonymized_telemetry is True  # the developer's setting really would enable telemetry
    with chroma_telemetry_disabled():
        assert os.environ[NAME] == "False"
        assert Settings().anonymized_telemetry is False  # what a Chroma client would read
    assert os.environ[NAME] == "True"  # restored afterwards


def test_the_helper_restores_an_unset_variable_as_unset(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    with chroma_telemetry_disabled():
        assert os.environ[NAME] == "False"
    assert NAME not in os.environ


def test_the_helper_restores_the_value_even_when_the_body_fails(monkeypatch):
    monkeypatch.setenv(NAME, "True")
    try:
        with chroma_telemetry_disabled():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert os.environ[NAME] == "True"


def test_the_test_session_has_telemetry_off():
    assert os.environ[NAME] == "False"
    assert Settings().anonymized_telemetry is False


def test_the_override_was_applied_before_chroma_was_imported():
    """Chroma's native core reads the setting at import time; a later override would be too late."""
    assert telemetry_guard.CHROMA_IMPORTED_BEFORE_OVERRIDE is False


def test_a_session_started_with_true_in_the_environment_still_runs_with_telemetry_off():
    """End to end: a fresh pytest session whose environment says True must see False inside its tests."""
    env = {**os.environ, NAME: "True", "PYTHON_DOTENV_DISABLED": "1"}
    probe = "tests/test_chroma_telemetry_disabled.py::test_the_test_session_has_telemetry_off"
    result = subprocess.run([sys.executable, "-m", "pytest", probe, "-q", "-p", "no:cacheprovider"],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, (result.stdout[-600:], result.stderr[-600:])
    assert "1 passed" in result.stdout
