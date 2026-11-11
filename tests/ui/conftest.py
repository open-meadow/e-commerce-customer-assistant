"""Shared fixtures for the UI tests that run against the real backend."""

import pytest

_PROVIDER_VARIABLES = ("LLM_PROVIDER", "LLM_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                       "LLM_TIMEOUT_SECONDS")


@pytest.fixture
def fresh_backend(monkeypatch):
    """A new process-wide backend for the test, with the offline assistant selected.

    The backend is a singleton built on first use, so tests reset it before and
    after, and clear every provider variable so no test can reach a real provider.
    """
    from app.backend import set_backend

    for name in _PROVIDER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    set_backend(None)
    yield
    set_backend(None)
