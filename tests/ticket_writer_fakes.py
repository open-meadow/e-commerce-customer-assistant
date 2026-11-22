"""Test-only fake ticket writer and a backend builder that injects it (not a test module).

This module lives under ``tests/`` on purpose: application code must never be able to import or select a fake writer. Production
composition (``app.backend.api.build_ticket_writer``) accepts only the real repository writer. A test that needs the fresh-description
flow injects the fake straight into the ``Engine`` after building a ``Backend`` with the gate OFF, exactly as the design allows.
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Optional

from app.backend import Backend, Settings
from app.orchestrator.ticket_writer import (CREATED, TicketWriteRequest, TicketWriteResult)
from backend_helpers import ScriptedLLM


class FakeTicketWriter:
    """Records every request and answers from a script. It writes nothing anywhere."""

    def __init__(self, *outcomes, default: Optional[TicketWriteResult] = None):
        self.script: list = list(outcomes)
        self.default = default or TicketWriteResult(CREATED, ticket_id="TKT_FAKE0001", status="Open")
        self.requests: list[TicketWriteRequest] = []

    def create_ticket(self, request: TicketWriteRequest) -> TicketWriteResult:
        self.requests.append(request)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                return item(request)
            return item
        return self.default


def fresh_backend(writer: Optional[FakeTicketWriter] = None, llm=None, **settings) -> Backend:
    """A backend with the fresh-description flow ON and a fake writer injected into its engine (tests only)."""
    backend = Backend(llm=llm or ScriptedLLM(), settings=Settings(**settings))
    backend.settings = dataclasses.replace(backend.settings, fresh_ticket_flow_enabled=True)
    backend.engine.settings = backend.settings
    backend.engine.ticket_writer = writer if writer is not None else FakeTicketWriter()
    return backend


def session_of(client):
    return client.backend.registry.get(client.state._handle)
