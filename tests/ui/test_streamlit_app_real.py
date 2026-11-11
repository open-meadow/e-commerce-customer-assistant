"""The Streamlit app driven with AppTest against the real backend and a temporary database."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.ui import client

APP_FILE = str(Path(__file__).resolve().parents[2] / "app" / "ui" / "streamlit_app.py")
REASON = "I want to return item IT101 from order ORD1001 because I changed my mind about it"

pytestmark = [
    pytest.mark.skipif(client.BACKEND_MODE != "real", reason="UI_BACKEND selects the fake in this run"),
    pytest.mark.usefixtures("fresh_backend"),
]


def start_app():
    return AppTest.from_file(APP_FILE, default_timeout=30).run()


def texts(at):
    return [t.value for t in at.text]


def login(at, customer_id="C101"):
    at.sidebar.selectbox(key="customer_selector").select(customer_id).run()
    return at


def say(at, message):
    at.chat_input[0].set_value(message).run()
    return at


def test_login_lists_the_six_seeded_customers(temp_db):
    at = start_app()
    assert not at.exception
    assert len(at.sidebar.selectbox(key="customer_selector").options) >= 6
    assert not at.error


def test_order_status_is_answered_from_the_database(temp_db):
    at = login(start_app())
    say(at, "Where is my order ORD1001?")
    assert not at.exception
    assert any("Delivered" in t for t in texts(at))


def test_another_customers_order_is_not_disclosed(temp_db):
    at = login(start_app())
    say(at, "status of ORD1003")
    assert not at.exception
    assert not any("Delivered" in t for t in texts(at))


def test_return_flow_saves_one_row_only_after_confirm(temp_db, raw):
    at = login(start_app())
    say(at, REASON)
    pick = next(b for b in at.button if b.label.startswith("Another reason"))
    pick.click().run()
    assert any("Expires at" in c.value for c in at.caption)
    assert raw.execute("SELECT count(*) FROM return_requests").fetchone()[0] == 0

    next(b for b in at.button if b.label == "Confirm").click().run()
    assert not at.exception
    assert any("has been submitted" in t for t in texts(at))
    assert [tuple(r) for r in raw.execute(
        "SELECT customer_id, item_id, reason, reason_category FROM return_requests")] == [
        ("C101", "IT101", REASON, "standard")]
    assert not [b for b in at.button if b.label == "Confirm"]


def test_cancel_writes_nothing(temp_db, raw):
    at = login(start_app())
    say(at, REASON)
    next(b for b in at.button if b.label.startswith("Another reason")).click().run()
    next(b for b in at.button if b.label == "Cancel").click().run()
    assert not at.exception
    assert raw.execute("SELECT count(*) FROM return_requests").fetchone()[0] == 0


def test_an_over_long_message_shows_a_banner_and_keeps_the_open_choice(temp_db):
    at = login(start_app())
    say(at, REASON)
    say(at, "x" * 2001)
    assert at.error and "2000" in at.error[0].value
    assert any(b.label.startswith("Another reason") for b in at.button)


def test_a_lost_session_starts_a_new_chat(temp_db):
    from app.backend import set_backend

    at = login(start_app())
    set_backend(None)  # a backend restart forgets every session
    say(at, "Where is my order ORD1001?")
    assert not at.exception
    assert any("new session" in str(m.value).lower() or "expired" in str(m.value).lower()
               for m in list(at.info) + list(at.warning) + list(at.caption) + list(at.error))


def test_gate_shows_the_seed_command_when_the_database_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "missing.db"))
    at = start_app()
    assert not at.exception
    assert at.error and "python -m app.db.seed_data" in at.error[0].value
    assert not at.sidebar.selectbox  # no login before the backend is ready
    assert not (tmp_path / "missing.db").exists()


def test_gate_shows_the_configuration_message_for_a_bad_provider(temp_db, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    at = start_app()
    assert not at.exception
    assert at.error and "ANTHROPIC_API_KEY" in at.error[0].value
    assert not at.sidebar.selectbox
