"""The session helpers must not depend on which backend the UI uses."""

import subprocess
import sys
from pathlib import Path

from app.ui.session import (
    create_chat,
    get_active_chat,
    is_chat_enabled,
    set_authenticated_customer_id,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_importing_session_does_not_import_a_backend():
    # A fresh interpreter: other tests in this process have already imported the backend.
    code = (
        "import sys\n"
        "import app.ui.session\n"
        "unwanted = [name for name in ('app.backend', 'app.ui.client', 'app.ui.fake_backend') "
        "if name in sys.modules]\n"
        "assert not unwanted, unwanted\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_helpers_store_any_backend_objects_without_inspecting_them():
    # Plain stand-ins: neither backend's classes are involved.
    state, pending, choice = object(), object(), object()
    session: dict = {}
    set_authenticated_customer_id(session, "C101")

    create_chat(session, state, "Welcome")
    chat = get_active_chat(session)
    chat["pending_view"] = pending
    chat["choice_view"] = choice

    assert is_chat_enabled(session)
    assert chat["conversation_state"] is state
    assert chat["pending_view"] is pending and chat["choice_view"] is choice
    assert chat["messages"] == [{"role": "assistant", "content": "Welcome"}]
