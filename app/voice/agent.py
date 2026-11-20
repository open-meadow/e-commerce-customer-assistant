"""LiveKit worker that routes voice turns through the support backend.

H3 resource lifecycle: a call has a maximum length and an idle limit; when the job ends for ANY reason (the customer left,
a limit was reached, the backend said the session is gone, or the worker is shutting down) the API is told which room ended,
so it can end the application session and release the LiveKit room. Closing an application session does not by itself release
LiveKit resources, and the job ending does not by itself end the application session: both directions are wired explicitly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

import httpx
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, ChatContext, ChatMessage
from livekit.agents import RunContext, function_tool, inference, room_io
from livekit.plugins import openai

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=False)

_AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "ecommerce-support-agent")
_SUPPORT_API_URL = os.environ.get("SUPPORT_API_URL", "http://api:8000").rstrip("/")
_BACKEND_TIMEOUT = 30.0
_CLOSE_TIMEOUT = 5.0  # the disconnect notice must never hold a shutting-down job
DEFAULT_MAX_SECONDS = 1800.0  # SUPPORT_VOICE_MAX_SECONDS: longest call
DEFAULT_IDLE_SECONDS = 300.0  # SUPPORT_VOICE_IDLE_SECONDS: longest silence
log = logging.getLogger("app.voice")
_INSTRUCTIONS = """
You are only the voice transport for an e-commerce customer-support backend. You
never answer the customer yourself. The support backend is the source of truth.
For each customer turn, call exactly one backend tool. Use send_customer_message
for a new question or request, and whenever you are unsure which tool fits.
Use confirm_pending_action only when the backend state shows a pending action
and the customer's words are an unambiguous yes or no about that action; a
question, an unclear, partial or mixed answer, or anything unrelated goes to
send_customer_message instead. Use select_backend_option only when the customer
clearly chooses one of the currently offered options.

Backend tools speak their reply directly, so do not generate any additional
spoken response after calling one. Never invent confirmation or choice IDs.
Never say that anything was done, created or submitted yourself. If a tool
fails, say only that you could not reach the support system and to try again.
"""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set.")
    return value


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


class SessionWatchdog:
    """A call's limits: a maximum length, and a maximum time since the customer last spoke or was answered."""

    def __init__(self, *, max_seconds: float, idle_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_seconds = max_seconds if max_seconds > 0 else DEFAULT_MAX_SECONDS
        self.idle_seconds = idle_seconds if idle_seconds > 0 else DEFAULT_IDLE_SECONDS
        self._clock = clock
        self._started = clock()
        self._last_activity = self._started

    def touch(self) -> None:
        self._last_activity = self._clock()

    def expired(self) -> Optional[str]:
        now = self._clock()
        if now - self._started > self.max_seconds:
            return "max_duration"
        if now - self._last_activity > self.idle_seconds:
            return "idle"
        return None


async def run_watchdog(watchdog: SessionWatchdog, shutdown: Callable[[str], object], *, poll_seconds: float = 5.0,
                       sleep=asyncio.sleep) -> None:
    """End the job (once) when a limit is reached. Cancel this task to stop watching."""
    while True:
        reason = watchdog.expired()
        if reason is not None:
            log.info("voice session ended: %s", reason)
            shutdown(reason)
            return
        await sleep(poll_seconds)


async def _notify_session_closed(room_name: str) -> None:
    """Tell the API this room's job ended. Best effort and bounded: a failure here must never block or break shutdown."""
    try:
        async with httpx.AsyncClient(base_url=_SUPPORT_API_URL, timeout=_CLOSE_TIMEOUT) as client:
            response = await client.post("/api/agent/close", json={"room_name": room_name})
            response.raise_for_status()
    except Exception as exc:  # the session may already be gone (404) or the API may be down; the API's own sweeper covers it
        log.info("could not notify the API that the voice session ended (%s)", type(exc).__name__)


async def _post_backend_turn(room_name: str, **turn: object) -> dict[str, object]:
    async with httpx.AsyncClient(
        base_url=_SUPPORT_API_URL,
        timeout=_BACKEND_TIMEOUT,
    ) as client:
        response = await client.post(
            "/api/agent/turn",
            json={"room_name": room_name, **turn},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise RuntimeError("The support API returned an invalid turn response.")
        return result


class SupportAgent(Agent):
    def __init__(self, room_name: str, on_end: Optional[Callable[[str], object]] = None,
                 on_activity: Optional[Callable[[], object]] = None) -> None:
        super().__init__(instructions=_INSTRUCTIONS)
        self._room_name = room_name
        self._on_end = on_end or (lambda reason: None)
        self._on_activity = on_activity or (lambda: None)
        self._latest_user_message = ""
        self._turn_claimed = False
        self._last_backend_reply: dict[str, object] | None = None

    async def on_user_turn_completed(
        self,
        turn_ctx: ChatContext,
        new_message: ChatMessage,
    ) -> None:
        self._on_activity()
        self._latest_user_message = new_message.text_content
        self._turn_claimed = False
        if self._last_backend_reply is not None:
            state = {
                key: self._last_backend_reply.get(key)
                for key in ("status", "error_code", "pending", "choice")
            }
            turn_ctx.add_message(
                role="assistant",
                content=(
                    "Internal support backend state for choosing the next tool; "
                    "do not speak this metadata: "
                    f"{json.dumps(state, ensure_ascii=False)}"
                ),
            )

    def _claim_turn(self) -> None:
        if self._turn_claimed:
            raise RuntimeError("Only one support action is allowed per user turn.")
        self._turn_claimed = True

    async def _speak_backend_reply(
        self,
        context: RunContext,
        reply: dict[str, object],
    ) -> None:
        self._last_backend_reply = reply
        text = reply["text"]
        if not isinstance(text, str):
            raise RuntimeError("The support API returned an invalid turn response.")
        await context.session.say(text)
        if reply.get("error_code") == "invalid_session":
            self._on_end("session ended")  # the backend no longer has this session: do not keep the call open

    @function_tool()
    async def send_customer_message(self, context: RunContext) -> None:
        """Send a new customer question or request to the support backend.

        The backend receives the exact latest speech transcript; do not
        paraphrase or reconstruct the customer's message.
        """
        self._claim_turn()
        reply = await _post_backend_turn(
            self._room_name,
            message=self._latest_user_message,
        )
        await self._speak_backend_reply(context, reply)

    @function_tool()
    async def confirm_pending_action(
        self,
        context: RunContext,
        action_id: str,
        accepted: bool,
    ) -> None:
        """Confirm or cancel the pending action after a clear yes or no.

        Use only the action_id returned by the backend for the current
        pending action. Set accepted to true only for explicit confirmation.
        """
        self._claim_turn()
        context.disallow_interruptions()
        reply = await _post_backend_turn(
            self._room_name,
            decision={"action_id": action_id, "accepted": accepted},
        )
        await self._speak_backend_reply(context, reply)

    @function_tool()
    async def select_backend_option(
        self,
        context: RunContext,
        prompt_id: str,
        option_id: str,
    ) -> None:
        """Select one of the options currently offered by the backend."""
        self._claim_turn()
        reply = await _post_backend_turn(
            self._room_name,
            choice={"prompt_id": prompt_id, "option_id": option_id},
        )
        await self._speak_backend_reply(context, reply)


server = AgentServer()


@server.rtc_session(agent_name=_AGENT_NAME)
async def support_agent(ctx: agents.JobContext) -> None:
    watchdog = SessionWatchdog(max_seconds=_env_seconds("SUPPORT_VOICE_MAX_SECONDS", DEFAULT_MAX_SECONDS),
                               idle_seconds=_env_seconds("SUPPORT_VOICE_IDLE_SECONDS", DEFAULT_IDLE_SECONDS))
    watchdog_task = asyncio.create_task(run_watchdog(watchdog, ctx.shutdown))

    async def on_shutdown() -> None:
        # Runs however the job ends. Stop the watchdog, then tell the API so it ends the session and releases the room.
        watchdog_task.cancel()
        await _notify_session_closed(ctx.room.name)

    ctx.add_shutdown_callback(on_shutdown)
    session = AgentSession(
        stt=openai.STT(model=_required_env("LIVEKIT_STT_MODEL")),
        llm=openai.LLM(model=os.environ.get("LIVEKIT_AGENT_MODEL") or "gpt-4o-mini"),
        tts=inference.TTS(
            model=_required_env("LIVEKIT_TTS_MODEL"),
            voice=_required_env("FISH_AUDIO_VOICE"),
        ),
        turn_detection="stt",
        use_tts_aligned_transcript=True,
    )
    await session.start(
        room=ctx.room,
        agent=SupportAgent(ctx.room.name, on_end=ctx.shutdown, on_activity=watchdog.touch),
        room_options=room_io.RoomOptions(
            text_output=room_io.TextOutputOptions(sync_transcription=True),
        ),
    )
    await session.say(
        "Hello! I'm your e-commerce support assistant. How can I help you today?"
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
