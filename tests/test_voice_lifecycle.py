"""H3: the LiveKit voice worker's own resource lifecycle.

Closing an application session does not by itself release LiveKit resources, and the agent job does not tell the API when the
customer leaves. These tests cover the worker side: a maximum call length and an idle limit, the disconnect notice to the API,
the job ending when the backend says the session is gone, and a clean shutdown. No LiveKit server, no model, no network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from app.voice import agent as voice


AGENT_TOKEN = "agent-secret-token-for-tests-0001"


@pytest.fixture(autouse=True)
def _agent_token(monkeypatch):
    monkeypatch.setenv("SUPPORT_AGENT_TOKEN", AGENT_TOKEN)


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestWatchdog:
    def test_an_idle_call_expires_and_activity_resets_only_the_idle_timer(self):
        clock = FakeClock()
        dog = voice.SessionWatchdog(max_seconds=600, idle_seconds=60, clock=clock)
        assert dog.expired() is None
        clock.advance(59)
        dog.touch()
        clock.advance(59)
        assert dog.expired() is None
        clock.advance(2)
        assert dog.expired() == "idle"

    def test_the_maximum_length_applies_even_to_an_active_call(self):
        clock = FakeClock()
        dog = voice.SessionWatchdog(max_seconds=100, idle_seconds=60, clock=clock)
        for _ in range(4):
            clock.advance(30)
            dog.touch()
        assert dog.expired() == "max_duration"

    def test_non_positive_limits_fall_back_to_the_defaults(self):
        dog = voice.SessionWatchdog(max_seconds=0, idle_seconds=-1, clock=FakeClock())
        assert dog.max_seconds == voice.DEFAULT_MAX_SECONDS and dog.idle_seconds == voice.DEFAULT_IDLE_SECONDS

    def test_the_watchdog_ends_the_job_once_when_a_limit_is_reached(self):
        clock = FakeClock()
        dog = voice.SessionWatchdog(max_seconds=100, idle_seconds=30, clock=clock)
        reasons = []

        async def sleep(seconds):
            clock.advance(seconds)

        async def go():
            await voice.run_watchdog(dog, lambda reason: reasons.append(reason), poll_seconds=10, sleep=sleep)

        run(go())
        assert reasons == ["idle"]

    def test_the_watchdog_can_be_cancelled_without_ending_the_job(self):
        dog = voice.SessionWatchdog(max_seconds=100, idle_seconds=30, clock=FakeClock())
        reasons = []

        async def go():
            task = asyncio.create_task(voice.run_watchdog(dog, reasons.append, poll_seconds=0.01))
            await asyncio.sleep(0.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run(go())
        assert reasons == []


class TestDisconnectNotice:
    def test_the_api_is_told_which_room_ended(self):
        posted = []

        class Client:
            def __init__(self, **kw):
                self.kw = kw

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, path, json=None):
                posted.append((path, json, self.kw.get("timeout")))
                return SimpleNamespace(status_code=204, raise_for_status=lambda: None)

        with mock.patch.object(voice.httpx, "AsyncClient", Client):
            run(voice._notify_session_closed("support-abc"))
        assert posted == [("/api/agent/close", {"room_name": "support-abc"}, voice._CLOSE_TIMEOUT)]
        assert voice._CLOSE_TIMEOUT <= 10

    @pytest.mark.parametrize("failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), RuntimeError("odd")])
    def test_a_failed_notice_never_raises_during_shutdown(self, failure):
        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, path, json=None):
                raise failure

        with mock.patch.object(voice.httpx, "AsyncClient", Client):
            run(voice._notify_session_closed("support-abc"))  # must not raise


class TestAgentEndsWhenTheSessionIsGone:
    def make(self, reply):
        ended, said = [], []
        agent = voice.SupportAgent("support-abc", on_end=lambda reason: ended.append(reason))
        context = SimpleNamespace(session=SimpleNamespace(say=mock.AsyncMock(side_effect=lambda text: said.append(text))))
        return agent, context, ended, said

    def test_an_invalid_session_reply_is_spoken_and_then_the_job_ends(self):
        agent, context, ended, said = self.make(None)
        run(agent._speak_backend_reply(context, {"text": "Your session has ended.", "error_code": "invalid_session"}))
        assert said == ["Your session has ended."] and ended == ["session ended"]

    def test_a_normal_reply_does_not_end_the_job(self):
        agent, context, ended, said = self.make(None)
        run(agent._speak_backend_reply(context, {"text": "Here you go.", "error_code": None}))
        assert said == ["Here you go."] and ended == []

    def test_every_turn_counts_as_activity(self):
        touched = []
        agent = voice.SupportAgent("support-abc", on_activity=lambda: touched.append(1))
        run(agent.on_user_turn_completed(SimpleNamespace(add_message=lambda **kw: None),
                                         SimpleNamespace(text_content="hello")))
        assert touched == [1]


class TestWorkerEntryPoint:
    def test_shutdown_callbacks_notify_the_api_and_stop_the_watchdog(self, monkeypatch):
        monkeypatch.setenv("LIVEKIT_STT_MODEL", "stt")
        monkeypatch.setenv("LIVEKIT_TTS_MODEL", "tts")
        monkeypatch.setenv("FISH_AUDIO_VOICE", "voice")
        callbacks, shutdowns = [], []
        ctx = SimpleNamespace(room=SimpleNamespace(name="support-abc"),
                              add_shutdown_callback=callbacks.append,
                              shutdown=lambda reason="": shutdowns.append(reason))
        notify = mock.AsyncMock()
        session = mock.MagicMock()
        session.start = mock.AsyncMock()
        session.say = mock.AsyncMock()

        async def go():
            with mock.patch.object(voice, "AgentSession", return_value=session), \
                    mock.patch.object(voice, "_notify_session_closed", notify), \
                    mock.patch.object(voice.openai, "STT"), mock.patch.object(voice.openai, "LLM"), \
                    mock.patch.object(voice.inference, "TTS"), mock.patch.object(voice.room_io, "RoomOptions"):
                await voice.support_agent(ctx)
                assert callbacks, "the job must register a shutdown callback"
                for callback in callbacks:
                    await callback()

        run(go())
        notify.assert_awaited_once_with("support-abc")
        assert shutdowns == []  # a normal shutdown does not trigger another shutdown


class FakeHttp:
    """Records what the worker sends to the API (client options, path, body)."""

    def __init__(self, status=204, error=None):
        self.calls, self.status, self.error = [], status, error

    def factory(self):
        outer = self

        class Client:
            def __init__(self, **kw):
                self.kw = kw

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, path, json=None):
                outer.calls.append((path, json, dict(self.kw.get("headers") or {}), self.kw.get("timeout")))
                if outer.error:
                    raise outer.error
                return SimpleNamespace(status_code=outer.status, raise_for_status=lambda: None,
                                       json=lambda: {"text": "ok", "error_code": None})

        return Client


class TestServiceCredential:
    def test_every_backend_call_carries_the_service_token(self):
        fake = FakeHttp()
        with mock.patch.object(voice.httpx, "AsyncClient", fake.factory()):
            run(voice._post_backend_turn("support-abc", message="hello"))
            run(voice._notify_session_closed("support-abc"))
        assert [c[0] for c in fake.calls] == ["/api/agent/turn", "/api/agent/close"]
        assert all(c[2] == {"X-Service-Token": AGENT_TOKEN} for c in fake.calls)

    def test_a_missing_token_sends_nothing_and_never_raises_on_shutdown(self, monkeypatch):
        monkeypatch.delenv("SUPPORT_AGENT_TOKEN")
        fake = FakeHttp()
        with mock.patch.object(voice.httpx, "AsyncClient", fake.factory()):
            run(voice._notify_session_closed("support-abc"))  # must not raise
            with pytest.raises(RuntimeError) as info:
                run(voice._post_backend_turn("support-abc", message="hello"))
        assert fake.calls == [] and "SUPPORT_AGENT_TOKEN" in str(info.value)

    def test_the_token_never_appears_in_an_error_or_a_log(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        monkeypatch.setenv("SUPPORT_AGENT_TOKEN", "short")  # malformed: refused without echoing it
        with pytest.raises(ValueError) as info:
            voice._agent_headers()
        assert "short" not in str(info.value)
        monkeypatch.setenv("SUPPORT_AGENT_TOKEN", AGENT_TOKEN)
        fake = FakeHttp(error=httpx.ConnectError("down"))
        with mock.patch.object(voice.httpx, "AsyncClient", fake.factory()):
            run(voice._notify_session_closed("support-abc"))
        assert AGENT_TOKEN not in caplog.text


class TestHardLimitsEndTheCall:
    """F5: a hard session limit ends the voice job; a transient refusal does not."""

    def make(self):
        ended, said = [], []
        agent = voice.SupportAgent("support-abc", on_end=lambda reason: ended.append(reason))
        context = SimpleNamespace(session=SimpleNamespace(say=mock.AsyncMock(side_effect=lambda text: said.append(text))))
        return agent, context, ended, said

    def test_a_terminal_reply_is_spoken_then_the_job_ends(self):
        agent, context, ended, said = self.make()
        run(agent._speak_backend_reply(context, {"text": "You have reached the usage limit for this session.",
                                                 "error_code": "rate_limited", "terminal": True}))
        assert said == ["You have reached the usage limit for this session."] and ended == ["session limit reached"]

    def test_a_transient_rate_limit_does_not_hang_up(self):
        agent, context, ended, said = self.make()
        run(agent._speak_backend_reply(context, {"text": "Please wait a moment.", "error_code": "rate_limited",
                                                 "terminal": False}))
        assert said == ["Please wait a moment."] and ended == []

    def test_a_reply_without_the_field_is_not_terminal(self):
        agent, context, ended, said = self.make()
        run(agent._speak_backend_reply(context, {"text": "Fine.", "error_code": None}))
        assert ended == []

    def test_a_vanished_session_still_ends_the_job_once(self):
        agent, context, ended, said = self.make()
        run(agent._speak_backend_reply(context, {"text": "Gone.", "error_code": "invalid_session", "terminal": True}))
        assert ended == ["session ended"]  # one reason, one shutdown request

    def test_ending_the_job_notifies_the_backend_through_the_shutdown_callback(self, monkeypatch):
        monkeypatch.setenv("LIVEKIT_STT_MODEL", "stt")
        monkeypatch.setenv("LIVEKIT_TTS_MODEL", "tts")
        monkeypatch.setenv("FISH_AUDIO_VOICE", "voice")
        callbacks, shutdowns = [], []
        ctx = SimpleNamespace(room=SimpleNamespace(name="support-abc"), add_shutdown_callback=callbacks.append,
                              shutdown=lambda reason="": shutdowns.append(reason))
        notify, session = mock.AsyncMock(), mock.MagicMock()
        session.start, session.say = mock.AsyncMock(), mock.AsyncMock()
        captured = {}

        def agent_factory(room_name, on_end=None, on_activity=None):
            captured["on_end"] = on_end
            return mock.MagicMock()

        async def go():
            with mock.patch.object(voice, "AgentSession", return_value=session), \
                    mock.patch.object(voice, "_notify_session_closed", notify), \
                    mock.patch.object(voice, "SupportAgent", agent_factory), \
                    mock.patch.object(voice.openai, "STT"), mock.patch.object(voice.openai, "LLM"), \
                    mock.patch.object(voice.inference, "TTS"), mock.patch.object(voice.room_io, "RoomOptions"):
                await voice.support_agent(ctx)
                captured["on_end"]("session limit reached")  # what the agent does after a terminal reply
                for callback in callbacks:  # the framework then runs the shutdown callbacks
                    await callback()

        run(go())
        assert shutdowns == ["session limit reached"]
        notify.assert_awaited_once_with("support-abc")


class TestStrictStartup:
    @pytest.mark.parametrize("variable,value", [
        ("SUPPORT_VOICE_MAX_SECONDS", "0"), ("SUPPORT_VOICE_MAX_SECONDS", "abc"), ("SUPPORT_VOICE_MAX_SECONDS", "999999"),
        ("SUPPORT_VOICE_MAX_SECONDS", "5"), ("SUPPORT_VOICE_IDLE_SECONDS", "-1"), ("SUPPORT_VOICE_IDLE_SECONDS", "nan"),
        ("SUPPORT_VOICE_IDLE_SECONDS", "100000")])
    def test_an_invalid_limit_is_rejected_with_a_clear_message(self, variable, value):
        env = {"SUPPORT_AGENT_TOKEN": AGENT_TOKEN, variable: value}
        with pytest.raises(voice.ConfigError) as info:
            voice.check_startup(env)
        assert variable in str(info.value) and AGENT_TOKEN not in str(info.value)

    def test_idle_longer_than_the_call_limit_is_rejected(self):
        with pytest.raises(voice.ConfigError):
            voice.check_startup({"SUPPORT_AGENT_TOKEN": AGENT_TOKEN, "SUPPORT_VOICE_MAX_SECONDS": "60",
                                 "SUPPORT_VOICE_IDLE_SECONDS": "120"})

    @pytest.mark.parametrize("token", [None, "", "short", "has a space in it 1234567890", "x" * 300])
    def test_a_missing_or_malformed_token_is_rejected_without_echoing_it(self, token):
        env = {} if token is None else {"SUPPORT_AGENT_TOKEN": token}
        with pytest.raises(voice.ConfigError) as info:
            voice.check_startup(env)
        assert "SUPPORT_AGENT_TOKEN" in str(info.value)
        assert not token or token not in str(info.value)

    def test_the_defaults_with_a_valid_token_start(self):
        voice.check_startup({"SUPPORT_AGENT_TOKEN": AGENT_TOKEN})

    def test_a_job_with_an_invalid_limit_stops_before_doing_any_work(self, monkeypatch):
        monkeypatch.setenv("SUPPORT_VOICE_MAX_SECONDS", "0")
        ctx = SimpleNamespace(room=SimpleNamespace(name="support-abc"), add_shutdown_callback=lambda cb: None,
                              shutdown=lambda reason="": None)
        with mock.patch.object(voice, "AgentSession") as session:
            with pytest.raises(voice.ConfigError):
                run(voice.support_agent(ctx))
        session.assert_not_called()
