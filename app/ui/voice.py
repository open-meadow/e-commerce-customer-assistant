"""Hosted speech-to-text helpers for voice chat."""

from __future__ import annotations

import io
import os
from functools import lru_cache
from typing import TYPE_CHECKING, BinaryIO

import numpy as np
from openai import APIError, OpenAI
import soundfile as sf

if TYPE_CHECKING:
    from kokoro import KPipeline

_TRANSCRIPTION_MODEL = "whisper-1"
_KOKORO_LANGUAGE = "a"
_KOKORO_VOICE = "af_heart"
_SAMPLE_RATE = 24000


class VoiceTranscriptionError(Exception):
    """A voice recording could not be transcribed."""


class VoiceSynthesisError(Exception):
    """Assistant speech could not be synthesized."""


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


@lru_cache(maxsize=1)
def _kokoro_pipeline() -> KPipeline:
    try:
        from kokoro import KPipeline
        return KPipeline(lang_code=_KOKORO_LANGUAGE)
    except ImportError as exc:
        raise VoiceSynthesisError(
            "Kokoro runtime is unavailable. Install the project dependencies "
            "and eSpeak NG to use voice replies."
        ) from exc


def synthesize_speech(text: str) -> bytes:
    """Generate an English WAV reply locally with Kokoro."""
    if not isinstance(text, str) or not text.strip():
        raise VoiceSynthesisError("There is no reply text to speak.")

    try:
        chunks = [
            audio.detach().cpu().numpy()
            for _, _, audio in _kokoro_pipeline()(
                text.strip(),
                voice=_KOKORO_VOICE,
            )
        ]
    except (OSError, RuntimeError) as exc:
        raise VoiceSynthesisError(
            "Kokoro could not synthesize the voice reply."
        ) from exc

    if not chunks:
        raise VoiceSynthesisError("Kokoro returned no audio for the voice reply.")

    audio = np.concatenate(chunks)
    wav_file = io.BytesIO()
    try:
        sf.write(wav_file, audio, _SAMPLE_RATE, format="WAV")
    except (OSError, RuntimeError) as exc:
        raise VoiceSynthesisError(
            "The synthesized voice reply could not be encoded."
        ) from exc
    return wav_file.getvalue()
