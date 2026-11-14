"""LiveKit worker that routes voice turns through the support backend."""

from __future__ import annotations

import json
import os
from pathlib import Path

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
_INSTRUCTIONS = """
You are the voice interface for an e-commerce customer-support assistant.
The support backend is the source of truth. For each customer turn, call exactly
one backend tool before replying. Use send_customer_message for a new question
or request, confirm_pending_action only when the customer clearly says yes or
no to the currently pending confirmation, and select_backend_option only when
the customer clearly chooses one of the currently offered options.

Backend tools speak their reply directly, so do not generate any additional
spoken response after calling one. Never invent confirmation or choice IDs.
"""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set.")
    return value


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
    def __init__(self, room_name: str) -> None:
        super().__init__(instructions=_INSTRUCTIONS)
        self._room_name = room_name
        self._latest_user_message = ""
        self._turn_claimed = False
        self._last_backend_reply: dict[str, object] | None = None

    async def on_user_turn_completed(
        self,
        turn_ctx: ChatContext,
        new_message: ChatMessage,
    ) -> None:
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
        agent=SupportAgent(ctx.room.name),
        room_options=room_io.RoomOptions(
            text_output=room_io.TextOutputOptions(sync_transcription=True),
        ),
    )
    await session.say(
        "Hello! I'm your e-commerce support assistant. How can I help you today?"
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
