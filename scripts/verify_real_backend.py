"""Scripted business-behaviour check of the real backend (offline, deterministic).

    uv run python scripts/verify_real_backend.py

What it does
------------
* Builds a FRESH temporary seeded database on every run (explicit path in a temporary folder,
  fixed reference time 2026-11-07T00:00:00Z, both clocks fixed at that instant), runs the real
  ``app.backend.Backend`` with the offline ``RuleBasedLLM`` only, and deletes everything afterwards,
  even when a check or the setup fails. It reuses ``scripts/eval/real_eval.py``'s ``environment()``
  for that (temporary database, fixed clocks, ``APP_DB_PATH`` restored on exit).
* Never reads ``.env``, never uses the default database path and never opens
  ``app/db/data/app.db`` or ``app/db/data/app_d2.db``: a guard refuses any attempt to open them, and a
  final check requires that the temporary database was the only SQLite file opened.
* Makes no network request and no LLM call to a provider: network functions are replaced by tripwires
  that count and refuse attempts, and the final check requires zero attempts.
* Drives the same public calls the Streamlit UI uses (``handle_message`` with a message, a choice or a
  confirmation decision) and checks BUSINESS STATE: database rows, pending actions and read-only
  repository answers used as an independent oracle. Reply text is only checked where the behaviour is
  communicated by text (an order's status, an ID the customer must see, a list of own records).

Exit code: 0 when every check passes, 1 when any check fails, 2 when the run could not complete.
Output contains check names and short facts (IDs and statuses of synthetic data), no temporary paths
and no credentials.

What it does NOT cover (see docs/06_demo_runbook.md): the Streamlit page and browser flow, the UI client
adapter and startup gate (tests/ui covers them), 10-minute expiry (the clock is fixed), and any real
language model.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Iterator
from unittest import mock
from urllib.parse import unquote, urlparse

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.backend.types import ChoiceSelection, ConfirmationDecision  # noqa: E402
from app.db import connection as db_connection  # noqa: E402
from app.db import repository  # noqa: E402
from app.llm.rule_based import RuleBasedLLM  # noqa: E402
from scripts.eval import real_eval  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_DATA_DIR = _PROJECT_ROOT / "app" / "db" / "data"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


# ---------------------------------------------------------------------------
# Tripwires
# ---------------------------------------------------------------------------

class Tripwires:
    """Counts and refuses network use; refuses protected databases and records every SQLite file opened."""

    def __init__(self) -> None:
        self.network_attempts = 0
        self.sqlite_opened: set[str] = set()
        self._real_connect = sqlite3.connect

    def _refuse_network(self, *args: Any, **kwargs: Any) -> Any:
        self.network_attempts += 1
        raise AssertionError("network access attempted")

    def _guarded_connect(self, database: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(database, (str, bytes, os.PathLike)):
            text = os.fsdecode(database)
            if text.startswith("file:"):
                text = unquote(urlparse(text).path)
                if len(text) > 2 and text[0] == "/" and text[2] == ":":
                    text = text[1:]
            if text != ":memory:":
                path = _norm(text)
                protected = {_norm(db_connection.PROTECTED_DB_PATH), _norm(db_connection.DEFAULT_DB_PATH)}
                if path in protected or path.startswith(_norm(str(_DATA_DIR)) + os.sep):
                    raise AssertionError("refusing to open a protected or default database")
                self.sqlite_opened.add(path)
        return self._real_connect(database, *args, **kwargs)

    @contextlib.contextmanager
    def active(self) -> Iterator["Tripwires"]:
        with mock.patch.object(urllib.request, "urlopen", self._refuse_network), \
                mock.patch.object(socket.socket, "connect", self._refuse_network), \
                mock.patch.object(socket.socket, "connect_ex", self._refuse_network), \
                mock.patch.object(socket, "create_connection", self._refuse_network), \
                mock.patch.object(socket, "getaddrinfo", self._refuse_network), \
                mock.patch.object(sqlite3, "connect", self._guarded_connect):
            yield self


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool]] = []

    def check(self, name: str, ok: bool, expected: str = "", actual: str = "") -> bool:
        ok = bool(ok)
        self.results.append((name, ok))
        print(f"[{PASS if ok else FAIL}] {name}")
        if not ok:
            print(f"        expected: {expected}")
            print(f"        actual:   {actual}")
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for _, ok in self.results if not ok)


class Chat:
    """One signed-in session, using only the public backend calls the UI uses."""

    def __init__(self, backend: Any, customer_id: str) -> None:
        started = backend.start_session(customer_id)
        if not started.ok or started.state is None:
            raise RuntimeError(f"could not start a session for {customer_id}")
        self.backend, self.state = backend, started.state

    def say(self, text: str) -> Any:
        return self.backend.handle_message(self.state, text)

    def pick(self, reply: Any, option_id: str) -> Any:
        return self.backend.handle_message(self.state, choice=ChoiceSelection(reply.choice.prompt_id, option_id))

    def decide(self, action_id: str, accepted: bool = True) -> Any:
        return self.backend.handle_message(self.state, decision=ConfirmationDecision(action_id, accepted))


def _rows(env: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    connection = db_connection.get_readonly_connection(env.db_path)
    try:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _no_write(reply: Any) -> bool:
    return reply.pending is None and reply.choice is None


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

RETURN_REASON = "I want to return item IT101 from order ORD1001 because I changed my mind about it"
TICKET_ISSUE = "My earbuds from ORD1001 stopped charging after two days and I need help."


def run_checks(env: Any, tripwires: Tripwires, report: Report) -> None:
    backend = env.backend

    print("== Startup")
    health = backend.check_backend()
    resolved = _norm(db_connection.resolve_db_path())
    report.check("startup: temporary database, not the default or protected path",
                 resolved == _norm(env.db_path) and resolved not in
                 {_norm(db_connection.DEFAULT_DB_PATH), _norm(db_connection.PROTECTED_DB_PATH)},
                 "APP_DB_PATH resolves to the temporary database only", "resolved to another path")
    report.check("startup: RuleBasedLLM only, database ready",
                 type(backend.llm.inner).__name__ == "RuleBasedLLM" and health.status == "ready"
                 and health.llm_mode == "rule_based",
                 "RuleBasedLLM, status ready, llm_mode rule_based",
                 f"{type(backend.llm.inner).__name__}, {health.status}, {health.llm_mode}")

    print("== Customer selection and seed state")
    ids = sorted(option.customer_id for option in backend.list_login_options())
    report.check("login lists the six seeded customers", ids == [f"C10{n}" for n in range(1, 7)],
                 "C101..C106", str(ids))
    unknown = backend.start_session("C999")
    report.check("an unknown customer cannot start a session",
                 not unknown.ok and unknown.state is None and unknown.error_code == "unknown_customer",
                 "ok=False, unknown_customer", f"ok={unknown.ok}, {unknown.error_code}")
    report.check("seed starts with no returns and no tickets", env.counts() == {"return_requests": 0, "support_tickets": 0},
                 "0 and 0", str(env.counts()))

    print("== Customer-scoped order lookup")
    c101 = Chat(backend, "C101")
    own = repository.get_order("C101", "ORD1001")
    reply = c101.say("Where is my order ORD1001?")
    report.check("own order: reply states the database status of ORD1001",
                 own is not None and reply.status == "ok" and "ORD1001" in reply.text
                 and own["status"].lower() in reply.text.lower(),
                 "ok reply naming ORD1001 and its stored status", f"status={reply.status}")
    foreign_visible = repository.get_order("C101", "ORD1003")
    reply = c101.say("status of ORD1003")
    report.check("foreign order: not visible to C101 and nothing about it is revealed",
                 foreign_visible is None and reply.status == "ok" and "IT103" not in reply.text
                 and "Bob" not in reply.text,
                 "repository returns None for C101; no C102 data in the reply", f"visible={foreign_visible is not None}")

    print("== Return eligibility and rejection (no write)")
    base = env.counts()
    reply = c101.say("I want to return item IT103 from order ORD1003 because it broke")
    report.check("return of another customer's item: no choice, no confirmation, no write",
                 _no_write(reply) and env.counts() == base, "nothing offered, counts unchanged",
                 f"pending={reply.pending is not None}, choice={reply.choice is not None}")
    outcome = repository.check_return_eligibility("C101", "ORD1001", "IT101", reason_category="damaged")["outcome"]
    reply = c101.say("I want to return item IT101 from order ORD1001 because it arrived damaged")
    picked = c101.pick(reply, "damaged") if reply.choice is not None else reply
    report.check("expired damaged window: refused, no confirmation, no write",
                 outcome == "outside_return_window" and picked.pending is None and env.counts() == base,
                 "oracle outside_return_window; no pending action; counts unchanged",
                 f"oracle={outcome}, pending={picked.pending is not None}")
    outcome = repository.check_return_eligibility("C101", "ORD1002", "IT102", reason_category="standard")["outcome"]
    reply = c101.say("I want to return item IT102 from order ORD1002 because it is not what I wanted")
    picked = c101.pick(reply, "standard") if reply.choice is not None else reply
    report.check("order not delivered: refused, no confirmation, no write",
                 outcome == "order_status_ineligible" and picked.pending is None and env.counts() == base,
                 "oracle order_status_ineligible; no pending action; counts unchanged",
                 f"oracle={outcome}, pending={picked.pending is not None}")

    print("== Return proposal, explicit confirmation, exact reason")
    reply = c101.say(RETURN_REASON)
    report.check("return request offers a reason category and writes nothing",
                 reply.status == "needs_choice" and env.counts() == base, "needs_choice, counts unchanged",
                 f"status={reply.status}")
    proposal = c101.pick(reply, "standard")
    report.check("return proposal needs confirmation, shows the exact reason, and writes nothing",
                 proposal.status == "needs_confirmation" and proposal.pending is not None
                 and proposal.pending.kind == "return_request" and RETURN_REASON in proposal.pending.summary
                 and env.counts() == base,
                 "needs_confirmation (return_request) with the typed reason; counts unchanged",
                 f"status={proposal.status}")
    typed = c101.say("yes, confirm it")
    report.check("typing 'yes' does not confirm: still pending, nothing written",
                 typed.status == "needs_confirmation" and env.counts() == base,
                 "still needs_confirmation; counts unchanged", f"status={typed.status}")
    action_id = proposal.pending.action_id
    done = c101.decide(action_id)
    returns = _rows(env, "SELECT return_id, customer_id, order_id, item_id, reason, reason_category, status "
                         "FROM return_requests")
    row = returns[0] if returns else {}
    report.check("confirmation writes exactly one return owned by C101 for the right item",
                 len(returns) == 1 and row.get("customer_id") == "C101" and row.get("order_id") == "ORD1001"
                 and row.get("item_id") == "IT101" and row.get("reason_category") == "standard"
                 and str(row.get("return_id", "")).startswith("RET_"),
                 "one RET_ row: C101, ORD1001, IT101, standard", f"{len(returns)} row(s)")
    report.check("the stored reason is the customer's own text, exactly",
                 row.get("reason") == RETURN_REASON, "reason == typed message", "reason differs from the typed message")
    report.check("the reply shows the new return ID the customer needs",
                 bool(row) and row["return_id"] in done.text, "ID in reply", "ID missing from reply")
    again = c101.decide(action_id)
    report.check("replaying the same confirmation writes nothing more",
                 again.status == "ok" and env.counts()["return_requests"] == 1, "still one return row",
                 f"{env.counts()['return_requests']} row(s)")

    print("== Duplicate return protection")
    oracle = repository.check_return_eligibility("C101", "ORD1001", "IT101", reason_category="standard")["outcome"]
    reply = c101.say(RETURN_REASON)
    picked = c101.pick(reply, "standard") if reply.choice is not None else reply
    report.check("a second return for the same item is refused, no confirmation, no write",
                 oracle == "active_return_exists" and picked.pending is None and env.counts()["return_requests"] == 1,
                 "oracle active_return_exists; no pending action; still one row",
                 f"oracle={oracle}, pending={picked.pending is not None}")

    print("== Customer-scoped return retrieval")
    return_id = row.get("return_id", "")
    listing = c101.say("show my returns")
    c102 = Chat(backend, "C102")
    other = c102.say("show my returns")
    report.check("C101 sees their own return", bool(return_id) and return_id in listing.text,
                 "return ID listed", "return ID not listed")
    report.check("C102 sees no returns and not C101's ID",
                 repository.get_customer_returns("C102") == [] and return_id not in other.text,
                 "no C102 returns; no leak", "leak or unexpected C102 return")

    print("== Ticket proposal, explicit confirmation, decline")
    chat = Chat(backend, "C101")
    asked = chat.say("I want to speak to a human agent")
    base_t = env.counts()
    proposal = chat.say(TICKET_ISSUE)
    report.check("ticket request asks for the issue, then proposes and writes nothing",
                 asked.status == "ok" and asked.pending is None and proposal.status == "needs_confirmation"
                 and proposal.pending is not None and proposal.pending.kind == "support_ticket"
                 and base_t["support_tickets"] == 0 and env.counts() == base_t,
                 "ok then needs_confirmation (support_ticket); no ticket row yet", f"status={proposal.status}")
    done = chat.decide(proposal.pending.action_id)
    tickets = _rows(env, "SELECT ticket_id, customer_id, order_id, issue_type, status, description FROM support_tickets")
    ticket = tickets[0] if tickets else {}
    report.check("confirmation writes exactly one open ticket owned by C101 with the customer's text",
                 len(tickets) == 1 and ticket.get("customer_id") == "C101" and ticket.get("status") == "Open"
                 and str(ticket.get("ticket_id", "")).startswith("TKT_") and TICKET_ISSUE in ticket.get("description", ""),
                 "one TKT_ row: C101, Open, description contains the issue", f"{len(tickets)} row(s)")
    report.check("the reply shows the new ticket ID", bool(ticket) and ticket["ticket_id"] in done.text,
                 "ID in reply", "ID missing from reply")
    decliner = Chat(backend, "C102")
    decliner.say("I want to speak to a human agent")
    second = decliner.say("My delivery for ORD1003 is late and nobody has replied to me.")
    before_decline = env.counts()
    declined = decliner.decide(second.pending.action_id, accepted=False) if second.pending is not None else second
    report.check("declining a ticket proposal writes nothing",
                 second.status == "needs_confirmation" and declined.status == "ok"
                 and env.counts() == before_decline and before_decline["support_tickets"] == 1,
                 "proposal offered; ticket count still 1 after Decline",
                 f"proposal={second.status}, after decline={declined.status}, tickets={env.counts()['support_tickets']}")

    print("== Customer-scoped ticket retrieval")
    ticket_id = ticket.get("ticket_id", "")
    mine = chat.say("show my tickets")
    theirs = c102.say("show my tickets")
    report.check("C101 sees their own ticket", bool(ticket_id) and ticket_id in mine.text,
                 "ticket ID listed", "ticket ID not listed")
    report.check("C102 sees no tickets and not C101's ID",
                 repository.get_customer_tickets("C102") == [] and ticket_id not in theirs.text,
                 "no C102 tickets; no leak", "leak or unexpected C102 ticket")

    print("== Final database state and safety")
    per_customer = _rows(env, "SELECT customer_id, "
                              "(SELECT COUNT(*) FROM return_requests r WHERE r.customer_id = c.customer_id) AS returns, "
                              "(SELECT COUNT(*) FROM support_tickets t WHERE t.customer_id = c.customer_id) AS tickets "
                              "FROM customers c ORDER BY customer_id")
    expected = {"C101": (1, 1)}
    actual = {r["customer_id"]: (r["returns"], r["tickets"]) for r in per_customer if (r["returns"], r["tickets"]) != (0, 0)}
    report.check("final state: only C101 has a return and a ticket, one each", actual == expected,
                 str(expected), str(actual))
    report.check("safety: zero network attempts and only the temporary database was opened",
                 tripwires.network_attempts == 0 and tripwires.sqlite_opened == {_norm(env.db_path)},
                 "0 network attempts; exactly one SQLite file (the temporary database)",
                 f"{tripwires.network_attempts} network attempt(s), {len(tripwires.sqlite_opened)} file(s)")


def main() -> int:
    report = Report()
    tripwires = Tripwires()
    try:
        with tripwires.active():
            with real_eval.environment(RuleBasedLLM) as env:
                run_checks(env, tripwires, report)
    except Exception as exc:  # noqa: BLE001 - report the class only; never print messages that could hold paths
        print(f"\nThe run could not complete ({type(exc).__name__}). Checks so far: {len(report.results)}.")
        return 2
    total = len(report.results)
    passed = total - report.failed
    print(f"\nRESULT: {'PASS' if report.failed == 0 else 'FAIL'} ({passed}/{total} checks passed)")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
