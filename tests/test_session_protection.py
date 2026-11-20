"""H3: session and resource protection at the registry and backend level.

Covers: the global session cap, creation rate limits, atomic creation under concurrent requests, idle and absolute expiry,
automatic cleanup, an active turn never losing its session, pending actions during cleanup, per-session turn and rate limits,
the provider-call budget, provider timeout and no retry, cleanup after failures, valid sessions staying functional, and no
duplicate ticket creation on repeated or concurrent confirmation.

Temporary databases, scripted providers and a controllable clock only; no network, no paid API.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from unittest import mock

import pytest

from app.backend import Backend, Settings
from app.backend.limits import BudgetedLLM, ProviderBudget, SessionLimitError, SlidingWindowLimiter
from app.backend.sessions import SessionRegistry
from app.llm.base import LLMError, LLMResult, ToolCall
from backend_helpers import Client, ScriptedLLM


@pytest.fixture(autouse=True)
def _database(temp_db):
    return temp_db


def tickets(raw):
    return [dict(r) for r in raw.execute("SELECT * FROM support_tickets").fetchall()]


def settings(**kw):
    return Settings(**{"idle_timeout": timedelta(minutes=30), **kw})  # these tests advance the clock past 30 minutes


def registry(**kw):
    return SessionRegistry(settings(**kw))


class TicketModel(ScriptedLLM):
    interprets_language = True

    def __init__(self):
        super().__init__(chat=self._chat)

    def _chat(self, system, messages, tools):
        if messages and messages[-1].get("role") == "tool":
            return LLMResult(text="ok")
        return LLMResult(tool_calls=(ToolCall("start_support_ticket", {"issue_type": "general"}, "t1"),))


def ticket_card(c):
    r = c.say("my earbuds from ORD1001 stopped working after two days")
    assert r.pending is not None and r.pending.kind == "support_ticket", r.text
    return r


# --------------------------------------------------------------------------------------------- limiter primitive
class TestSlidingWindowLimiter:
    def test_allows_up_to_the_limit_then_refuses_until_the_window_passes(self, bnow):
        limiter = SlidingWindowLimiter(3, 60)
        assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
        assert limiter.allow("b") is True  # keys are independent
        assert limiter.retry_after("a") > 0
        bnow(seconds=61)
        assert limiter.allow("a") is True

    def test_the_key_table_stays_bounded(self, bnow):
        limiter = SlidingWindowLimiter(1, 60, max_keys=50)
        for i in range(500):
            limiter.allow(f"k{i}")
        assert limiter.key_count() <= 50

    def test_a_refused_attempt_does_not_extend_the_window(self, bnow):
        limiter = SlidingWindowLimiter(1, 60)
        assert limiter.allow("a")
        for _ in range(20):
            bnow(seconds=2)
            limiter.allow("a")
        bnow(seconds=30)  # 70 s after the one accepted attempt
        assert limiter.allow("a") is True


# --------------------------------------------------------------------------------------------- global cap and creation rate
class TestSessionCap:
    def test_the_global_cap_refuses_the_next_session_with_a_clear_reason(self, bnow):
        reg = registry(max_active_sessions=3)
        for i in range(3):
            reg.create("C101", "A", client_key=f"c{i}")
        with pytest.raises(SessionLimitError) as info:
            reg.create("C101", "A", client_key="c9")
        assert info.value.reason == "capacity" and reg.count() == 3

    def test_ending_or_expiring_a_session_frees_a_slot(self, bnow):
        reg = registry(max_active_sessions=2, session_creates_per_minute=100, session_creates_per_client_per_minute=100)
        a = reg.create("C101", "A")
        reg.create("C102", "B")
        with pytest.raises(SessionLimitError):
            reg.create("C103", "C")
        assert reg.end(a.handle)
        reg.create("C103", "C")
        bnow(minutes=31)  # both remaining sessions are idle: creating purges them first
        reg.create("C104", "D")
        assert reg.count() == 1

    def test_backend_reports_capacity_without_creating_anything(self):
        backend = Backend(llm=TicketModel(), settings=settings(max_active_sessions=1))
        assert backend.start_session("C101").ok
        second = backend.start_session("C102")
        assert not second.ok and second.state is None and second.error_code == "capacity"
        assert "try again" in second.greeting.lower()
        assert backend.registry.count() == 1


class TestCreationRate:
    def test_a_client_cannot_create_sessions_faster_than_the_limit(self, bnow):
        reg = registry(session_creates_per_client_per_minute=3, session_creates_per_minute=100)
        for _ in range(3):
            reg.create("C101", "A", client_key="1.2.3.4")
        with pytest.raises(SessionLimitError) as info:
            reg.create("C101", "A", client_key="1.2.3.4")
        assert info.value.reason == "creation_rate" and info.value.retry_after > 0
        reg.create("C101", "A", client_key="5.6.7.8")  # another client is unaffected
        bnow(seconds=61)
        reg.create("C101", "A", client_key="1.2.3.4")

    def test_the_global_creation_rate_applies_to_every_client(self, bnow):
        reg = registry(session_creates_per_minute=4, session_creates_per_client_per_minute=100)
        for i in range(4):
            reg.create("C101", "A", client_key=f"c{i}")
        with pytest.raises(SessionLimitError) as info:
            reg.create("C101", "A", client_key="another")
        assert info.value.reason == "creation_rate"

    def test_a_refused_creation_does_not_use_up_the_budget(self, bnow):
        reg = registry(max_active_sessions=1, session_creates_per_client_per_minute=2, session_creates_per_minute=100)
        reg.create("C101", "A", client_key="x")
        for _ in range(5):
            with pytest.raises(SessionLimitError) as info:
                reg.create("C101", "A", client_key="x")
            assert info.value.reason == "capacity"  # refused for capacity, not counted against the rate
        reg.sweep()
        assert reg.count() == 1


class TestInProcessCallersAreNotRateLimited:
    def test_a_caller_without_a_client_identity_is_bounded_by_the_cap_only(self):
        reg = registry(max_active_sessions=50, session_creates_per_client_per_minute=2, session_creates_per_minute=3)
        for _ in range(40):  # the Streamlit UI and the evaluation harness start sessions in-process, with no peer address
            reg.create("C101", "A")
        assert reg.count() == 40

    def test_the_cap_still_applies_to_them(self):
        reg = registry(max_active_sessions=5)
        for _ in range(5):
            reg.create("C101", "A")
        with pytest.raises(SessionLimitError) as info:
            reg.create("C101", "A")
        assert info.value.reason == "capacity"

    def test_default_settings_keep_the_previous_idle_timeout(self):
        assert Settings().idle_timeout == timedelta(minutes=60)


class TestConcurrentCreation:
    def test_forty_simultaneous_requests_create_exactly_the_cap(self):
        reg = registry(max_active_sessions=10, session_creates_per_minute=1000, session_creates_per_client_per_minute=1000)
        outcomes, barrier = [], threading.Barrier(40)

        def worker(i):
            barrier.wait()
            try:
                reg.create("C101", "A", client_key=f"c{i}")
                outcomes.append("ok")
            except SessionLimitError as exc:
                outcomes.append(exc.reason)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert outcomes.count("ok") == 10 and outcomes.count("capacity") == 30 and reg.count() == 10

    def test_simultaneous_requests_from_one_client_respect_its_rate(self):
        reg = registry(max_active_sessions=1000, session_creates_per_client_per_minute=5, session_creates_per_minute=1000)
        outcomes, barrier = [], threading.Barrier(30)

        def worker():
            barrier.wait()
            try:
                reg.create("C101", "A", client_key="same")
                outcomes.append("ok")
            except SessionLimitError as exc:
                outcomes.append(exc.reason)

        threads = [threading.Thread(target=worker) for _ in range(30)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert outcomes.count("ok") == 5 and outcomes.count("creation_rate") == 25


# --------------------------------------------------------------------------------------------- expiry and cleanup
class TestIdleAndAbsoluteExpiry:
    def test_an_idle_session_expires_and_its_requests_are_refused(self, bnow):
        backend = Backend(llm=TicketModel(), settings=settings())
        c = Client(backend, "C101")
        assert c.say("hello").status in ("ok", "needs_confirmation")
        bnow(minutes=31)
        r = c.say("hello again")
        assert r.error_code == "invalid_session" and r.status == "error"

    def test_activity_keeps_a_session_alive_until_the_absolute_limit(self, bnow):
        backend = Backend(llm=TicketModel(), settings=settings(max_session_age=timedelta(hours=2),
                                                               session_requests_per_minute=1000, max_requests_per_session=1000,
                                                               max_messages_per_session=1000))
        c = Client(backend, "C101")
        for _ in range(7):
            bnow(minutes=15)
            assert c.say("hello").error_code is None  # touched every 15 minutes, 105 minutes in total
        bnow(minutes=20)  # now 125 minutes old
        assert c.say("hello").error_code == "invalid_session"

    def test_sweep_reports_idle_as_the_reason(self, bnow):
        reg = registry()
        dropped = []
        reg.add_drop_listener(lambda session, reason: dropped.append((session.handle, reason)))
        idle = reg.create("C101", "A")
        bnow(minutes=31)
        assert reg.sweep() == 1 and dropped == [(idle.handle, "idle")]

    def test_sweep_reports_age_as_the_reason_for_an_active_but_old_session(self, bnow):
        reg = registry(max_session_age=timedelta(hours=1))
        dropped = []
        reg.add_drop_listener(lambda session, reason: dropped.append((session.handle, reason)))
        old = reg.create("C102", "B")
        for _ in range(2):  # active every 25 minutes
            bnow(minutes=25)
            assert reg.get(old.handle) is not None
        assert reg.sweep() == 0
        bnow(minutes=25)  # 75 minutes old (over the 60-minute limit) but active 25 minutes ago
        assert reg.sweep() == 1 and dropped == [(old.handle, "age")]

    def test_ending_a_session_notifies_listeners_once(self):
        reg = registry()
        events = []
        reg.add_drop_listener(lambda session, reason: events.append(reason))
        s = reg.create("C101", "A")
        assert reg.end(s.handle) is True and reg.end(s.handle) is False
        assert events == ["ended"]


class TestAutomaticCleanup:
    def test_sweep_removes_expired_sessions_and_all_their_pending_actions(self, bnow):
        reg = registry()
        for i in range(5):
            s = reg.create("C101", "A", client_key=f"c{i}")
            reg.put_pending(s, "support_ticket", {"description": "x"}, "summary")
        assert len(reg._pending) == 5
        bnow(minutes=31)
        assert reg.sweep() == 5
        assert reg.count() == 0 and reg._pending == {}

    def test_the_background_sweeper_thread_cleans_up_by_itself_and_stops(self):
        reg = registry(idle_timeout=timedelta(milliseconds=40))
        reg.create("C101", "A")
        reg.start_sweeper(0.01)
        try:
            deadline = time.time() + 5
            while reg.count() and time.time() < deadline:
                time.sleep(0.01)
            assert reg.count() == 0
        finally:
            reg.stop_sweeper()
        assert reg._sweeper is None

    def test_the_sweeper_survives_a_failing_listener(self, bnow):
        reg = registry()
        reg.add_drop_listener(lambda session, reason: 1 / 0)
        reg.create("C101", "A")
        bnow(minutes=31)
        assert reg.sweep() == 1  # the failure is logged, not raised, and the session is gone

    def test_pending_history_is_bounded_per_session(self):
        reg = registry()
        s = reg.create("C101", "A")
        for i in range(60):
            reg.put_pending(s, "support_ticket", {"description": str(i)}, "summary")
        assert len(reg._pending) <= 21
        latest = reg.open_pending(s)
        assert latest is not None and latest.payload["description"] == "59"

    def test_shutdown_ends_every_session(self):
        reg = registry()
        events = []
        reg.add_drop_listener(lambda session, reason: events.append(reason))
        for i in range(3):
            reg.create("C101", "A", client_key=f"c{i}")
        reg.shutdown()
        assert reg.count() == 0 and events == ["shutdown"] * 3


# --------------------------------------------------------------------------------------------- active operations
class TestAnActiveTurnNeverLosesItsSession:
    def test_sweep_skips_a_session_whose_turn_is_running(self, bnow):
        reg = registry()
        s = reg.create("C101", "A")
        assert s.turn_lock.acquire(blocking=False)
        try:
            bnow(hours=9)  # idle AND over the absolute limit
            assert reg.sweep() == 0 and reg.get(s.handle) is s
        finally:
            s.turn_lock.release()
        assert reg.sweep() == 1

    def test_sweep_skips_a_session_with_a_claimed_action(self, bnow):
        reg = registry()
        s = reg.create("C101", "A")
        action = reg.put_pending(s, "support_ticket", {"description": "x"}, "summary")
        claimed, _ = reg.claim(s, action.action_id)
        assert claimed is not None
        bnow(hours=9)
        assert reg.sweep() == 0
        reg.finish(s, claimed, committed=True)
        assert reg.sweep() == 1

    def test_a_slow_turn_survives_the_clock_passing_the_idle_limit_and_a_sweep(self, bnow, raw):
        started, release = threading.Event(), threading.Event()

        def chat(system, messages, tools):
            started.set()
            assert release.wait(10)
            return LLMResult(text="done")

        backend = Backend(llm=ScriptedLLM(chat=chat), settings=settings())
        c = Client(backend, "C101")
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("r", c.say("where is my order ORD1001?")))
        t.start()
        assert started.wait(10)
        bnow(minutes=45)
        assert backend.registry.sweep() == 0  # the running turn protects its session
        release.set()
        t.join(10)
        assert box["r"].error_code is None
        bnow(minutes=5)
        assert c.say("thanks").error_code is None  # the session is still valid afterwards

    def test_a_request_that_waited_for_the_lock_is_refused_if_the_session_ended_meanwhile(self):
        started, release = threading.Event(), threading.Event()

        def chat(system, messages, tools):
            started.set()
            release.wait(10)
            return LLMResult(text="ok")

        backend = Backend(llm=ScriptedLLM(chat=chat), settings=settings(turn_lock_timeout_seconds=10))
        c = Client(backend, "C101")
        first = threading.Thread(target=lambda: c.say("where is my order ORD1001?"))
        first.start()
        assert started.wait(10)
        box = {}
        second = threading.Thread(target=lambda: box.setdefault("r", c.say("and ORD1002?")))
        second.start()
        time.sleep(0.2)
        backend.end_session(c.state)
        release.set()
        first.join(10)
        second.join(10)
        assert box["r"].error_code == "invalid_session"


class TestPendingActionsDuringCleanup:
    def test_an_expired_session_cannot_confirm_its_open_card_and_nothing_is_written(self, bnow, raw):
        backend = Backend(llm=TicketModel(), settings=settings())
        c = Client(backend, "C101")
        card = ticket_card(c)
        bnow(minutes=31)
        assert backend.registry.sweep() == 1
        r = c.confirm(card)
        assert r.error_code == "invalid_session"
        assert tickets(raw) == [] and backend.registry._pending == {}

    def test_a_confirmed_card_in_a_live_session_is_written_exactly_once(self, raw):
        backend = Backend(llm=TicketModel(), settings=settings())
        c = Client(backend, "C101")
        card = ticket_card(c)
        done = c.confirm(card)
        assert done.error_code is None and len(tickets(raw)) == 1


# --------------------------------------------------------------------------------------------- turn and rate limits
class TestPerSessionLimits:
    def test_every_kind_of_request_counts_toward_the_session_limit(self):
        backend = Backend(llm=TicketModel(), settings=settings(max_requests_per_session=3, session_requests_per_minute=100))
        c = Client(backend, "C101")
        for _ in range(3):
            assert c.say("hello").error_code is None
        r = c.say("hello")
        assert r.error_code == "rate_limited" and "limit" in r.text.lower()

    def test_decisions_and_choices_count_too(self):
        from app.backend.types import ChoiceSelection, ConfirmationDecision

        backend = Backend(llm=TicketModel(), settings=settings(max_requests_per_session=2, session_requests_per_minute=100))
        c = Client(backend, "C101")
        backend.handle_message(c.state, decision=ConfirmationDecision("nope", True))
        backend.handle_message(c.state, choice=ChoiceSelection("p", "o"))
        r = backend.handle_message(c.state, decision=ConfirmationDecision("nope", True))
        assert r.error_code == "rate_limited"

    def test_the_per_minute_rate_recovers_when_the_window_passes(self, bnow):
        backend = Backend(llm=TicketModel(), settings=settings(session_requests_per_minute=3))
        c = Client(backend, "C101")
        for _ in range(3):
            assert c.say("hello").error_code is None
        refused = c.say("hello")
        assert refused.error_code == "rate_limited" and "wait" in refused.text.lower()
        bnow(seconds=61)
        assert c.say("hello").error_code is None  # a valid session keeps working

    def test_a_refused_request_is_not_silently_dropped_it_says_so_and_leaves_state_untouched(self, bnow, raw):
        backend = Backend(llm=TicketModel(), settings=settings(session_requests_per_minute=2))
        c = Client(backend, "C101")
        card = ticket_card(c)  # request 1
        refused = backend.handle_message(c.state, "another message")  # request 2
        assert refused.error_code is None
        refused = c.confirm(card)  # request 3, over the minute limit: reported, not executed
        assert refused.error_code == "rate_limited" and tickets(raw) == []
        bnow(seconds=61)
        assert c.confirm(card).error_code is None and len(tickets(raw)) == 1  # the customer retries the same confirmation

    def test_message_length_limit_is_still_enforced(self):
        backend = Backend(llm=TicketModel(), settings=settings(max_message_chars=50))
        c = Client(backend, "C101")
        assert c.say("x" * 51).error_code == "message_too_long"

    def test_a_waiting_request_gives_up_instead_of_blocking_a_worker_forever(self):
        started, release = threading.Event(), threading.Event()

        def chat(system, messages, tools):
            started.set()
            release.wait(10)
            return LLMResult(text="ok")

        backend = Backend(llm=ScriptedLLM(chat=chat), settings=settings(turn_lock_timeout_seconds=0.2))
        c = Client(backend, "C101")
        t = threading.Thread(target=lambda: c.say("where is my order ORD1001?"))
        t.start()
        assert started.wait(10)
        r = c.say("hello")
        assert r.error_code == "rate_limited" and "still" in r.text.lower()
        release.set()
        t.join(10)
        assert c.say("hello").error_code is None


# --------------------------------------------------------------------------------------------- provider budget
class TestProviderBudget:
    def test_a_session_cannot_exceed_its_provider_call_budget(self):
        llm = ScriptedLLM(chat=lambda s, m, t: LLMResult(text="hi"))
        backend = Backend(llm=llm, settings=settings(provider_calls_per_session=2, session_requests_per_minute=100))
        c = Client(backend, "C101")
        assert c.say("where is my order ORD1001?").error_code is None
        assert c.say("where is my order ORD1002?").error_code is None
        calls = llm.chat_calls
        r = c.say("where is my order ORD1003?")
        assert r.error_code == "rate_limited" and llm.chat_calls == calls  # refused before any provider call

    def test_the_global_window_limits_all_sessions_together(self, bnow):
        llm = ScriptedLLM(chat=lambda s, m, t: LLMResult(text="hi"))
        backend = Backend(llm=llm, settings=settings(provider_calls_per_minute=2, session_requests_per_minute=100))
        a, b = Client(backend, "C101"), Client(backend, "C102")
        assert a.say("where is my order ORD1001?").error_code is None
        assert b.say("where is my order ORD1002?").error_code is None
        assert a.say("and ORD1003?").error_code == "rate_limited"
        bnow(seconds=61)
        assert a.say("and ORD1003?").error_code is None

    def test_a_budget_hit_in_the_middle_of_a_turn_fails_that_turn_cleanly(self):
        budget = ProviderBudget(per_session=1, per_minute=100)
        inner = ScriptedLLM(chat=lambda s, m, t: LLMResult(text="hi"), complete=lambda s, p: "x")
        wrapped = BudgetedLLM(inner, budget)
        with budget.session("s1"):
            wrapped.chat("sys", [], [], max_output_tokens=10)
            with pytest.raises(LLMError) as info:
                wrapped.chat("sys", [], [], max_output_tokens=10)
        assert info.value.category == "budget" and inner.chat_calls == 1

    def test_the_wrapper_passes_attributes_through(self):
        inner = ScriptedLLM(chat=lambda s, m, t: LLMResult(text="hi"))
        inner.interprets_language = True
        assert BudgetedLLM(inner, ProviderBudget(10, 10)).interprets_language is True
        assert BudgetedLLM(inner, ProviderBudget(10, 10)).name == "scripted"

    def test_the_backend_keeps_the_raw_client_for_inspection(self):
        llm = ScriptedLLM(chat=lambda s, m, t: LLMResult(text="hi"))
        assert Backend(llm=llm, settings=settings()).llm is llm


class TestProviderFailures:
    def test_a_provider_error_gives_an_unavailable_reply_and_the_session_survives(self, raw):
        calls = {"n": 0}

        def chat(system, messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMError("timeout", category="timeout")
            return LLMResult(text="back")

        backend = Backend(llm=ScriptedLLM(chat=chat), settings=settings())
        c = Client(backend, "C101")
        r = c.say("where is my order ORD1001?")
        assert r.status == "error" and r.error_code == "unavailable"
        assert c.say("where is my order ORD1001?").error_code is None and tickets(raw) == []

    @pytest.mark.parametrize("failure,category", [(TimeoutError("timed out"), "timeout")])
    def test_a_timeout_is_one_call_never_retried(self, failure, category):
        from app.llm.providers import OpenAIResponsesClient

        client = OpenAIResponsesClient("sk-test", "m", "https://api.example.invalid/v1", 5.0)
        with mock.patch("urllib.request.urlopen", side_effect=failure) as urlopen:
            with pytest.raises(LLMError) as info:
                client.chat("system", [{"role": "user", "content": "hi"}], [], max_output_tokens=10)
        assert info.value.category == category and urlopen.call_count == 1

    def test_a_rate_limit_response_is_one_call_never_retried(self):
        import urllib.error
        from app.llm.providers import OpenAIResponsesClient

        client = OpenAIResponsesClient("sk-test", "m", "https://api.example.invalid/v1", 5.0)
        err = urllib.error.HTTPError("https://api.example.invalid/v1/responses", 429, "Too Many Requests", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err) as urlopen:
            with pytest.raises(LLMError) as info:
                client.chat("system", [{"role": "user", "content": "hi"}], [], max_output_tokens=10)
        assert info.value.status == 429 and urlopen.call_count == 1

    def test_the_provider_timeout_setting_is_clamped(self):
        from app.llm.providers import build_llm_client

        env = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test", "LLM_MODEL": "m",
               "LLM_TIMEOUT_SECONDS": "100000"}
        assert build_llm_client(env)._timeout <= 120
        env["LLM_TIMEOUT_SECONDS"] = "0"
        assert build_llm_client(env)._timeout >= 1


# --------------------------------------------------------------------------------------------- failures and duplicates
class TestCleanupAfterFailures:
    def test_an_exception_in_a_turn_releases_the_lock_and_leaves_the_session_usable(self):
        calls = {"n": 0}

        def chat(system, messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return LLMResult(text="fine")

        backend = Backend(llm=ScriptedLLM(chat=chat), settings=settings())
        c = Client(backend, "C101")
        session = backend.registry.get(c.state._handle)
        assert c.say("where is my order ORD1001?").status == "error"
        assert not session.turn_lock.locked()
        assert c.say("where is my order ORD1001?").error_code is None

    def test_a_failing_write_does_not_leak_reserved_capacity_or_make_the_session_immortal(self, raw, bnow):
        backend = Backend(llm=TicketModel(), settings=settings())
        c = Client(backend, "C101")
        card = ticket_card(c)
        session = backend.registry.get(c.state._handle)
        with mock.patch.object(backend.engine, "_execute", side_effect=RuntimeError("db down")):
            r = c.confirm(card)
        assert r.status == "error" and tickets(raw) == []
        assert session.reserved["support_ticket"] == 0
        assert backend.registry.capacity_left(session, "support_ticket") == backend.settings.max_tickets_per_session
        bnow(minutes=31)
        assert backend.registry.sweep() == 1  # nothing keeps it alive

    def test_ending_a_session_twice_is_harmless(self):
        backend = Backend(llm=TicketModel(), settings=settings())
        c = Client(backend, "C101")
        backend.end_session(c.state)
        backend.end_session(c.state)
        assert backend.registry.count() == 0


class TestNoDuplicateBusinessOperations:
    def test_concurrent_confirmations_create_one_ticket(self, raw):
        backend = Backend(llm=TicketModel(), settings=settings(session_requests_per_minute=1000, max_requests_per_session=1000))
        c = Client(backend, "C101")
        card = ticket_card(c)
        replies, barrier = [], threading.Barrier(10)

        def worker():
            barrier.wait()
            replies.append(c.confirm(card))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(tickets(raw)) == 1 and len(replies) == 10

    def test_a_retried_confirmation_after_a_lost_response_does_not_create_a_second_ticket(self, raw):
        backend = Backend(llm=TicketModel(), settings=settings())
        c = Client(backend, "C101")
        card = ticket_card(c)
        first = c.confirm(card)
        retry = c.confirm(card)  # the client never saw the first reply and sends the same decision again
        assert first.error_code is None and len(tickets(raw)) == 1
        assert retry.pending is None and len(tickets(raw)) == 1

    def test_the_ticket_cap_is_not_exceeded_by_concurrent_cards(self, raw):
        backend = Backend(llm=TicketModel(), settings=settings(max_tickets_per_session=1, session_requests_per_minute=1000,
                                                               max_requests_per_session=1000))
        c = Client(backend, "C101")
        card = ticket_card(c)
        results = []
        threads = [threading.Thread(target=lambda: results.append(c.confirm(card))) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(tickets(raw)) == 1


class TestValidSessionsStayFunctional:
    def test_normal_use_is_unaffected_by_the_default_limits(self, raw):
        backend = Backend(llm=TicketModel(), settings=Settings())
        c = Client(backend, "C101")
        for _ in range(5):
            assert c.say("hello").error_code is None
        card = ticket_card(c)
        assert c.confirm(card).error_code is None and len(tickets(raw)) == 1

    def test_settings_from_environment_use_documented_names_and_ignore_bad_values(self):
        s = Settings.from_env({"SUPPORT_MAX_SESSIONS": "7", "SUPPORT_IDLE_TIMEOUT_SECONDS": "90",
                               "SUPPORT_MAX_BODY_BYTES": "not-a-number", "SUPPORT_PROVIDER_CALLS_PER_SESSION": "-5"})
        assert s.max_active_sessions == 7 and s.idle_timeout == timedelta(seconds=90)
        assert s.max_body_bytes == Settings().max_body_bytes  # unparsable: default
        assert s.provider_calls_per_session == Settings().provider_calls_per_session  # non-positive: default
        assert Settings.from_env({}) == Settings()
