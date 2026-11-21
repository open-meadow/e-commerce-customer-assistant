"""Server-side client for creating and closing LiveKit voice sessions."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=False)

if __package__:
    from .session import LiveKitVoiceSession
else:
    from session import LiveKitVoiceSession


def _support_api_url() -> str:
    return (os.environ.get("SUPPORT_API_URL") or "http://api:8000").rstrip("/")


def _identity_headers(browser_id: str | None) -> dict[str, str]:
    """Tell the API which browser this is, proving that WE are the Streamlit server.

    The API honours ``X-Browser-Id`` only together with ``X-Service-Token`` equal to its ``SUPPORT_UI_TOKEN``. Without the
    token (or without a browser id) nothing is sent and the API falls back to this server's network address, so all voice
    users would share one bucket. The token is read from the environment and never logged.
    """
    token = (os.environ.get("SUPPORT_UI_TOKEN") or "").strip()
    if not token or not browser_id:
        return {}
    return {"X-Service-Token": token, "X-Browser-Id": browser_id}


def start_voice_session(customer_id: str, browser_id: str | None = None) -> LiveKitVoiceSession:
    with httpx.Client(base_url=_support_api_url(), timeout=30.0, headers=_identity_headers(browser_id)) as client:
        response = client.post("/api/sessions", json={"customer_id": customer_id})
        response.raise_for_status()
        payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("The support API returned an invalid voice session.")
    session_id = payload.get("session_id")
    url = payload.get("url")
    token = payload.get("token")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(url, str)
        or not url
        or not isinstance(token, str)
        or not token
    ):
        raise ValueError("The support API returned an invalid voice session.")
    return {"session_id": session_id, "url": url, "token": token}


def end_voice_session(session_id: str, browser_id: str | None = None) -> None:
    with httpx.Client(base_url=_support_api_url(), timeout=15.0, headers=_identity_headers(browser_id)) as client:
        response = client.post(
            "/api/sessions/close",
            json={"session_id": session_id},
        )
        response.raise_for_status()
