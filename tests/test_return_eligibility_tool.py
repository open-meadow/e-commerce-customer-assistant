"""The read-only return-eligibility tool: asking whether an item can be returned is not asking to return it.

Until now the only way for the model to learn eligibility was to start a return, which asked a customer who had
only asked a question for a reason and a category. ``check_return_eligibility`` exposes the repository's advisory
check as a read tool. These tests pin its contract:

* both windows (standard 168 h, damaged or wrong product 48 h) are judged at ONE instant;
* every time is rendered in UTC by code, so the model never converts a time zone or does date arithmetic;
* the answer says what it cannot see (product returnability, condition, packaging and accessories);
* ownership comes from the session only, nothing is written, and no draft, choice or pending action is created;
* the authoritative recheck at confirmation is untouched: an earlier "open" answer never lets a return through.

Seed facts at the baseline T0 = 2026-11-07T00:00:00Z: ORD1001 (C101, IT101) delivered T0-72h; ORD1005 (C103,
IT105 and IT106) delivered T0-36h; ORD1003 (C102, IT103) delivered T0-240h; ORD1002 (C101, IT102) shipped;
ORD1010 (C105, IT111) returned. Scripted model output only; no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import app.backend  # noqa: F401  (imported first: app.backend and app.orchestrator import each other)
from app.backend import Backend, Settings
from app.db import clock, repository
from app.llm.base import LLMResult, ToolCall
from app.orchestrator.engine import SYSTEM_PROMPT
from app.tools.context import CustomerContext
from app.tools.registry import TOOL_SPECS, _utc_text, execute_tool, llm_tool_definitions
from backend_helpers import Client, ScriptedLLM
from conftest import T0, fingerprint

UTC = timezone.utc
NAME = "check_return_eligibility"


def run(customer, order, item, **extra):
    return execute_tool(CustomerContext(customer, "ref"), NAME, {"order_id": order, "item_id": item, **extra})


def data(result):
    assert result.status == "ok", result
    return json.loads(result.to_json())["data"]


class TestRepositoryWindows:
    def test_both_windows_come_from_one_instant(self, temp_db, monkeypatch):
        calls = []
        real = clock.utc_now
        monkeypatch.setattr(clock, "utc_now", lambda: calls.append(1) or real())
        verdicts = repository.check_return_windows("C101", "ORD1001", "IT101")
        assert len(calls) == 1
        assert verdicts["standard"]["evaluated_at"] == verdicts["damaged"]["evaluated_at"]

    def test_the_standard_and_damaged_windows_differ_by_length(self, temp_db):
        v = repository.check_return_windows("C101", "ORD1001", "IT101")
        assert (v["standard"]["window_hours"], v["damaged"]["window_hours"]) == (168, 48)
        assert v["standard"]["outcome"] == "eligible" and v["damaged"]["outcome"] == "outside_return_window"
        assert v["standard"]["window_closes_at"] == "2026-11-11T00:00:00Z"
        assert v["damaged"]["window_closes_at"] == "2026-11-06T00:00:00Z"

    def test_wrong_product_has_the_same_window_as_damaged(self, temp_db):
        damaged = repository.check_return_eligibility("C101", "ORD1001", "IT101", reason_category="damaged")
        wrong = repository.check_return_eligibility("C101", "ORD1001", "IT101", reason_category="wrong_product")
        keys = ("outcome", "window_hours", "window_closes_at", "delivered_at")
        assert {k: damaged[k] for k in keys} == {k: wrong[k] for k in keys}

    def test_the_window_edge_is_inclusive_to_the_second(self, temp_db, set_now):
        set_now(datetime(2026, 11, 6, 0, 0, 0, tzinfo=UTC))  # exactly when the damaged window closes
        assert repository.check_return_windows("C101", "ORD1001", "IT101")["damaged"]["outcome"] == "eligible"
        set_now(datetime(2026, 11, 6, 0, 0, 1, tzinfo=UTC))
        assert repository.check_return_windows("C101", "ORD1001", "IT101")["damaged"]["outcome"] == \
            "outside_return_window"

    def test_category_independent_outcomes_agree(self, temp_db):
        for customer, order, item, outcome in (("C101", "ORD1002", "IT102", "order_status_ineligible"),
                                               ("C102", "ORD1001", "IT101", "order_not_found"),
                                               ("C101", "ORD1001", "IT999", "item_not_found"),
                                               ("C105", "ORD1010", "IT111", "order_status_ineligible")):
            v = repository.check_return_windows(customer, order, item)
            assert v["standard"]["outcome"] == v["damaged"]["outcome"] == outcome

    def test_it_never_writes(self, temp_db):
        before = fingerprint(temp_db)
        repository.check_return_windows("C101", "ORD1001", "IT101")
        repository.check_return_windows("C103", "ORD1005", "IT105")
        assert fingerprint(temp_db) == before

    @pytest.mark.parametrize("args", [("", "ORD1001", "IT101"), ("C101", "", "IT101"), ("C101", "ORD1001", "")])
    def test_bad_identifiers_are_rejected_before_any_work(self, temp_db, args):
        with pytest.raises(ValueError):
            repository.check_return_windows(*args)

    def test_a_non_string_is_a_type_error(self, temp_db):
        with pytest.raises(TypeError):
            repository.check_return_windows(None, "ORD1001", "IT101")


class TestContract:
    def test_the_schema_has_no_customer_id_and_two_required_ids(self):
        spec = TOOL_SPECS[NAME]
        assert set(spec.properties) == {"order_id", "item_id"} and spec.required == ("order_id", "item_id")
        assert spec.parameters["additionalProperties"] is False
        shown = [t for t in llm_tool_definitions() if t["name"] == NAME][0]
        assert not any("customer" in name.lower() for name in shown["parameters"]["properties"])

    def test_it_is_a_read_tool_with_a_handler(self):
        assert TOOL_SPECS[NAME].handler is not None

    @pytest.mark.parametrize("extra", [{"customer_id": "C102"}, {"reason_category": "damaged"}])
    def test_extra_arguments_are_rejected(self, temp_db, extra):
        assert run("C101", "ORD1001", "IT101", **extra).status == "invalid_input"

    def test_a_missing_item_is_rejected(self, temp_db):
        result = execute_tool(CustomerContext("C101", "ref"), NAME, {"order_id": "ORD1001"})
        assert result.status == "invalid_input"

    def test_a_malformed_id_is_rejected(self, temp_db):
        assert run("C101", "ORD1001'; DROP TABLE orders;--", "IT101").status == "invalid_input"


class TestAnswers:
    def test_standard_open_and_damaged_closed(self, temp_db):
        d = data(run("C101", "ORD1001", "IT101"))
        assert d["result"] == "timing_check"
        std, dmg = d["windows"]["standard"], d["windows"]["damaged_or_wrong_product"]
        assert (std["status"], std["window_hours"], std["hours_left"]) == ("open", 168, 96)
        assert std["closes_at_utc"] == "2026-11-11T00:00:00Z"
        assert std["closes_at_text"] == "11 November 2026, 00:00 UTC"
        assert (dmg["status"], dmg["window_hours"]) == ("closed", 48) and "hours_left" not in dmg
        assert dmg["closes_at_text"] == "6 November 2026, 00:00 UTC"

    def test_both_open(self, temp_db):
        d = data(run("C103", "ORD1005", "IT105"))
        std, dmg = d["windows"]["standard"], d["windows"]["damaged_or_wrong_product"]
        assert (std["status"], std["hours_left"]) == ("open", 132)  # delivered 5 Nov 12:00; closes 12 Nov 12:00
        assert (dmg["status"], dmg["hours_left"]) == ("open", 12)
        assert dmg["closes_at_text"] == "7 November 2026, 12:00 UTC"

    def test_both_closed(self, temp_db):
        d = data(run("C102", "ORD1003", "IT103"))
        assert {w["status"] for w in d["windows"].values()} == {"closed"}

    def test_hours_left_rounds_down(self, temp_db, set_now):
        set_now(T0 + timedelta(minutes=30))
        d = data(run("C103", "ORD1005", "IT105"))
        assert d["windows"]["damaged_or_wrong_product"]["hours_left"] == 11  # 11.5 hours left

    def test_every_time_is_utc_text_made_by_code(self, temp_db):
        d = data(run("C103", "ORD1005", "IT105"))
        texts = [d["delivered_at_text"], d["checked_at_text"],
                 *[w["closes_at_text"] for w in d["windows"].values()]]
        assert all(t.endswith(" UTC") for t in texts)
        assert d["delivered_at_text"] == "5 November 2026, 12:00 UTC"
        assert d["checked_at_text"] == "7 November 2026, 00:00 UTC"

    def test_the_utc_renderer_never_shifts_the_time(self):
        assert _utc_text("2026-12-31T23:59:59Z") == "31 December 2026, 23:59 UTC"
        assert _utc_text("2026-01-05T00:00:00Z") == "5 January 2026, 00:00 UTC"

    def test_what_is_not_checked_is_stated_every_time(self, temp_db):
        d = data(run("C101", "ORD1001", "IT101"))
        joined = " ".join(d["not_checked"]).lower()
        assert len(d["not_checked"]) == 3
        assert "returned at all" in joined and "condition" in joined and "packaging and accessories" in joined

    def test_the_note_separates_a_timing_check_from_an_approval(self, temp_db):
        note = run("C101", "ORD1001", "IT101").note.lower()
        assert "timing check only, not an approval and not a promise" in note
        assert "rechecks everything when the customer confirms" in note
        assert "never convert it to another time zone" in note
        assert "never suggest which reason to choose" in note
        assert "start_return_request only when the customer asks" in note

    def test_a_shipped_order_cannot_start_a_return(self, temp_db):
        d = data(run("C101", "ORD1002", "IT102"))
        assert d["result"] == "cannot_start_a_return" and d["reason"] == "order_status_ineligible"
        assert d["order_status"] == "Shipped" and "windows" not in d

    def test_an_active_return_is_reported_with_its_status(self, temp_db):
        repository.create_return_request("C103", "ORD1005", "IT105", "it overheats", reason_category="standard")
        d = data(run("C103", "ORD1005", "IT105"))
        assert d["reason"] == "active_return_exists" and d["existing_return_status"] == "Requested"

    def test_an_unknown_item_is_reported(self, temp_db):
        assert data(run("C101", "ORD1001", "IT999"))["reason"] == "item_not_found"

    def test_another_customers_order_looks_like_a_missing_order(self, temp_db):
        result = run("C102", "ORD1001", "IT101")
        assert result.status == "not_found" and result.code == "order_not_found"
        body = result.to_json()
        assert "Delivered" not in body and "IT101" not in body and "2026-11" not in body

    def test_the_output_carries_no_contact_details(self, temp_db):
        body = run("C101", "ORD1001", "IT101").to_json().lower()
        assert "@" not in body and "alice" not in body and "phone" not in body


class Sequence(ScriptedLLM):
    """One scripted answer per chat call, declared like a real model."""

    interprets_language = True

    def __init__(self, *answers):
        super().__init__(chat=self._next)
        self.answers = list(answers)
        self.seen: list[list[dict]] = []

    def _next(self, system, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.answers.pop(0)


def ask(order="ORD1001", item="IT101", call_id="e1"):
    return LLMResult(tool_calls=(ToolCall(NAME, {"order_id": order, "item_id": item}, call_id),))


def start_return(order="ORD1001", item="IT101"):
    return LLMResult(tool_calls=(ToolCall("start_return_request", {"order_id": order, "item_id": item}, "r1"),))


def session_of(backend, client):
    return backend.registry.get(client.state._handle)


class TestInTheEngine:
    def test_a_question_creates_no_draft_choice_or_pending_action(self, temp_db, raw):
        llm = Sequence(ask(), LLMResult(text="Based on the delivery date, an ordinary return can be requested "
                                             "until 11 November 2026, 00:00 UTC."))
        backend = Backend(llm=llm, settings=Settings())
        c = Client(backend, "C101")
        reply = c.say("Can I still return my earbuds from ORD1001?")
        s = session_of(backend, c)
        assert reply.status == "ok" and reply.pending is None and reply.choice is None
        assert s.draft is None and s.choice is None and s.pending_id is None and s.ticket_offer is None
        assert raw.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0] == 0
        tool_turns = [json.loads(m["content"]) for m in llm.seen[1] if m["role"] == "tool"]
        assert tool_turns and tool_turns[0]["code"] == "return_check"

    def test_the_model_sees_the_windows_not_a_verdict_to_relay(self, temp_db):
        llm = Sequence(ask(), LLMResult(text="ok"))
        backend = Backend(llm=llm, settings=Settings())
        Client(backend, "C101").say("is ORD1001 earbuds still returnable?")
        seen = [json.loads(m["content"]) for m in llm.seen[1] if m["role"] == "tool"][0]
        assert set(seen["data"]["windows"]) == {"standard", "damaged_or_wrong_product"}
        assert "eligible" not in json.dumps(seen["data"]).lower()

    def test_an_earlier_open_answer_never_lets_a_closed_return_through(self, temp_db, raw, set_now):
        llm = Sequence(ask(), LLMResult(text="An ordinary return can be requested until 11 November, 00:00 UTC."),
                       start_return())
        backend = Backend(llm=llm, settings=Settings())
        c = Client(backend, "C101")
        c.say("Can I return the earbuds from ORD1001?")
        set_now(T0 + timedelta(days=6))  # the standard window has now closed
        reply = c.say("I want to return the earbuds from ORD1001, they keep cutting out")
        s = session_of(backend, c)
        assert "closed" in reply.text and reply.choice is None and reply.pending is None
        assert s.draft is None and raw.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0] == 0

    def test_starting_a_return_still_needs_the_category_and_the_explicit_confirm(self, temp_db, raw):
        llm = Sequence(ask(), LLMResult(text="Until 11 November 2026, 00:00 UTC."), start_return())
        backend = Backend(llm=llm, settings=Settings())
        c = Client(backend, "C101")
        c.say("Can I return the earbuds from ORD1001?")
        menu = c.say("I want to return the earbuds from ORD1001, they keep cutting out")
        assert menu.status == "needs_choice"
        card = c.pick(menu, "standard")
        assert card.status == "needs_confirmation"
        assert raw.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0] == 0
        done = c.confirm(card)
        assert "submitted" in done.text and raw.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0] == 1

    def test_another_customers_order_is_not_found_in_the_engine_too(self, temp_db):
        llm = Sequence(ask(), LLMResult(text="I could not find that order on your account."))
        backend = Backend(llm=llm, settings=Settings())
        Client(backend, "C102").say("can I return ORD1001?")
        seen = [json.loads(m["content"]) for m in llm.seen[1] if m["role"] == "tool"][0]
        assert seen["status"] == "not_found" and "windows" not in json.dumps(seen)


class TestPromptAndHarness:
    def test_the_prompt_separates_the_question_from_the_request(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "check_return_eligibility" in SYSTEM_PROMPT
        assert "a question, not a return request" in lowered
        assert "timing check, not an approval" in lowered
        assert "in utc, without converting them" in lowered
        assert "only when the customer asks to return the item" in lowered

    def test_the_old_begin_a_return_to_find_out_rule_is_gone(self):
        assert "begin the return with start_return_request" not in SYSTEM_PROMPT

    def test_the_limits_of_the_check_are_in_the_prompt(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "packaging and accessories" in lowered and "returned at all" in lowered

    def test_the_evaluation_harness_knows_the_tool(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
        import real_eval as ev
        assert NAME in ev._REAL_TOOLS
