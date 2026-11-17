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
DAMAGED = "damaged_or_wrong_item_reporting_deadline"


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
        std, dmg = d["windows"]["ordinary_return_window"], d["windows"][DAMAGED]
        assert (std["status"], std["window_hours"], std["time_left_text"]) == ("open", 168, "about 4 days")
        assert std["closes_at_utc"] == "2026-11-11T00:00:00Z"
        assert std["closes_at_text"] == "11 November 2026, 00:00 UTC"
        assert (dmg["status"], dmg["window_hours"]) == ("closed", 48) and "time_left_text" not in dmg
        assert dmg["closes_at_text"] == "6 November 2026, 00:00 UTC"

    def test_both_open(self, temp_db):
        d = data(run("C103", "ORD1005", "IT105"))
        std, dmg = d["windows"]["ordinary_return_window"], d["windows"][DAMAGED]
        assert (std["status"], std["time_left_text"]) == ("open", "about 5 days")  # closes 12 Nov 12:00
        assert (dmg["status"], dmg["time_left_text"]) == ("open", "about 12 hours")
        assert dmg["closes_at_text"] == "7 November 2026, 12:00 UTC"

    def test_both_closed(self, temp_db):
        d = data(run("C102", "ORD1003", "IT103"))
        assert {w["status"] for w in d["windows"].values()} == {"closed"}

    def test_time_left_rounds_down_and_is_text_not_a_bare_number(self, temp_db, set_now):
        set_now(T0 + timedelta(minutes=30))
        d = data(run("C103", "ORD1005", "IT105"))
        view = d["windows"][DAMAGED]
        assert view["time_left_text"] == "about 11 hours"  # 11.5 hours left
        assert "hours_left" not in view

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
        assert "photo" not in joined  # photos are raised only when the customer reports damage

    def test_the_note_separates_a_timing_check_from_an_approval(self, temp_db):
        note = run("C101", "ORD1001", "IT101").note.lower()
        assert "timing check only, not an approval and not a promise" in note
        assert "rechecks everything when the customer confirms" in note
        assert "never convert it to another time zone" in note
        assert "never suggest which reason to choose" in note
        assert "start_return_request only when the customer asks" in note
        assert "photos" in note

    def test_a_shipped_order_cannot_start_a_return(self, temp_db):
        d = data(run("C101", "ORD1002", "IT102"))
        assert d["result"] == "cannot_start_a_return" and d["reason"] == "order_status_ineligible"
        assert d["order_status"] == "Shipped" and "windows" not in d
        assert d["explanation"] == "Returns can only be started for delivered orders, and this order's status is Shipped."

    def test_an_active_return_is_reported_with_its_status(self, temp_db):
        repository.create_return_request("C103", "ORD1005", "IT105", "it overheats", reason_category="standard")
        d = data(run("C103", "ORD1005", "IT105"))
        assert d["reason"] == "active_return_exists" and d["existing_return_status"] == "Requested"
        assert d["explanation"] == "A return request for this item already exists (status: Requested)."

    def test_an_unknown_item_is_reported(self, temp_db):
        d = data(run("C101", "ORD1001", "IT999"))
        assert d["result"] == "item_not_on_this_order" and d["reason"] == "item_not_found"
        assert d["order_items"] == [{"item_id": "IT101", "product_name": "Wireless Earbuds"}]
        assert d["explanation"] == ("That item ID is not on this order. The items on this order are: "
                                    "IT101 (Wireless Earbuds).")

    def test_another_customers_order_looks_like_a_missing_order(self, temp_db):
        result = run("C102", "ORD1001", "IT101")
        assert result.status == "not_found" and result.code == "order_not_found"
        body = result.to_json()
        assert "Delivered" not in body and "IT101" not in body and "2026-11" not in body

    def test_the_output_carries_no_contact_details(self, temp_db):
        body = run("C101", "ORD1001", "IT101").to_json().lower()
        assert "@" not in body and "alice" not in body and "phone" not in body


class TestTwoDifferentPeriods:
    """Policies.md: most products 7 days from delivery. FAQ Q3.4: report a damaged or wrong item within 48 hours."""

    def test_the_two_periods_have_different_names_and_meanings(self, temp_db):
        windows = data(run("C101", "ORD1001", "IT101"))["windows"]
        ordinary, deadline = windows["ordinary_return_window"], windows[DAMAGED]
        assert ordinary["window_hours"] == 168 and "7 days from delivery" in ordinary["meaning"]
        assert deadline["window_hours"] == 48 and "report a damaged or wrong item" in deadline["meaning"]
        assert "48 hours from delivery" in deadline["meaning"] and "reporting" not in ordinary["meaning"]

    def test_the_note_names_the_reporting_deadline_and_the_photo_limit(self, temp_db):
        note = run("C101", "ORD1001", "IT101").note
        assert "facts_to_state" in note and "never suggest which reason to choose" in note
        assert "never raise photos unless the customer says the item arrived damaged or wrong" in note.lower()
        assert "never say how to send photos" in note.lower()
        assert "do not mention the ordinary period" in note

    def test_the_photo_note_is_only_on_the_damaged_or_wrong_item_deadline(self, temp_db):
        windows = data(run("C101", "ORD1001", "IT101"))["windows"]
        assert "clear photos" in windows[DAMAGED]["photos_note"]
        assert "cannot collect photos" in windows[DAMAGED]["photos_note"]
        assert "photos_note" not in windows["ordinary_return_window"]

    def test_a_blocked_answer_carries_no_photo_text(self, temp_db):
        blocked = run("C101", "ORD1002", "IT102")
        assert "photo" not in blocked.to_json().lower() and "not_checked" not in json.loads(blocked.to_json())["data"]


class TestTimeLeft:
    @pytest.mark.parametrize("seconds, text", [
        (0, "less than 1 hour"), (1, "less than 1 hour"), (3599, "less than 1 hour"), (3600, "about 1 hour"),
        (7199, "about 1 hour"), (7200, "about 2 hours"), (47 * 3600 + 3599, "about 47 hours"),
        (48 * 3600, "about 2 days"), (156 * 3600, "about 6 days")])
    def test_the_wording_never_reads_as_expired(self, seconds, text):
        from app.tools.registry import _time_left_text
        assert _time_left_text(seconds) == text

    def test_an_open_window_in_its_last_hour_stays_open_with_the_exact_time(self, temp_db, set_now):
        set_now(datetime(2026, 11, 7, 11, 30, tzinfo=UTC))  # the ORD1005 reporting deadline closes at 12:00
        view = data(run("C103", "ORD1005", "IT105"))["windows"][DAMAGED]
        assert view["status"] == "open" and view["time_left_text"] == "less than 1 hour"
        assert view["closes_at_text"] == "7 November 2026, 12:00 UTC"

    def test_the_inclusive_boundary_is_preserved(self, temp_db, set_now):
        set_now(datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC))  # exactly at the closing instant
        view = data(run("C103", "ORD1005", "IT105"))["windows"][DAMAGED]
        assert view["status"] == "open" and view["time_left_text"] == "less than 1 hour"
        set_now(datetime(2026, 11, 7, 12, 0, 1, tzinfo=UTC))  # one second later
        assert data(run("C103", "ORD1005", "IT105"))["windows"][DAMAGED]["status"] == "closed"


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
        assert set(seen["data"]["windows"]) == {"ordinary_return_window", DAMAGED}
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
        assert "three different intents" in lowered
        assert "has made a return request: call start_return_request" in lowered
        assert "do not run an eligibility check first" in lowered
        assert "has asked a question: call check_return_eligibility" in lowered
        assert "is asking about its status: call get_my_returns" in lowered
        assert "never use start_return_request for (2) or (3)" in lowered
        assert "timing check, not an approval" in lowered

    def test_the_old_begin_a_return_to_find_out_rule_is_gone(self):
        assert "begin the return with start_return_request" not in SYSTEM_PROMPT

    def test_the_limits_and_the_photo_wording_live_in_the_tool_not_the_prompt(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "photo" not in lowered and "packaging" not in lowered
        assert "never say the item is eligible or that a return will be accepted" in lowered

    def test_the_prompt_gives_the_paragraph_as_written_so_the_model_does_not_translate(self):
        assert "facts_to_state paragraph that matches what they said, as written" in SYSTEM_PROMPT

    def test_the_evaluation_harness_knows_the_tool(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
        import real_eval as ev
        assert NAME in ev._REAL_TOOLS


class TestFactsToState:
    """The paragraphs the model is asked to give are built by code, from the same verdicts as the windows."""

    def facts(self, customer, order, item):
        return data(run(customer, order, item))["facts_to_state"]

    def test_there_is_one_paragraph_per_kind_of_customer_message(self, temp_db):
        facts = self.facts("C101", "ORD1001", "IT101")
        assert set(facts) == {"no_reason_given", "item_damaged_or_wrong", "any_other_reason"}
        assert all(isinstance(v, str) for v in facts.values())

    def test_no_reason_given_has_the_ordinary_period_and_the_limits_and_not_a_closed_deadline(self, temp_db):
        paragraph = self.facts("C101", "ORD1001", "IT101")["no_reason_given"]
        assert ("The ordinary return period (7 days from delivery) is open until 11 November 2026, 00:00 UTC "
                "(about 4 days left).") in paragraph
        assert "deadline to report" not in paragraph and "6 November" not in paragraph  # closed and not asked about
        assert "cannot check whether this product can be returned at all" in paragraph
        assert "condition" in paragraph and "packaging and accessories" in paragraph

    def test_no_reason_given_mentions_the_deadline_only_while_it_is_open(self, temp_db):
        paragraph = self.facts("C103", "ORD1005", "IT105")["no_reason_given"]
        assert paragraph.index("ordinary return period") < paragraph.index("deadline to report")
        assert "is open until 7 November 2026, 12:00 UTC (about 12 hours left)" in paragraph

    def test_a_damaged_report_with_the_deadline_closed_gets_the_deadline_and_support_but_no_photos(self, temp_db):
        paragraph = self.facts("C101", "ORD1001", "IT101")["item_damaged_or_wrong"]
        assert "closed on 6 November 2026, 00:00 UTC" in paragraph
        assert "support ticket" in paragraph and "can't promise any particular outcome" in paragraph
        assert "photo" not in paragraph  # photos cannot restore a closed deadline and are not implied to
        assert "ordinary" not in paragraph and "7 days" not in paragraph  # never steers to the other period

    def test_a_damaged_report_with_the_deadline_open_gets_the_photo_sentence(self, temp_db):
        paragraph = self.facts("C103", "ORD1005", "IT106")["item_damaged_or_wrong"]
        assert "is open until 7 November 2026, 12:00 UTC" in paragraph
        assert "clear photos" in paragraph and "cannot collect photos here" in paragraph and "support ticket" in paragraph
        assert "ordinary" not in paragraph and "7 days" not in paragraph

    def test_any_other_reason_gets_the_ordinary_period_and_the_limits_without_photos(self, temp_db):
        paragraph = self.facts("C101", "ORD1001", "IT101")["any_other_reason"]
        assert "ordinary return period" in paragraph and "photo" not in paragraph and "48" not in paragraph

    def test_no_other_paragraph_mentions_photos_except_the_damaged_one(self, temp_db):
        facts = self.facts("C103", "ORD1005", "IT105")
        assert "photo" in facts["item_damaged_or_wrong"]
        assert "photo" not in facts["no_reason_given"] and "photo" not in facts["any_other_reason"]

    def test_both_open_and_both_closed_read_correctly(self, temp_db):
        both_open = self.facts("C103", "ORD1005", "IT105")["no_reason_given"]
        assert "is open until 12 November 2026, 12:00 UTC (about 5 days left)" in both_open
        assert "is open until 7 November 2026, 12:00 UTC (about 12 hours left)" in both_open
        both_closed = self.facts("C102", "ORD1003", "IT103")["no_reason_given"]
        assert "closed on 4 November 2026, 00:00 UTC" in both_closed
        assert "30 October" not in both_closed  # nothing was said about damage
        assert "support ticket" in both_closed and "photo" not in both_closed
        assert "cannot check whether" not in both_closed  # nothing can be returned, so the limits are moot

    def test_the_last_hour_reads_as_less_than_one_hour_not_zero(self, temp_db, set_now):
        set_now(datetime(2026, 11, 7, 11, 30, tzinfo=UTC))
        paragraph = self.facts("C103", "ORD1005", "IT105")["no_reason_given"]
        assert "(less than 1 hour left)" in paragraph and "0 hours" not in paragraph

    def test_every_time_in_the_facts_is_utc_text(self, temp_db):
        for paragraph in self.facts("C101", "ORD1001", "IT101").values():
            assert paragraph.count(" UTC") >= 1

    def test_the_note_asks_for_the_whole_paragraph(self, temp_db):
        note = run("C101", "ORD1001", "IT101").note
        assert "three ready paragraphs" in note and "give all of it" in note and "do not leave a sentence out" in note


class TestIntentSeparationWording:
    """Live run 1: the model called the eligibility tool for 'I want to return the earbuds ...'."""

    def test_the_tool_description_forbids_use_for_a_return_request(self):
        description = TOOL_SPECS[NAME].description
        assert "QUESTION" in description and "Never use it when the customer says they want to return" in description
        assert "start_return_request" in description and "checks eligibility itself" in description

    def test_the_return_tool_description_still_says_the_customer_chooses_the_reason(self):
        assert "reason category" in TOOL_SPECS["start_return_request"].description

    def test_a_refund_status_of_not_applicable_says_nothing_about_returns(self, temp_db):
        note = execute_tool(CustomerContext("C101", "ref"), "get_refund_policy", {"order_id": "ORD1001"}).note
        assert "'Not Applicable'" in note and "says nothing about whether a refund or a return is possible" in note


class TestWrongItemRecovery:
    """Live run 2: the model called the tool with a guessed item ID and then told the customer the item 'cannot be
    found'. The tool now answers with the order's own items so the model can call it again with the right one."""

    def test_a_wrong_item_lists_the_customers_own_items_with_names(self, temp_db):
        d = data(run("C103", "ORD1005", "IT999"))
        assert [i["item_id"] for i in d["order_items"]] == ["IT105", "IT106"]
        assert {i["product_name"] for i in d["order_items"]} == {"24-inch Monitor (Full HD)", "65W GaN Charger"}
        assert "IT105 (24-inch Monitor (Full HD)), IT106 (65W GaN Charger)" in d["explanation"]

    def test_the_note_asks_for_one_retry_and_forbids_saying_the_item_cannot_be_found(self, temp_db):
        note = run("C103", "ORD1005", "IT999").note
        assert "Call this tool again, once" in note and "Do not tell the customer the item cannot be found" in note
        assert "if it is not clear which one, ask the customer" in note

    def test_another_customers_order_still_lists_nothing(self, temp_db):
        result = run("C102", "ORD1005", "IT105")
        assert result.status == "not_found" and "order_items" not in result.to_json()
        assert "IT105" not in result.to_json() and "Monitor" not in result.to_json()

    def test_a_wrong_item_on_a_returned_or_shipped_order_is_still_blocked_first(self, temp_db):
        # ordering of the outcomes is the repository's: the order status is judged before the item
        assert data(run("C101", "ORD1002", "IT999"))["reason"] == "order_status_ineligible"

    def test_the_model_can_retry_with_the_right_item_and_then_answers_from_the_facts(self, temp_db):
        llm = Sequence(ask("ORD1005", "IT999"), ask("ORD1005", "IT106"),
                       LLMResult(text="The ordinary return period (7 days from delivery) is open until "
                                      "12 November 2026, 12:00 UTC."))
        backend = Backend(llm=llm, settings=Settings())
        reply = Client(backend, "C103").say("Is it too late to return something from ORD1005?")  # no item named: code cannot resolve it
        assert reply.status == "ok" and "12 November 2026, 12:00 UTC" in reply.text
        assert reply.pending is None and reply.choice is None
        seen = [json.loads(m["content"]) for m in llm.seen[2] if m["role"] == "tool"]
        assert [s["data"]["result"] for s in seen] == ["item_not_on_this_order", "timing_check"]

