"""Hosted speech-to-text helpers for voice chat."""

from __future__ import annotations

import os
from typing import BinaryIO

from openai import APIError, OpenAI

_TRANSCRIPTION_MODEL = "whisper-1"


class VoiceTranscriptionError(Exception):
    """A voice recording could not be transcribed."""


def transcribe_audio(audio: bytes | BinaryIO) -> str:
    """Send a WAV recording to OpenAI and return its transcript."""
    audio_bytes = audio if isinstance(audio, bytes) else audio.read()
    if not audio_bytes:
        raise VoiceTranscriptionError("The voice recording is empty.")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or not api_key.strip():
        raise VoiceTranscriptionError(
            "OPENAI_API_KEY is required to transcribe voice messages."
        )

    try:
        result = OpenAI(api_key=api_key).audio.transcriptions.create(
            model=_TRANSCRIPTION_MODEL,
            file=("voice.wav", audio_bytes, "audio/wav"),
        )
    except APIError as exc:
        raise VoiceTranscriptionError(
            "Voice transcription is unavailable. Please try again or type your "
            "message."
        ) from exc

    transcript = result.text.strip()
    if not transcript:
        raise VoiceTranscriptionError(
            "No speech was detected. Please try again or type your message."
        )
    return transcript
