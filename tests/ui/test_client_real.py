"""app/ui/client.py: backend selection and the real backend through the adapter.

All databases are temporary (``temp_db`` sets ``APP_DB_PATH``); no test opens
``data/app.db`` or ``data/app_d2.db``, and no provider is ever configured.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.ui import client

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REASON = "I want to return item IT101 from order ORD1001 because I changed my mind about it"


def run_python(code, **env):
    """Run ``code`` in a fresh interpreter with the given UI_BACKEND setting."""
    child_env = {k: v for k, v in os.environ.items() if k not in ("UI_BACKEND", "APP_DB_PATH")}
    child_env.update({k: v for k, v in env.items() if v is not None})
    return subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=child_env,
                          capture_output=True, text=True, timeout=120)


class TestBackendSelection:
    """The choice is made once, at import time, so each case uses a fresh interpreter."""

    def test_default_is_the_real_backend(self):
        result = run_python("import app.ui.client as c; print(c.BACKEND_MODE, c.ConversationState.__module__)")
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["real", "app.backend.types"]

    def test_fake_can_be_selected(self):
        result = run_python("import app.ui.client as c; print(c.BACKEND_MODE, c.ConversationState.__module__)",
                            UI_BACKEND="fake")
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["fake", "app.ui.fake_backend"]

    @pytest.mark.parametrize("value", ["REAL", " Fake "])
    def test_the_value_is_case_and_space_insensitive(self, value):
        result = run_python("import app.ui.client as c; print(c.BACKEND_MODE)", UI_BACKEND=value)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == value.strip().lower()

    @pytest.mark.parametrize("value", ["demo", "true", "real,fake"])
    def test_an_unknown_value_fails_loudly(self, value):
        result = run_python("import app.ui.client", UI_BACKEND=value)
        assert result.returncode != 0 and "UI_BACKEND must be one of real, fake" in result.stderr

    def test_the_fake_needs_no_database(self):
        code = ("import app.ui.client as c; s = c.backend_status(); "
                "print(s.ready, s.code, [o.customer_id for o in c.list_login_options()])")
        result = run_python(code, UI_BACKEND="fake", APP_DB_PATH=str(PROJECT_ROOT / "does" / "not" / "exist.db"))
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "True ready ['C101', 'C102']"

    def test_script_mode_import_works_with_only_the_ui_folder_on_the_path(self):
        # `streamlit run` puts only app/ui on sys.path; the project root is added by client.py.
        code = ("import sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
                f"sys.path.insert(0, r'{PROJECT_ROOT / 'app' / 'ui'}'); "
                "import client; print(client.BACKEND_MODE, client.start_session.__module__)")
        result = run_python(code)
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["real", "app.backend.api"]


@pytest.mark.skipif(client.BACKEND_MODE != "real", reason="UI_BACKEND selects the fake in this run")
class TestRealBackendThroughTheAdapter:
    pytestmark = pytest.mark.usefixtures("fresh_backend")

    def test_login_options_are_the_six_seeded_customers(self, temp_db):
        options = client.list_login_options()
        assert [o.customer_id for o in options] == [f"C10{n}" for n in range(1, 7)] or {
            o.customer_id for o in options} == {f"C10{n}" for n in range(1, 7)}
        assert next(o for o in options if o.customer_id == "C101").display_name == "Alice Walker"

    def test_a_session_starts_with_a_greeting(self, temp_db):
        result = client.start_session("C101")
        assert result.ok and result.state is not None and "Alice" in result.greeting
        assert result.error_code is None

    @pytest.mark.parametrize("customer_id", ["C999", "", "c101"])
    def test_an_unknown_customer_cannot_start_a_session(self, temp_db, customer_id):
        result = client.start_session(customer_id)
        assert not result.ok and result.state is None and result.error_code == "unknown_customer"

    def test_order_status_comes_from_the_database(self, temp_db):
        state = client.start_session("C101").state
        reply = client.handle_message(state, "Where is my order ORD1001?")
        assert reply.status == "ok" and "Delivered" in reply.text and reply.error_code is None
        assert isinstance(reply.state, client.ConversationState)

    def test_another_customers_order_is_not_found(self, temp_db):
        state = client.start_session("C101").state
        reply = client.handle_message(state, "status of ORD1003")  # C102's order
        assert "couldn't find" in reply.text.lower() and "Delivered" not in reply.text

    def test_the_return_flow_writes_one_row_only_after_confirmation(self, temp_db, raw):
        state = client.start_session("C101").state
        choice = client.handle_message(state, REASON)
        assert choice.status == "needs_choice" and isinstance(choice.choice, client.ChoiceView)
        pending = client.handle_message(
            state, choice=client.ChoiceSelection(choice.choice.prompt_id, "standard"))
        assert pending.status == "needs_confirmation" and isinstance(pending.pending, client.PendingView)
        assert REASON in pending.pending.summary and pending.pending.expires_at
        assert raw.execute("SELECT count(*) FROM return_requests").fetchone()[0] == 0
        done = client.handle_message(
            state, decision=client.ConfirmationDecision(pending.pending.action_id, True))
        assert "RET_" in done.text
        assert [tuple(r) for r in raw.execute(
            "SELECT customer_id, item_id, reason, reason_category FROM return_requests")] == [
            ("C101", "IT101", REASON, "standard")]

    def test_an_ended_session_is_reported_as_invalid(self, temp_db):
        from app.backend import end_session

        state = client.start_session("C101").state
        end_session(state)
        reply = client.handle_message(state, "hello")
        assert reply.status == "error" and reply.error_code == "invalid_session"

    def test_a_message_over_the_limit_is_an_error_reply_with_its_own_text(self, temp_db):
        state = client.start_session("C101").state
        reply = client.handle_message(state, "x" * 2001)
        assert reply.status == "error" and reply.error_code == "message_too_long"
        assert "2000" in reply.text and reply.request_id


@pytest.mark.skipif(client.BACKEND_MODE != "real", reason="UI_BACKEND selects the fake in this run")
class TestBackendStatus:
    pytestmark = pytest.mark.usefixtures("fresh_backend")

    def test_ready_for_an_initialized_database(self, temp_db):
        status = client.backend_status()
        assert status.ready and status.code == "ready" and status.message == ""

    def test_a_missing_database_names_the_seed_command(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "missing.db"))
        status = client.backend_status()
        assert not status.ready and status.code == "not_initialized"
        assert "python -m app.db.seed_data" in status.message and "--reference-time" in status.message
        assert not (tmp_path / "missing.db").exists()  # checking never creates it

    def test_an_empty_database_file_is_not_initialized(self, tmp_path, monkeypatch):
        path = tmp_path / "empty.db"
        path.write_bytes(b"")
        monkeypatch.setenv("APP_DB_PATH", str(path))
        assert client.backend_status().code == "not_initialized"

    def test_an_old_schema_is_incompatible(self, tmp_path, monkeypatch):
        path = tmp_path / "legacy.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE customers (customer_id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        monkeypatch.setenv("APP_DB_PATH", str(path))
        status = client.backend_status()
        assert not status.ready and status.code == "schema_incompatible"
        assert "different version" in status.message

    def test_the_protected_database_is_reported_as_unavailable(self, protected_sentinel, monkeypatch):
        monkeypatch.setenv("APP_DB_PATH", protected_sentinel.path)
        status = client.backend_status()
        assert not status.ready and status.code == "unavailable"
        assert protected_sentinel.unchanged()  # refused before anything was opened

    def test_an_unconfigured_provider_names_variables_but_no_values(self, temp_db, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        status = client.backend_status()
        assert not status.ready and status.code == "not_configured"
        assert "ANTHROPIC_API_KEY" in status.message and "LLM_MODEL" in status.message

    def test_a_configured_provider_is_never_called_by_the_check(self, temp_db, monkeypatch):
        import urllib.request

        def forbidden(*args, **kwargs):
            raise AssertionError("backend_status must not make a network request")

        monkeypatch.setattr(urllib.request, "urlopen", forbidden)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
        monkeypatch.setenv("LLM_MODEL", "fake-model")
        status = client.backend_status()
        assert status.ready and "fake-key-for-tests" not in status.message

    def test_a_configuration_error_clears_once_the_environment_is_fixed(self, temp_db, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        assert client.backend_status().code == "not_configured"
        assert client.backend_status().code == "not_configured"  # still failing closed
        monkeypatch.delenv("LLM_PROVIDER")
        assert client.backend_status().ready
