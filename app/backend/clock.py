"""Clock for sessions, expiry and pending actions.

Separate from the repository clock (``app.db.clock``). Tests monkeypatch
``now`` here to control idle expiry and confirmation expiry.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)
