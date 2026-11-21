"""H3: the HTTP boundary (FastAPI) and its LiveKit resources.

LiveKit is never contacted: the dispatch and room-delete coroutines are replaced by recorders. Covers request-body limits, the
per-client request and creation rates, per-browser buckets that only an authenticated front end can set (and that a forged
header or a forwarded header cannot), the agent endpoints' service token, the global cap shared by every browser, the
module-level session maps being cleaned when the backend expires a session, LiveKit dispatch failure and timeout cleanup,
bounded and concurrent room release (including shutdown), terminal replies, and idle timers that only accepted requests refresh.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app.backend import Backend, Settings
from app.backend import api as backend_api
from app.backend import http_api
from app.llm.base import LLMResult
from backend_helpers import ScriptedLLM

AGENT_TOKEN = "agent-secret-token-for-tests-0001"
UI_TOKEN = "ui-secret-token-for-tests-00000002"
AGENT = {"X-Service-Token": AGENT_TOKEN}


def browser(n):
    """A well-formed browser identifier (16 to 64 URL-safe characters)."""
    return f"browser-{n:03d}-" + "q" * 12


def as_browser(n, token=UI_TOKEN):
    return {"X-Service-Token": token, "X-Browser-Id": browser(n)}


@pytest.fixture(autouse=True)
def _database(temp_db):
    return temp_db


@pytest.fixture(autouse=True)
def livekit(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.invalid")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret-secret-secret-secret-secret")
    log = {"dispatched": [], "deleted": [], "fail_dispatch": None, "dispatch_delay": 0.0, "delete_delay": 0.0,
           "fail_delete": set(), "fail_all_deletes": False, "in_flight": 0, "max_in_flight": 0}

    async def dispatch(*, url, api_key, api_secret, room_name):
        if log["dispatch_delay"]:
            await asyncio.sleep(log["dispatch_delay"])
        if log["fail_dispatch"] is not None:
            raise log["fail_dispatch"]
        log["dispatched"].append(room_name)

    async def delete(*, url, api_key, api_secret, room_name):
        log["deleted"].append(room_name)
        log["in_flight"] += 1
        log["max_in_flight"] = max(log["max_in_flight"], log["in_flight"])
        try:
            if log["delete_delay"]:
                await asyncio.sleep(log["delete_delay"])
            if log["fail_all_deletes"] or room_name in log["fail_delete"]:
                raise RuntimeError("livekit down")
        finally:
            log["in_flight"] -= 1

    monkeypatch.setattr(http_api, "_dispatch_agent", dispatch)
    monkeypatch.setattr(http_api, "_delete_room", delete)
    return log


def install(llm=None, **kw):
    kw.setdefault("idle_timeout", timedelta(minutes=30))  # these tests advance the clock past 30 minutes
    kw.setdefault("agent_token", AGENT_TOKEN)
    kw.setdefault("ui_token", UI_TOKEN)
    backend = Backend(llm=llm or ScriptedLLM(chat=lambda s, m, t: LLMResult(text="hello")), settings=Settings(**kw))
    backend_api.set_backend(backend)
    http_api._reset_state()
    return backend


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    backend_api.set_backend(None)
    http_api._reset_state()


def client(peer="203.0.113.7"):
    return TestClient(http_api.app, client=(peer, 50000))


def start(c, customer="C101", **headers):
    return c.post("/api/sessions", json={"customer_id": customer}, headers=headers)


def say(c, session_id, message="hello", **headers):
    return c.post("/api/sessions/turn", json={"session_id": session_id, "message": message}, headers=headers)


def maps_empty():
    return not http_api._sessions and not http_api._session_by_room and not http_api._session_by_handle


def deleted(livekit):
    assert http_api._flush_cleanup(10), "room releases did not finish"
    return livekit["deleted"]


# --------------------------------------------------------------------------------------------- normal use
class TestNormalUse:
    def test_create_turn_and_close_still_work(self, livekit):
        backend = install()
        c = client()
        created = start(c)
        assert created.status_code == 200
        body = created.json()
        assert body["room_name"] in livekit["dispatched"] and body["greeting"].startswith("Hi ")
        assert say(c, body["session_id"]).status_code == 200
        assert c.post("/api/sessions/close", json={"session_id": body["session_id"]}).status_code == 204
        assert maps_empty() and backend.registry.count() == 0
        assert deleted(livekit) == [body["room_name"]]

    def test_the_voice_agent_turn_endpoint_still_works_with_its_token(self):
        install()
        c = client()
        body = start(c).json()
        r = c.post("/api/agent/turn", json={"room_name": body["room_name"], "message": "hello"}, headers=AGENT)
        assert r.status_code == 200 and r.json()["text"]

    def test_livekit_not_configured_creates_nothing(self, monkeypatch):
        backend = install()
        monkeypatch.delenv("LIVEKIT_URL")
        r = start(client())
        assert r.status_code == 503 and backend.registry.count() == 0 and maps_empty()

    def test_an_unknown_customer_creates_nothing(self):
        backend = install()
        r = start(client(), customer="C999")
        assert r.status_code == 404 and backend.registry.count() == 0 and maps_empty()

    def test_the_session_endpoints_need_no_service_token(self):
        install()
        c = client()
        sid = start(c).json()["session_id"]  # no X-Service-Token anywhere
        assert say(c, sid).status_code == 200
        assert c.post("/api/sessions/close", json={"session_id": sid}).status_code == 204


# --------------------------------------------------------------------------------------------- caps and rates
class TestLimitsAtTheBoundary:
    def test_the_global_session_cap_returns_503_and_creates_nothing_else(self, livekit):
        backend = install(max_active_sessions=2, session_creates_per_client_per_minute=100, session_creates_per_minute=100)
        assert [start(client(f"10.0.0.{i}")).status_code for i in range(2)] == [200, 200]
        refused = start(client("10.0.0.9"))
        assert refused.status_code == 503 and "retry-after" in refused.headers
        assert backend.registry.count() == 2 and len(livekit["dispatched"]) == 2 and len(http_api._sessions) == 2

    def test_the_creation_rate_returns_429_with_retry_after(self):
        install(session_creates_per_client_per_minute=2, session_creates_per_minute=100)
        c = client("198.51.100.4")
        assert [start(c).status_code for _ in range(2)] == [200, 200]
        refused = start(c)
        assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
        assert start(client("198.51.100.5")).status_code == 200  # a different peer is unaffected

    def test_a_forwarded_header_cannot_buy_a_new_identity(self):
        install(session_creates_per_client_per_minute=2, session_creates_per_minute=100, client_requests_per_minute=1000)
        c = client("198.51.100.4")
        statuses = [start(c, **{"X-Forwarded-For": f"9.9.9.{i}", "X-Real-IP": f"8.8.8.{i}"}).status_code for i in range(4)]
        assert statuses == [200, 200, 429, 429]

    def test_the_per_client_request_rate_returns_429(self):
        install(client_requests_per_minute=3, session_creates_per_client_per_minute=100)
        c = client("198.51.100.4")
        sid = start(c).json()["session_id"]  # request 1
        assert say(c, sid).status_code == 200  # 2
        assert say(c, sid).status_code == 200  # 3
        refused = say(c, sid)
        assert refused.status_code == 429 and "retry-after" in refused.headers

    def test_the_internal_agent_endpoint_is_not_limited_by_the_client_rate(self):
        install(client_requests_per_minute=2, session_creates_per_client_per_minute=100, session_requests_per_minute=1000,
                max_requests_per_session=1000)
        c = client("198.51.100.4")
        room = start(c).json()["room_name"]
        for _ in range(6):
            assert c.post("/api/agent/turn", json={"room_name": room, "message": "hello"}, headers=AGENT).status_code == 200

    def test_concurrent_creation_never_exceeds_the_cap(self, livekit):
        backend = install(max_active_sessions=5, session_creates_per_client_per_minute=1000, session_creates_per_minute=1000,
                          client_requests_per_minute=1000)

        def one(i):
            return start(client(f"10.1.0.{i}")).status_code

        with ThreadPoolExecutor(20) as pool:
            statuses = list(pool.map(one, range(20)))
        assert statuses.count(200) == 5 and statuses.count(503) == 15
        assert backend.registry.count() == 5 and len(http_api._sessions) == 5 and len(livekit["dispatched"]) == 5


class TestBodyAndInputLimits:
    def test_an_oversized_declared_body_is_rejected_before_it_is_read(self):
        install(max_body_bytes=2000)
        r = client().post("/api/sessions/turn", content=b"x" * 5000, headers={"content-type": "application/json"})
        assert r.status_code == 413

    def test_an_oversized_streamed_body_without_a_length_is_rejected(self):
        install(max_body_bytes=2000)

        def chunks():
            for _ in range(10):
                yield b"x" * 500

        r = client().post("/api/sessions/turn", content=chunks(), headers={"content-type": "application/json"})
        assert r.status_code == 413

    def test_a_normal_body_passes_and_a_long_message_gets_the_friendly_error(self):
        install(max_body_bytes=16384, max_message_chars=100)
        c = client()
        sid = start(c).json()["session_id"]
        r = say(c, sid, "x" * 150)
        assert r.status_code == 200 and r.json()["error_code"] == "message_too_long"

    def test_identifier_fields_have_a_length_limit(self):
        install()
        assert client().post("/api/sessions", json={"customer_id": "C" * 500}).status_code == 422
        assert client().post("/api/sessions/turn", json={"session_id": "s" * 500, "message": "hi"}).status_code == 422


# --------------------------------------------------------------------------------------------- F1/F2: per-browser buckets
class TestBrowserIdentity:
    """The browser identifier is anonymous abuse control honoured only from the authenticated Streamlit server."""

    def test_each_browser_has_its_own_creation_bucket_behind_the_same_peer(self):
        install(session_creates_per_client_per_minute=2, session_creates_per_minute=100, client_requests_per_minute=1000)
        c = client("10.9.9.9")  # the Streamlit server: one TCP peer for every browser
        assert [start(c, **as_browser(1)).status_code for _ in range(2)] == [200, 200]
        assert start(c, **as_browser(1)).status_code == 429  # browser 1 used its bucket
        assert [start(c, **as_browser(2)).status_code for _ in range(2)] == [200, 200]  # browser 2 has its own
        assert start(c, **as_browser(2)).status_code == 429

    def test_every_browser_shares_the_global_cap(self):
        backend = install(max_active_sessions=3, session_creates_per_client_per_minute=100, session_creates_per_minute=100,
                          client_requests_per_minute=1000)
        c = client("10.9.9.9")
        assert [start(c, **as_browser(n)).status_code for n in range(3)] == [200, 200, 200]
        refused = start(c, **as_browser(4))  # a fresh browser still cannot exceed the global cap
        assert refused.status_code == 503 and backend.registry.count() == 3

    def test_every_browser_shares_the_global_creation_rate(self):
        install(session_creates_per_client_per_minute=100, session_creates_per_minute=4, client_requests_per_minute=1000)
        c = client("10.9.9.9")
        assert [start(c, **as_browser(n)).status_code for n in range(4)] == [200] * 4
        assert start(c, **as_browser(99)).status_code == 429

    def test_an_identifier_from_an_unauthenticated_caller_is_ignored(self):
        install(session_creates_per_client_per_minute=2, session_creates_per_minute=100, client_requests_per_minute=1000)
        c = client("198.51.100.4")
        statuses = [start(c, **{"X-Browser-Id": browser(n)}).status_code for n in range(4)]  # no service token: rotation is useless
        assert statuses == [200, 200, 429, 429]

    def test_a_wrong_token_does_not_vouch_for_an_identifier(self):
        install(session_creates_per_client_per_minute=2, session_creates_per_minute=100, client_requests_per_minute=1000)
        c = client("198.51.100.4")
        statuses = [start(c, **as_browser(n, token="not-the-ui-token-0000000000")).status_code for n in range(4)]
        assert statuses == [200, 200, 429, 429]

    def test_the_agent_token_does_not_vouch_for_an_identifier(self):
        install(session_creates_per_client_per_minute=2, session_creates_per_minute=100, client_requests_per_minute=1000)
        c = client("198.51.100.4")
        statuses = [start(c, **as_browser(n, token=AGENT_TOKEN)).status_code for n in range(4)]  # the two secrets are separate
        assert statuses == [200, 200, 429, 429]

    @pytest.mark.parametrize("bad", ["short", "has spaces in it 1234567890", "../../etc/passwd/../../xx", "a" * 100])
    def test_a_malformed_identifier_is_ignored_even_with_the_right_token(self, bad):
        install(session_creates_per_client_per_minute=2, session_creates_per_minute=100, client_requests_per_minute=1000)
        c = client("198.51.100.4")
        results = [start(c, **{"X-Service-Token": UI_TOKEN, "X-Browser-Id": bad}).status_code for _ in range(3)]
        assert results == [200, 200, 429]  # the identifier was not honoured, so all three fell into the peer's bucket

    def test_the_customer_id_is_never_an_identity(self):
        install(session_creates_per_client_per_minute=2, session_creates_per_minute=100, client_requests_per_minute=1000)
        c = client("10.9.9.9")
        statuses = [start(c, customer=cid, **as_browser(1)).status_code for cid in ("C101", "C102", "C103")]
        assert statuses == [200, 200, 429]  # same browser, different customers: one bucket

    def test_requests_are_limited_per_browser_not_per_peer(self):
        install(client_requests_per_minute=3, session_creates_per_client_per_minute=100)
        c = client("10.9.9.9")
        sid1 = start(c, **as_browser(1)).json()["session_id"]  # 1
        assert say(c, sid1, **as_browser(1)).status_code == 200  # 2
        assert say(c, sid1, **as_browser(1)).status_code == 200  # 3
        assert say(c, sid1, **as_browser(1)).status_code == 429  # browser 1 is out
        sid2 = start(c, **as_browser(2)).json()["session_id"]  # browser 2 on the same peer is unaffected
        assert say(c, sid2, **as_browser(2)).status_code == 200

    def test_a_forwarded_header_is_still_ignored_next_to_an_identifier(self):
        install(session_creates_per_client_per_minute=2, session_creates_per_minute=100, client_requests_per_minute=1000)
        c = client("10.9.9.9")
        statuses = [start(c, **as_browser(1), **{"X-Forwarded-For": f"9.9.9.{i}"}).status_code for i in range(3)]
        assert statuses == [200, 200, 429]

    def test_a_missing_identifier_still_counts_against_the_global_limits(self):
        install(session_creates_per_client_per_minute=100, session_creates_per_minute=4, client_requests_per_minute=1000)
        statuses = [start(client(f"10.7.0.{i}")).status_code for i in range(6)]  # distinct peers, no identifiers at all
        assert statuses == [200, 200, 200, 200, 429, 429]

    def test_concurrent_creation_from_many_browsers_respects_cap_and_buckets(self):
        backend = install(max_active_sessions=6, session_creates_per_client_per_minute=2, session_creates_per_minute=1000,
                          client_requests_per_minute=1000)

        def one(i):
            return start(client("10.9.9.9"), **as_browser(i % 5)).status_code  # 5 browsers, 4 attempts each, one peer

        with ThreadPoolExecutor(20) as pool:
            statuses = list(pool.map(one, range(20)))
        assert statuses.count(200) <= 6 and backend.registry.count() == statuses.count(200)  # the cap holds
        per_browser = {}
        for key in backend.registry._create_client._hits:
            per_browser[key] = len(backend.registry._create_client._hits[key])
        assert per_browser and all(count <= 2 for count in per_browser.values())  # no browser beat its own bucket

    def test_one_browser_creating_concurrently_gets_exactly_its_bucket(self):
        install(max_active_sessions=100, session_creates_per_client_per_minute=3, session_creates_per_minute=1000,
                client_requests_per_minute=1000)

        def one(_):
            return start(client("10.9.9.9"), **as_browser(1)).status_code

        with ThreadPoolExecutor(12) as pool:
            statuses = list(pool.map(one, range(12)))
        assert statuses.count(200) == 3 and statuses.count(429) == 9


# --------------------------------------------------------------------------------------------- F3: agent authentication
class TestAgentAuthentication:
    def room(self):
        return start(client(), **{}).json()["room_name"]

    def test_missing_credentials_are_rejected_on_both_endpoints(self):
        install()
        room = self.room()
        c = client()
        assert c.post("/api/agent/turn", json={"room_name": room, "message": "hello"}).status_code == 401
        assert c.post("/api/agent/close", json={"room_name": room}).status_code == 401

    def test_invalid_credentials_are_rejected_and_change_nothing(self, livekit):
        backend = install()
        room = self.room()
        c = client()
        for token in ("wrong-token-0000000000000000", AGENT_TOKEN[:-1], AGENT_TOKEN + "x", UI_TOKEN, "x"):
            h = {"X-Service-Token": token}
            assert c.post("/api/agent/turn", json={"room_name": room, "message": "hello"}, headers=h).status_code == 401
            assert c.post("/api/agent/close", json={"room_name": room}, headers=h).status_code == 401
        assert backend.registry.count() == 1 and len(http_api._sessions) == 1 and livekit["deleted"] == []

    def test_a_valid_token_works_for_turn_and_close(self, livekit):
        backend = install()
        room = self.room()
        c = client()
        assert c.post("/api/agent/turn", json={"room_name": room, "message": "hello"}, headers=AGENT).status_code == 200
        assert c.post("/api/agent/close", json={"room_name": room}, headers=AGENT).status_code == 204
        assert backend.registry.count() == 0 and deleted(livekit) == [room]

    def test_without_a_configured_token_the_endpoints_fail_closed(self):
        install(agent_token="")
        room = self.room()
        c = client()
        for headers in ({}, AGENT, {"X-Service-Token": ""}):
            assert c.post("/api/agent/turn", json={"room_name": room, "message": "hello"}, headers=headers).status_code == 503
            assert c.post("/api/agent/close", json={"room_name": room}, headers=headers).status_code == 503

    def test_the_response_does_not_reveal_whether_the_token_or_the_room_was_wrong(self):
        install()
        c = client()
        a = c.post("/api/agent/turn", json={"room_name": "support-unknown", "message": "x"})
        b = c.post("/api/agent/turn", json={"room_name": "support-unknown", "message": "x"}, headers={"X-Service-Token": "zzz"})
        assert (a.status_code, a.json()) == (b.status_code, b.json()) == (401, {"detail": "Unauthorized."})

    def test_the_comparison_is_constant_time(self):
        from app.backend import limits

        install()
        c = client()
        with mock.patch.object(limits.hmac, "compare_digest", wraps=limits.hmac.compare_digest) as spy:
            c.post("/api/agent/turn", json={"room_name": "support-x", "message": "x"}, headers={"X-Service-Token": "guess"})
        assert spy.call_count == 1
        assert all(isinstance(arg, bytes) for arg in spy.call_args.args)

    def test_a_secret_never_appears_in_a_response_or_a_log(self, caplog):
        caplog.set_level(logging.DEBUG)
        install()
        room = self.room()
        c = client()
        guess = "attacker-guess-0123456789abcdef"
        r1 = c.post("/api/agent/turn", json={"room_name": room, "message": "x"}, headers={"X-Service-Token": guess})
        r2 = c.post("/api/agent/close", json={"room_name": room}, headers={"X-Service-Token": guess})
        ok = c.post("/api/agent/turn", json={"room_name": room, "message": "x"}, headers=AGENT)
        for text in (r1.text, r2.text, ok.text, caplog.text):
            assert guess not in text and AGENT_TOKEN not in text and UI_TOKEN not in text

    def test_repeated_failures_from_one_peer_are_throttled(self):
        install()
        c = client("198.51.100.77")
        codes = [c.post("/api/agent/close", json={"room_name": "support-x"}, headers={"X-Service-Token": "bad"}).status_code
                 for _ in range(35)]
        assert codes[:30] == [401] * 30 and set(codes[30:]) == {429}
        assert client("198.51.100.78").post("/api/agent/close", json={"room_name": "support-x"}).status_code == 401  # others unaffected

    def test_room_ownership_and_session_validation_remain_intact(self):
        install()
        c = client()
        assert c.post("/api/agent/turn", json={"room_name": "support-nope", "message": "x"}, headers=AGENT).status_code == 404
        assert c.post("/api/agent/close", json={"room_name": "support-nope"}, headers=AGENT).status_code == 404
        room = self.room()
        assert c.post("/api/agent/close", json={"room_name": room}, headers=AGENT).status_code == 204
        assert c.post("/api/agent/turn", json={"room_name": room, "message": "x"}, headers=AGENT).status_code == 404  # ended
        assert c.post("/api/agent/close", json={"room_name": room}, headers=AGENT).status_code == 404  # idempotent

    def test_a_valid_token_does_not_make_a_guessed_room_name_work(self):
        install()
        room_a = self.room()
        c = client()
        guess = room_a[:-1] + "g"  # a character a hex room name never ends with, so it can never equal the real one
        assert c.post("/api/agent/turn", json={"room_name": guess, "message": "x"}, headers=AGENT).status_code == 404

    def test_a_missing_token_is_warned_about_at_startup(self, caplog):
        caplog.set_level(logging.WARNING)
        install(agent_token="")
        with client():
            pass
        assert "SUPPORT_AGENT_TOKEN is not set" in caplog.text


# --------------------------------------------------------------------------------------------- the HTTP maps
class TestSessionMapsAreCleaned:
    def test_a_backend_expiry_removes_the_http_entries_and_releases_the_room(self, bnow, livekit):
        backend = install()
        c = client()
        body = start(c).json()
        bnow(minutes=31)
        assert backend.registry.sweep() == 1
        assert maps_empty() and deleted(livekit) == [body["room_name"]]
        assert say(c, body["session_id"]).status_code == 404

    def test_an_abandoned_session_is_cleaned_without_any_further_request(self, bnow, livekit):
        backend = install()
        for i in range(3):
            start(client(f"10.2.0.{i}"))
        assert len(http_api._sessions) == 3
        bnow(minutes=31)
        backend.registry.sweep()
        assert maps_empty() and len(deleted(livekit)) == 3

    def test_a_session_with_a_running_turn_keeps_its_http_entry(self, bnow, livekit):
        backend = install()
        body = start(client()).json()
        session = backend.registry.get(http_api._sessions[body["session_id"]].state._handle)
        assert session.turn_lock.acquire(blocking=False)
        try:
            bnow(hours=9)
            assert backend.registry.sweep() == 0
            assert body["session_id"] in http_api._sessions and livekit["deleted"] == []
        finally:
            session.turn_lock.release()

    def test_an_invalid_session_reply_cleans_up_and_releases_the_room(self, bnow, livekit):
        backend = install()
        c = client()
        body = start(c).json()
        backend.registry.end(http_api._sessions[body["session_id"]].state._handle)  # ended behind the API's back
        assert maps_empty()  # the listener already removed it
        assert deleted(livekit) == [body["room_name"]]

    def test_closing_twice_is_a_404_and_does_not_delete_the_room_twice(self, livekit):
        install()
        c = client()
        body = start(c).json()
        assert c.post("/api/sessions/close", json={"session_id": body["session_id"]}).status_code == 204
        assert c.post("/api/sessions/close", json={"session_id": body["session_id"]}).status_code == 404
        assert deleted(livekit) == [body["room_name"]]

    def test_a_failing_listener_never_leaves_the_maps_inconsistent(self, bnow, livekit):
        backend = install()
        backend.registry.add_drop_listener(lambda session, reason: 1 / 0)  # registered after the http listener
        body = start(client()).json()
        bnow(minutes=31)
        backend.registry.sweep()
        assert maps_empty() and deleted(livekit) == [body["room_name"]]

    def test_a_failure_while_scheduling_the_release_still_leaves_the_maps_clean(self, bnow, monkeypatch):
        backend = install()
        start(client())

        def boom(room):
            raise RuntimeError("queue broken")

        monkeypatch.setattr(http_api._releaser, "schedule", boom)
        bnow(minutes=31)
        backend.registry.sweep()  # the registry isolates the listener failure
        assert maps_empty() and backend.registry.count() == 0


# --------------------------------------------------------------------------------------------- LiveKit lifecycle
class TestLiveKitLifecycle:
    def test_a_failed_dispatch_releases_everything_and_reports_502(self, livekit):
        backend = install()
        livekit["fail_dispatch"] = RuntimeError("livekit down")
        r = start(client())
        assert r.status_code == 502 and "livekit" not in r.text.lower()
        assert backend.registry.count() == 0 and maps_empty() and len(deleted(livekit)) == 1

    def test_a_dispatch_that_hangs_times_out_with_504_and_releases_everything(self, livekit):
        backend = install(livekit_timeout_seconds=0.2)
        livekit["dispatch_delay"] = 5
        r = start(client())
        assert r.status_code == 504
        assert backend.registry.count() == 0 and maps_empty() and len(deleted(livekit)) == 1

    def test_a_failing_room_delete_never_breaks_cleanup_or_the_response(self, livekit):
        backend = install()
        c = client()
        body = start(c).json()
        livekit["fail_all_deletes"] = True
        assert c.post("/api/sessions/close", json={"session_id": body["session_id"]}).status_code == 204
        assert maps_empty() and backend.registry.count() == 0
        assert deleted(livekit) == [body["room_name"]]  # it was attempted, failed, and was logged

    def test_the_agents_disconnect_notice_ends_the_session_and_releases_the_room(self, livekit):
        backend = install()
        c = client()
        body = start(c).json()
        r = c.post("/api/agent/close", json={"room_name": body["room_name"]}, headers=AGENT)
        assert r.status_code == 204 and maps_empty() and backend.registry.count() == 0
        assert deleted(livekit) == [body["room_name"]]
        assert c.post("/api/agent/close", json={"room_name": body["room_name"]}, headers=AGENT).status_code == 404
        assert deleted(livekit) == [body["room_name"]]  # still released once

    def test_shutdown_ends_every_session_and_releases_every_room(self, livekit):
        backend = install(session_creates_per_client_per_minute=100)
        with client() as c:  # entering the context runs the application's lifespan
            rooms = [start(c).json()["room_name"] for _ in range(3)]
            assert backend.registry.count() == 3
        assert backend.registry.count() == 0 and maps_empty()
        assert sorted(livekit["deleted"]) == sorted(rooms)

    def test_the_sweeper_runs_while_the_application_runs_and_stops_with_it(self):
        backend = install(sweep_interval_seconds=0.05)
        with client():
            assert backend.registry._sweeper is not None
        assert backend.registry._sweeper is None


# --------------------------------------------------------------------------------------------- F4 / F9: bounded cleanup
class TestBoundedRoomCleanup:
    def test_closing_returns_before_a_slow_release_finishes(self, livekit):
        install(livekit_timeout_seconds=5, session_creates_per_client_per_minute=100)
        c = client()
        body = start(c).json()
        livekit["delete_delay"] = 1.0
        started = time.time()
        assert c.post("/api/sessions/close", json={"session_id": body["session_id"]}).status_code == 204
        assert time.time() - started < 0.5  # the request thread is not held by LiveKit
        assert deleted(livekit) == [body["room_name"]]  # and the release still happens

    def test_the_agent_close_and_the_invalid_session_path_do_not_block_either(self, livekit):
        install(livekit_timeout_seconds=5)
        c = client()
        room = start(c).json()["room_name"]
        livekit["delete_delay"] = 1.0
        started = time.time()
        assert c.post("/api/agent/close", json={"room_name": room}, headers=AGENT).status_code == 204
        assert time.time() - started < 0.5
        deleted(livekit)

    def test_shutdown_time_is_bounded_however_many_rooms_hang(self, livekit):
        backend = install(livekit_timeout_seconds=0.3, shutdown_timeout_seconds=0.6, max_active_sessions=100,
                          session_creates_per_client_per_minute=100, session_creates_per_minute=1000,
                          client_requests_per_minute=1000)
        livekit["delete_delay"] = 30  # every release hangs until its own timeout
        with client() as c:
            for i in range(40):
                assert start(c, **as_browser(i)).status_code == 200
            started = time.time()
        elapsed = time.time() - started
        assert elapsed < 6.0, elapsed  # deadline 0.6 s plus one per-room timeout, not 40 x 0.3 s = 12 s (generous for a loaded machine)
        assert livekit["max_in_flight"] <= http_api._RoomReleaser.WORKERS  # concurrency is itself bounded
        assert backend.registry.count() == 0 and maps_empty()

    def test_rooms_are_released_concurrently(self, livekit):
        install(livekit_timeout_seconds=5, shutdown_timeout_seconds=10, max_active_sessions=100,
                session_creates_per_client_per_minute=100, session_creates_per_minute=1000, client_requests_per_minute=1000)
        livekit["delete_delay"] = 0.2
        with client() as c:
            for i in range(24):
                assert start(c, **as_browser(i)).status_code == 200
            started = time.time()
        elapsed = time.time() - started
        assert len(livekit["deleted"]) == 24
        assert livekit["max_in_flight"] >= 4, livekit["max_in_flight"]  # releases overlapped: they were not sequential
        assert elapsed < 4.0, elapsed  # a loose bound under load; sequential would be at least 24 x 0.2 = 4.8 s

    def test_one_failing_room_does_not_stop_the_others(self, livekit):
        install(session_creates_per_client_per_minute=100)
        c = client()
        bodies = [start(c, **as_browser(i)).json() for i in range(6)]
        rooms = [b["room_name"] for b in bodies]
        livekit["fail_delete"] = {rooms[2]}
        for room in rooms:
            http_api._releaser.schedule(room)
        assert http_api._flush_cleanup(10)
        assert sorted(livekit["deleted"]) == sorted(rooms)  # the failing one was tried and the other five were released

    def test_a_room_that_is_already_queued_is_not_queued_twice(self, livekit):
        install()
        livekit["delete_delay"] = 0.5
        for _ in range(5):
            http_api._releaser.schedule("support-same-room")
        assert http_api._flush_cleanup(10)
        assert livekit["deleted"] == ["support-same-room"]

    def test_shutdown_twice_is_harmless(self, livekit):
        backend = install()
        with client() as c:
            body = start(c).json()
        assert deleted(livekit) == [body["room_name"]]
        http_api._shutdown(backend)  # a second shutdown finds nothing to do
        assert backend.registry.count() == 0 and deleted(livekit) == [body["room_name"]]

    def test_an_expired_session_purged_inside_a_create_does_not_slow_that_create(self, bnow, livekit):
        install(livekit_timeout_seconds=5, session_creates_per_client_per_minute=100)
        c = client()
        for i in range(3):
            start(c, **as_browser(i))
        livekit["delete_delay"] = 1.0
        bnow(minutes=31)  # all three are idle: the next create purges them inline
        started = time.time()
        assert start(c, **as_browser(9)).status_code == 200
        assert time.time() - started < 0.8
        deleted(livekit)

    def test_active_operations_are_safe_while_other_sessions_are_cleaned(self, bnow, livekit):
        backend = install()
        c = client()
        keep = start(c, **as_browser(1)).json()
        drop = start(c, **as_browser(2)).json()
        session = backend.registry.get(http_api._sessions[keep["session_id"]].state._handle)
        assert session.turn_lock.acquire(blocking=False)
        try:
            bnow(minutes=31)
            assert backend.registry.sweep() == 1  # only the idle one without a running turn
        finally:
            session.turn_lock.release()
        assert keep["session_id"] in http_api._sessions and drop["session_id"] not in http_api._sessions
        assert deleted(livekit) == [drop["room_name"]]


# --------------------------------------------------------------------------------------------- F5: terminal replies over HTTP
class TestTerminalReplies:
    def test_a_hard_limit_is_flagged_terminal_and_a_transient_one_is_not(self):
        install(max_requests_per_session=2, session_requests_per_minute=1000, client_requests_per_minute=1000)
        c = client()
        sid = start(c).json()["session_id"]
        first, second, third = (say(c, sid).json() for _ in range(3))
        assert first["terminal"] is False and second["terminal"] is False
        assert third["error_code"] == "rate_limited" and third["terminal"] is True  # the session's request limit

    def test_the_per_minute_rate_is_transient(self):
        install(session_requests_per_minute=1, client_requests_per_minute=1000)
        c = client()
        sid = start(c).json()["session_id"]
        assert say(c, sid).json()["terminal"] is False
        slow_down = say(c, sid).json()
        assert slow_down["error_code"] == "rate_limited" and slow_down["terminal"] is False

    def test_a_vanished_session_is_terminal_on_the_agent_endpoint(self):
        backend = install()
        c = client()
        body = start(c).json()
        session = http_api._sessions[body["session_id"]].state
        # the registry forgets the session without the HTTP maps noticing (as if its listener had not run)
        backend.registry._sessions.pop(session._handle)
        r = c.post("/api/agent/turn", json={"room_name": body["room_name"], "message": "hi"}, headers=AGENT).json()
        assert r["error_code"] == "invalid_session" and r["terminal"] is True


# --------------------------------------------------------------------------------------------- F8: only accepted requests keep a session alive
class TestRefusedRequestsDoNotRefreshTheIdleTimer:
    def test_a_rate_limited_http_request_does_not_extend_the_session(self, bnow):
        backend = install(client_requests_per_minute=1)
        c = client()
        sid = start(c).json()["session_id"]  # consumes the client's one request this minute
        bnow(minutes=29)
        assert c.get("/api/login-options").status_code == 200  # uses the client's request for this minute
        assert say(c, sid).status_code == 429  # refused before it reaches the session
        bnow(minutes=2)  # 31 minutes since the last accepted activity
        assert backend.registry.sweep() == 1

    def test_an_unauthorized_agent_request_does_not_extend_the_session(self, bnow):
        backend = install()
        c = client()
        room = start(c).json()["room_name"]
        bnow(minutes=29)
        assert c.post("/api/agent/turn", json={"room_name": room, "message": "hi"}).status_code == 401
        assert c.post("/api/agent/turn", json={"room_name": room, "message": "hi"},
                      headers={"X-Service-Token": "wrong-token-0000000000000000"}).status_code == 401
        bnow(minutes=2)
        assert backend.registry.sweep() == 1

    def test_an_accepted_request_does_extend_the_session(self, bnow):
        backend = install(client_requests_per_minute=1000)
        c = client()
        sid = start(c).json()["session_id"]
        bnow(minutes=29)
        assert say(c, sid).status_code == 200
        bnow(minutes=2)  # 31 minutes since creation, but only 2 since the accepted request
        assert backend.registry.sweep() == 0 and say(c, sid).json()["error_code"] is None


# --------------------------------------------------------------------------------------------- F7: startup rejects bad configuration
class TestStartupRejectsInvalidConfiguration:
    @pytest.mark.parametrize("variable,value", [
        ("SUPPORT_MAX_SESSIONS", "0"), ("SUPPORT_MAX_BODY_BYTES", "999999999"), ("SUPPORT_REQUESTS_PER_CLIENT_PER_MINUTE", "many"),
        ("SUPPORT_SHUTDOWN_TIMEOUT_SECONDS", "0.1"), ("SUPPORT_AGENT_TOKEN", "short"), ("SUPPORT_UI_TOKEN", "has a space 1234567890")])
    def test_the_api_does_not_start_with_an_invalid_value(self, monkeypatch, variable, value):
        from app.backend.config import ConfigError

        backend_api.set_backend(None)  # force the lifespan to build the backend from the environment
        http_api._reset_state()
        monkeypatch.setenv(variable, value)
        with pytest.raises(ConfigError) as info:
            with client():
                pass
        assert variable in str(info.value)
        if variable.endswith("TOKEN"):
            assert value not in str(info.value)  # a secret is never echoed

    def test_the_api_starts_with_valid_overrides(self, monkeypatch):
        backend_api.set_backend(None)
        http_api._reset_state()
        monkeypatch.setenv("SUPPORT_MAX_SESSIONS", "7")
        monkeypatch.setenv("SUPPORT_AGENT_TOKEN", AGENT_TOKEN)
        with client():
            assert backend_api.get_backend().settings.max_active_sessions == 7
