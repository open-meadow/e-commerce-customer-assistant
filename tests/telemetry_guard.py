"""Forces Chroma usage telemetry off for the test session (not a test module).

Chroma's native core reads ``ANONYMIZED_TELEMETRY`` when ``chromadb`` is first imported, so the override must be in
place before any test module imports it; setting it later (for example in a fixture) is too late, and with a
developer's ``ANONYMIZED_TELEMETRY=True`` telemetry (an outbound network call) would stay on for the whole session.
``tests/conftest.py`` imports this module first and calls ``apply()``; ``restore()`` (called from
``pytest_sessionfinish``) puts back the original value, or its absence. Monkeypatch semantics throughout.
Test-only: production telemetry configuration is not touched.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Iterator, Optional

import pytest

NAME = "ANONYMIZED_TELEMETRY"
# Evaluated when this module is first imported (by tests/conftest.py, before any test module).
CHROMA_IMPORTED_BEFORE_OVERRIDE = "chromadb" in sys.modules
_patch: Optional[pytest.MonkeyPatch] = None


def apply() -> None:
    global _patch
    if _patch is None:
        _patch = pytest.MonkeyPatch()
        _patch.setenv(NAME, "False")


def restore() -> None:
    global _patch
    if _patch is not None:
        _patch.undo()
        _patch = None


@contextlib.contextmanager
def chroma_telemetry_disabled() -> Iterator[None]:
    """The same override as a context manager: force False inside, restore the original afterwards."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(NAME, "False")
        yield
