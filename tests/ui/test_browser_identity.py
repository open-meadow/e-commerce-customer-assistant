"""The Streamlit side of per-browser rate limiting (F1, F2).

A server-generated, random, opaque browser identifier lives in Streamlit's session state. It is passed to the in-process backend's
session start, and (with the UI service token) to the HTTP API's voice session start and close. It is anonymous abuse control, not
authentication, and it is never derived from the customer ID or a header.
"""

from pathlib import Path
from unittest import mock

import pytest
from streamlit.testing.v1 import AppTest

from app.ui import client, livekit_client
from app.ui.session import BROWSER_ID_KEY, clear_conversation, initialize_session, new_browser_id

APP_FILE = str(Path(__file__).resolve().parents[2] / "app" / "ui" / "streamlit_app.py")
UI_TOKEN = "ui-secret-token-for-tests-00000002"


class TestTheIdentifierInSessionState:
    def test_it_is_created_once_and_kept_across_re_initialisation(self):
        session = {}
        initialize_session(session)
        first = session[BROWSER_ID_KEY]
        initialize_session(session)
        assert session[BROWSER_ID_KEY] == first

    def test_it_survives_a_conversation_reset_within_the_same_browser(self):
        session = {}
        initialize_session(session)
        first = session[BROWSER_ID_KEY]
        clear_conversation(session)  # a new login or chat in the same browser keeps the same bucket
        initialize_session(session)
        assert session[BROWSER_ID_KEY] == first

    def test_two_browsers_get_different_random_identifiers(self):
        ids = set()
        for _ in range(500):
            session = {}
            initialize_session(session)
            ids.add(session[BROWSER_ID_KEY])
        assert len(ids) == 500

    def test_it_comes_from_the_secrets_generator_and_has_enough_entropy(self):
        with mock.patch("app.ui.session.secrets.token_urlsafe", return_value="x" * 32) as generator:
            assert new_browser_id() == "x" * 32
        generator.assert_called_once()
        assert len(new_browser_id()) >= 32  # 192 random bits

    def test_a_blank_or_non_string_value_is_replaced(self):
        for bad in ("", None, 12345):
            session = {BROWSER_ID_KEY: bad}
            initialize_session(session)
            assert isinstance(session[BROWSER_ID_KEY], str) and len(session[BROWSER_ID_KEY]) >= 32

    @pytest.mark.skipif(client.BACKEND_MODE != "real", reason="the canned demo backend has no rate limits")
    def test_the_backend_accepts_what_the_ui_generates(self):
        identifier = new_browser_id()
        assert client.browser_client_key(identifier) == "browser:" + identifier
        assert client.browser_client_key("") is None and client.browser_client_key("short") is None

    def test_the_identifier_never_contains_a_customer_id(self):
        session = {"authenticated_customer_id": "C101"}
        initialize_session(session)
        assert "C101" not in session[BROWSER_ID_KEY]


class FakeHttpClient:
    """Stands in for ``httpx.Client`` and records the options and requests the UI makes."""

    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        FakeHttpClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, path, json=None):
        self.requests.append((path, json))
        response = mock.MagicMock()
        response.raise_for_status = lambda: None
        response.json = lambda: {"session_id": "sid", "url": "wss://x.invalid", "token": "room-token"}
        return response


@pytest.fixture
def http(monkeypatch):
    FakeHttpClient.instances = []
    monkeypatch.setattr(livekit_client.httpx, "Client", FakeHttpClient)
    monkeypatch.setenv("SUPPORT_API_URL", "http://api.invalid:8000")
    return FakeHttpClient


class TestVoiceRequestsCarryTheIdentity:
    def test_start_and_close_send_the_browser_id_with_the_ui_token(self, http, monkeypatch):
        monkeypatch.setenv("SUPPORT_UI_TOKEN", UI_TOKEN)
        livekit_client.start_voice_session("C101", "browser-identifier-0001")
        livekit_client.end_voice_session("sid", "browser-identifier-0001")
        assert [i.kwargs["headers"] for i in http.instances] == [
            {"X-Service-Token": UI_TOKEN, "X-Browser-Id": "browser-identifier-0001"}] * 2
        assert [i.requests[0][0] for i in http.instances] == ["/api/sessions", "/api/sessions/close"]

    def test_without_the_ui_token_nothing_identifying_is_sent(self, http, monkeypatch):
        monkeypatch.delenv("SUPPORT_UI_TOKEN", raising=False)
        livekit_client.start_voice_session("C101", "browser-identifier-0001")
        assert http.instances[0].kwargs["headers"] == {}  # the API would ignore it anyway: it falls back to our address

    def test_without_a_browser_id_the_token_is_not_sent_either(self, http, monkeypatch):
        monkeypatch.setenv("SUPPORT_UI_TOKEN", UI_TOKEN)
        livekit_client.start_voice_session("C101")
        livekit_client.end_voice_session("sid", None)
        assert [i.kwargs["headers"] for i in http.instances] == [{}, {}]

    def test_the_customer_id_travels_only_in_the_body_never_as_an_identity_header(self, http, monkeypatch):
        monkeypatch.setenv("SUPPORT_UI_TOKEN", UI_TOKEN)
        livekit_client.start_voice_session("C101", "browser-identifier-0001")
        instance = http.instances[0]
        assert instance.requests == [("/api/sessions", {"customer_id": "C101"})]
        assert not any("C101" in str(v) for v in instance.kwargs["headers"].values())

    def test_the_service_token_is_not_part_of_what_the_page_receives(self, http, monkeypatch):
        monkeypatch.setenv("SUPPORT_UI_TOKEN", UI_TOKEN)
        session = livekit_client.start_voice_session("C101", "browser-identifier-0001")
        assert set(session) == {"session_id", "url", "token"} and UI_TOKEN not in str(session)


@pytest.mark.skipif(client.BACKEND_MODE != "real", reason="UI_BACKEND selects the fake in this run")
@pytest.mark.usefixtures("fresh_backend")
class TestTheRealAppUsesPerBrowserBuckets:
    def browser(self):
        return AppTest.from_file(APP_FILE, default_timeout=30).run()

    def login(self, at, customer):
        at.sidebar.selectbox(key="customer_selector").select(customer).run()
        return at

    def buckets(self):
        from app.backend import get_backend

        return dict(get_backend().registry._create_client._hits)

    def test_the_session_start_is_counted_against_this_browsers_key(self, temp_db):
        at = self.login(self.browser(), "C101")
        assert not at.exception
        identifier = at.session_state[BROWSER_ID_KEY]
        assert list(self.buckets()) == ["browser:" + identifier]

    def test_two_browsers_are_counted_separately(self, temp_db):
        a = self.login(self.browser(), "C101")
        b = self.login(self.browser(), "C102")
        keys = set(self.buckets())
        assert keys == {"browser:" + a.session_state[BROWSER_ID_KEY], "browser:" + b.session_state[BROWSER_ID_KEY]}
        assert a.session_state[BROWSER_ID_KEY] != b.session_state[BROWSER_ID_KEY]

    def test_one_browser_hitting_its_limit_does_not_block_another(self, temp_db, monkeypatch):
        monkeypatch.setenv("SUPPORT_SESSIONS_PER_CLIENT_PER_MINUTE", "2")
        a = self.browser()
        for customer in ("C101", "C102"):
            self.login(a, customer)
            assert not a.error
        self.login(a, "C103")  # the third start inside a minute is refused for this browser
        assert any("wait a moment" in e.value.lower() or "too many" in e.value.lower() for e in a.error)
        b = self.login(self.browser(), "C101")  # another browser has its own bucket
        assert not b.error and not b.exception

    def test_the_global_cap_still_applies_to_a_fresh_browser(self, temp_db, monkeypatch):
        monkeypatch.setenv("SUPPORT_MAX_SESSIONS", "1")
        first = self.login(self.browser(), "C101")
        assert not first.error
        second = self.login(self.browser(), "C102")
        assert any("busy" in e.value.lower() for e in second.error)
