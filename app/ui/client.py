"""Adapter exposing the frontend API from the selected backend implementation.

The UI imports everything backend-related from this module only. Which
implementation answers is chosen once, at import time, by the ``UI_BACKEND``
environment variable:

* ``real`` (the default): ``app.backend``, the tested backend with the
  database, tools, retrieval and confirmation rules.
* ``fake``: ``app.ui.fake_backend``, a canned in-process demo that needs no
  database. Use it for offline UI work.

``backend_status()`` tells the UI whether the selected backend can serve
requests, with a customer-safe message when it cannot.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# ``streamlit run`` executes the app as a script, which puts only ``app/ui`` on
# ``sys.path``. Add the project root so ``app.backend`` imports in either launch
# style (same approach as ``scripts/init_demo_db.py``).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Settings (APP_DB_PATH, UI_BACKEND, LLM_*) may live in a ``.env`` file at the project
# root. Real environment variables win (override=False). Loaded here, at the UI entry
# point, and not in the library modules, so tests never read a developer's .env.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env", override=False)

BACKEND_ENV = "UI_BACKEND"
_BACKENDS = ("real", "fake")


def _selected_backend() -> str:
    value = (os.environ.get(BACKEND_ENV) or "real").strip().lower()
    if value not in _BACKENDS:
        raise ValueError(
            f"{BACKEND_ENV} must be one of {', '.join(_BACKENDS)}; got {value!r}"
        )
    return value


BACKEND_MODE = _selected_backend()

if BACKEND_MODE == "fake":
    if __package__:
        from .fake_backend import (
            ChatReply,
            ChoiceOption,
            ChoiceSelection,
            ChoiceView,
            Citation,
            ConfirmationDecision,
            ConversationState,
            LoginOption,
            PendingView,
            StartSessionResult,
            handle_message,
            list_login_options,
            start_session,
        )
    else:
        from fake_backend import (
            ChatReply,
            ChoiceOption,
            ChoiceSelection,
            ChoiceView,
            Citation,
            ConfirmationDecision,
            ConversationState,
            LoginOption,
            PendingView,
            StartSessionResult,
            handle_message,
            list_login_options,
            start_session,
        )
else:
    from app.backend import (
        ChatReply,
        ChoiceOption,
        ChoiceSelection,
        ChoiceView,
        Citation,
        ConfirmationDecision,
        ConversationState,
        LLMConfigError,
        LoginOption,
        PendingView,
        StartSessionResult,
        check_backend,
        get_backend,
        handle_message,
        list_login_options,
        start_session,
    )


@dataclass(frozen=True)
class BackendStatus:
    """Whether the selected backend can serve requests.

    ``code`` is ``ready``, ``not_configured``, ``not_initialized``,
    ``schema_incompatible`` or ``unavailable``. ``message`` is safe to show to a
    customer or operator: it never contains a secret, a path or a stack trace.
    """

    ready: bool
    code: str = "ready"
    message: str = ""


_STATUS_MESSAGES = {
    "not_initialized": (
        "The database has not been initialized yet. An operator must create it "
        "first: python -m app.db.seed_data --reference-time <current UTC time> "
        "(set APP_DB_PATH or pass --db-path to choose the file)."
    ),
    "schema_incompatible": (
        "The database was created by a different version of the application. "
        "An operator must create a fresh database."
    ),
    "unavailable": "The service is temporarily unavailable. Please try again shortly.",
}


def backend_status() -> BackendStatus:
    """Check the selected backend once, before showing the login screen.

    For the real backend this builds it (which fails with ``LLMConfigError`` when
    a requested language-model provider is misconfigured; the message names the
    missing variables, never their values) and then reads the database health.
    It does not call the language-model provider.
    """
    if BACKEND_MODE == "fake":
        return BackendStatus(ready=True)

    try:
        get_backend()
    except LLMConfigError as exc:
        return BackendStatus(
            ready=False,
            code="not_configured",
            message=f"The assistant is not configured: {exc}",
        )

    health = check_backend()
    if health.status == "ready":
        return BackendStatus(ready=True)
    return BackendStatus(
        ready=False,
        code=health.status,
        message=_STATUS_MESSAGES.get(health.status, _STATUS_MESSAGES["unavailable"]),
    )


__all__ = [
    "BACKEND_MODE",
    "BackendStatus",
    "ChatReply",
    "ChoiceOption",
    "ChoiceSelection",
    "ChoiceView",
    "Citation",
    "ConfirmationDecision",
    "ConversationState",
    "LoginOption",
    "PendingView",
    "StartSessionResult",
    "backend_status",
    "handle_message",
    "list_login_options",
    "start_session",
]
