"""H3: the HTTP boundary (FastAPI) and its LiveKit resources.

LiveKit is never contacted: the dispatch and room-delete coroutines are replaced by recorders. Covers request-body limits, the
per-client request and creation rates (a spoofed forwarded header must not change the client), the global cap, the module-level
session maps being cleaned when the backend expires a session, LiveKit dispatch failure and timeout cleanup, room release on
close, on the agent's disconnect notice and on shutdown, and concurrent creation.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.backend import Backend, Settings
from app.backend import api as backend_api
from app.backend import http_api
from app.llm.base import LLMResult
from backend_helpers import ScriptedLLM


@pytest.fixture(autouse=True)
def _database(temp_db):
    return temp_db


@pytest.fixture(autouse=True)
def livekit(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.invalid")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret-secret-secret-secret-secret")
    log = {"dispatched": [], "deleted": [], "fail_dispatch": None, "dispatch_delay": 0.0, "fail_delete": False}

    async def dispatch(*, url, api_key, api_secret, room_name):
        if log["dispatch_delay"]:
            await asyncio.sleep(log["dispatch_delay"])
        if log["fail_dispatch"] is not None:
            raise log["fail_dispatch"]
        log["dispatched"].append(room_name)

    async def delete(*, url, api_key, api_secret, room_name):
        log["deleted"].append(room_name)
        if log["fail_delete"]:
            raise RuntimeError("livekit down")

    monkeypatch.setattr(http_api, "_dispatch_agent", dispatch)
    monkeypatch.setattr(http_api, "_delete_room", delete)
    return log


def install(**kw):
    kw.setdefault("idle_timeout", timedelta(minutes=30))  # these tests advance the clock past 30 minutes
    backend = Backend(llm=ScriptedLLM(chat=lambda s, m, t: LLMResult(text="hello")), settings=Settings(**kw))
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
        assert livekit["deleted"] == [body["room_name"]]

    def test_the_voice_agent_turn_endpoint_still_works(self):
        install()
        c = client()
        body = start(c).json()
        r = c.post("/api/agent/turn", json={"room_name": body["room_name"], "message": "hello"})
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
            assert c.post("/api/agent/turn", json={"room_name": room, "message": "hello"}).status_code == 200

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


# --------------------------------------------------------------------------------------------- the HTTP maps
class TestSessionMapsAreCleaned:
    def test_a_backend_expiry_removes_the_http_entries_and_releases_the_room(self, bnow, livekit):
        backend = install()
        c = client()
        body = start(c).json()
        bnow(minutes=31)
        assert backend.registry.sweep() == 1
        assert maps_empty() and livekit["deleted"] == [body["room_name"]]
        assert say(c, body["session_id"]).status_code == 404

    def test_an_abandoned_session_is_cleaned_without_any_further_request(self, bnow, livekit):
        backend = install()
        for i in range(3):
            start(client(f"10.2.0.{i}"))
        assert len(http_api._sessions) == 3
        bnow(minutes=31)
        backend.registry.sweep()
        assert maps_empty() and len(livekit["deleted"]) == 3

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
        assert livekit["deleted"] == [body["room_name"]]

    def test_closing_twice_is_a_404_and_does_not_delete_the_room_twice(self, livekit):
        install()
        c = client()
        body = start(c).json()
        assert c.post("/api/sessions/close", json={"session_id": body["session_id"]}).status_code == 204
        assert c.post("/api/sessions/close", json={"session_id": body["session_id"]}).status_code == 404
        assert livekit["deleted"] == [body["room_name"]]


# --------------------------------------------------------------------------------------------- LiveKit lifecycle
class TestLiveKitLifecycle:
    def test_a_failed_dispatch_releases_everything_and_reports_502(self, livekit):
        backend = install()
        livekit["fail_dispatch"] = RuntimeError("livekit down")
        r = start(client())
        assert r.status_code == 502 and "livekit" not in r.text.lower()
        assert backend.registry.count() == 0 and maps_empty() and len(livekit["deleted"]) == 1

    def test_a_dispatch_that_hangs_times_out_with_504_and_releases_everything(self, livekit):
        backend = install(livekit_timeout_seconds=0.2)
        livekit["dispatch_delay"] = 5
        r = start(client())
        assert r.status_code == 504
        assert backend.registry.count() == 0 and maps_empty() and len(livekit["deleted"]) == 1

    def test_a_failing_room_delete_never_breaks_cleanup_or_the_response(self, livekit):
        backend = install()
        c = client()
        body = start(c).json()
        livekit["fail_delete"] = True
        assert c.post("/api/sessions/close", json={"session_id": body["session_id"]}).status_code == 204
        assert maps_empty() and backend.registry.count() == 0

    def test_a_hanging_room_delete_is_bounded_by_the_timeout(self, monkeypatch, livekit):
        install(livekit_timeout_seconds=0.2)

        async def slow_delete(*, url, api_key, api_secret, room_name):
            await asyncio.sleep(5)

        monkeypatch.setattr(http_api, "_delete_room", slow_delete)
        c = client()
        body = start(c).json()
        import time

        started = time.time()
        assert c.post("/api/sessions/close", json={"session_id": body["session_id"]}).status_code == 204
        assert time.time() - started < 3

    def test_the_agents_disconnect_notice_ends_the_session_and_releases_the_room(self, livekit):
        backend = install()
        c = client()
        body = start(c).json()
        r = c.post("/api/agent/close", json={"room_name": body["room_name"]})
        assert r.status_code == 204 and maps_empty() and backend.registry.count() == 0
        assert c.post("/api/agent/close", json={"room_name": body["room_name"]}).status_code == 404

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


class TestNoDuplicateOperationsThroughHttp:
    def test_a_retried_confirmation_over_http_creates_one_ticket(self, raw):
        from app.llm.base import ToolCall

        class TicketModel(ScriptedLLM):
            interprets_language = True

            def __init__(self):
                super().__init__(chat=self._chat)

            def _chat(self, system, messages, tools):
                if messages and messages[-1].get("role") == "tool":
                    return LLMResult(text="ok")
                return LLMResult(tool_calls=(ToolCall("start_support_ticket", {"issue_type": "general"}, "t1"),))

        backend_api.set_backend(Backend(llm=TicketModel(), settings=Settings()))
        http_api._reset_state()
        c = client()
        sid = start(c).json()["session_id"]
        card = say(c, sid, "my earbuds from ORD1001 stopped working after two days").json()
        action_id = card["pending"]["action_id"]
        decision = {"session_id": sid, "decision": {"action_id": action_id, "accepted": True}}
        first = c.post("/api/sessions/turn", json=decision)
        retry = c.post("/api/sessions/turn", json=decision)  # the client never saw the first response
        assert first.status_code == 200 and retry.status_code == 200
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1
