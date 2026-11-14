"""Importing application modules and collecting the retrieval tests must never need an API key or touch the network.

Each case runs in a fresh Python process so the check is real (not influenced by this process's imports or
environment). The child process refuses every socket connection and DNS lookup and counts the attempts; it
must import the application, build nothing, and collect the three retrieval test modules, with
``OPENAI_API_KEY`` unset, blank, and with ``PYTHON_DOTENV_DISABLED=1`` (so no ``.env`` is read).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RETRIEVAL_MODULES = ["tests/test_retrieval.py", "tests/test_retrieval_regression.py",
                     "tests/test_retrieval_topics.py"]

CHILD = r"""
import socket, sys

attempts = []
def refuse(*args, **kwargs):
    attempts.append(1)
    raise OSError("network access is not allowed in this check")
socket.socket.connect = refuse
socket.socket.connect_ex = refuse
socket.create_connection = refuse
socket.getaddrinfo = refuse

import app.backend  # first: app.backend and app.orchestrator import each other (documented), backend is the entry
import app, app.db.connection, app.tools, app.orchestrator.engine, app.llm.providers
import app.rag, app.rag.retriever, app.backend.api, app.backend.http_api
import app.ui.session
if %(import_ui_client)s:
    import app.ui.client  # loads .env unless PYTHON_DOTENV_DISABLED=1, so only imported when that is set

import pytest
code = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "-p", "no:randomly"] + %(modules)r)
print("COLLECT_EXIT", int(code))
print("NETWORK_ATTEMPTS", len(attempts))
print("VECTOR_INDEX_IMPORTED", "app.rag.vector_index" in sys.modules)
"""


def run_child(variant: dict[str, str | None], import_ui_client: bool = False) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LIVEKIT_URL", "LIVEKIT_API_KEY",
                        "LIVEKIT_API_SECRET", "PYTHON_DOTENV_DISABLED", "VECTOR_STORE_DIR", "APP_DB_PATH")}
    env["ANONYMIZED_TELEMETRY"] = "False"
    env["LLM_PROVIDER"] = "rule_based"
    for key, value in variant.items():
        if value is not None:
            env[key] = value
    code = CHILD % {"modules": RETRIEVAL_MODULES, "import_ui_client": import_ui_client}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=240)


@pytest.mark.parametrize("name,variant,import_ui_client", [
    ("OPENAI_API_KEY unset", {}, False),
    ("OPENAI_API_KEY blank", {"OPENAI_API_KEY": ""}, False),
    ("PYTHON_DOTENV_DISABLED=1, key unset", {"PYTHON_DOTENV_DISABLED": "1"}, True),
])
def test_imports_and_collection_need_no_key_and_make_no_network_call(name, variant, import_ui_client):
    result = run_child(variant, import_ui_client)
    assert result.returncode == 0, (name, result.stdout[-800:], result.stderr[-800:])
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines()
                 if line.startswith(("COLLECT_EXIT", "NETWORK_ATTEMPTS", "VECTOR_INDEX_IMPORTED")))
    assert lines["COLLECT_EXIT"] == "0", (name, result.stdout[-800:])  # all three modules were collected
    assert lines["NETWORK_ATTEMPTS"] == "0"
    assert lines["VECTOR_INDEX_IMPORTED"] == "False"  # importing the application never builds or loads the index
    assert "error" not in result.stdout.lower().split("collect_exit")[0][-400:]


def test_the_retrieval_modules_have_no_import_time_knowledge_base():
    """Static guard: nothing in a test module may call ``default_knowledge_base()`` at import time."""
    for relative in RETRIEVAL_MODULES:
        for number, line in enumerate((ROOT / relative).read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#", 1)[0]
            if code.startswith("KB = ") or code.startswith("CHUNKS = "):
                assert "default_knowledge_base(" not in code, (relative, number, line)
