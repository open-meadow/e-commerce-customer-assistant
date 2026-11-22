"""Phase 2: the fresh-description write path, idempotency, duplicate detection, recovery, events and erasure."""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.db import repository
from app.db.connection import get_connection
from app.db.errors import DatabaseOperationalError, DatabaseOutcomeUnknownError
from phase2_support import CommitProxy, an_item, force_status

UTC = timezone.utc
T0 = datetime(2026, 11, 7, tzinfo=UTC)
DAY = 86400


def create(key, description="screen cracked", issue="general", customer="C101", **kw):
    return repository.create_ticket_from_description(customer, issue, description, key, **kw)


def rows(raw, sql, params=()):
    return [dict(r) for r in raw.execute(sql, params)]


class TestFreshCreation:
    def test_creates_a_description_only_ticket_with_a_null_summary_and_a_created_event(self, temp_db, raw):
        result = create("rk-1", "My earbuds stopped working")
        assert result["outcome"] == "created" and result["ticket_id"].startswith("TKT_")
        ticket = rows(raw, "SELECT * FROM support_tickets")[0]
        assert ticket["conversation_summary"] is None and ticket["description"] == "My earbuds stopped working"
        assert ticket["status"] == "Open" and ticket["priority"] == "Normal" and ticket["request_key"] == "rk-1"
        assert ticket["issue_key"] == "n:" + ticket["ticket_id"] and ticket["updated_at"] == ticket["created_at"]
        events = rows(raw, "SELECT kind, actor_type, actor_ref, seq, request_key FROM ticket_events")
        assert events == [{"kind": "created", "actor_type": "customer", "actor_ref": "customer:C101", "seq": 1,
                           "request_key": "rk-1"}]

    def test_no_summary_is_generated_or_marked_anywhere(self, temp_db, raw):
        create("rk-1")
        assert raw.execute("SELECT COUNT(*) FROM support_tickets WHERE conversation_summary IS NOT NULL").fetchone()[0] == 0

    @pytest.mark.parametrize("text", [
        "  leading and trailing  ", "line1\nline2\r\nline3\ttab", "*Markdown* and <b>HTML</b> &amp; stuff",
        " no-break ", "उत्पाद खराब", "emoji \U0001F600 astral",
        "उ" * 2000, "\U0001F600" * 2000, "a" * 2000])
    def test_the_description_is_stored_exactly(self, temp_db, raw, text):
        create("rk-" + str(abs(hash(text))), text)
        assert raw.execute("SELECT description FROM support_tickets").fetchone()[0] == text

    def test_2001_code_points_are_rejected_unmodified_and_nothing_is_written(self, temp_db, raw):
        with pytest.raises(ValueError):
            create("rk-1", "a" * 2001)
        with pytest.raises(ValueError):
            create("rk-2", "\U0001F600" * 2001)
        for bad in ("", "  \t\n", "a\x00b"):
            with pytest.raises(ValueError):
                create("rk-3", bad)
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM ticket_write_intents").fetchone()[0] == 0

    def test_argument_validation(self, temp_db):
        for bad_key in ("", "k" * 201, "a\x00b"):
            with pytest.raises(ValueError):
                create(bad_key)
        with pytest.raises(TypeError):
            create(5)
        with pytest.raises(ValueError):
            create("rk", item_id="IT1")  # an item needs its order
        with pytest.raises(ValueError):
            create("rk", issue="refund")

    def test_the_intent_is_persisted_with_the_immutable_payload_and_marked_committed(self, temp_db, raw):
        result = create("rk-1", "payload text", "payment", order_id="ORD1001")
        intent = rows(raw, "SELECT * FROM ticket_write_intents")[0]
        payload = json.loads(intent["payload_json"])
        assert payload == {"description": "payload text", "issue_type": "payment", "item_id": None,
                           "order_id": "ORD1001", "parent_ticket_id": None}
        assert intent["request_hash"] == hashlib.sha256(intent["payload_json"].encode()).hexdigest()
        assert intent["state"] == "committed" and intent["ticket_id"] == result["ticket_id"]
        ticket = rows(raw, "SELECT request_hash FROM support_tickets")[0]
        assert ticket["request_hash"] == intent["request_hash"]


class TestOwnership:
    def test_an_owned_order_is_linked_and_an_unowned_one_is_silently_null(self, temp_db, raw):
        create("rk-1", order_id="ORD1001")
        create("rk-2", customer="C102", order_id="ORD1001")  # not C102's order: same result, nothing disclosed
        got = rows(raw, "SELECT customer_id, order_id FROM support_tickets ORDER BY customer_id")
        assert got == [{"customer_id": "C101", "order_id": "ORD1001"}, {"customer_id": "C102", "order_id": None}]
        assert create("rk-3", customer="C102", order_id="ORD9999")["outcome"] == "created"

    def test_an_item_must_belong_to_the_customers_order(self, temp_db, raw):
        item = an_item("ORD1001")
        assert create("rk-1", issue="product", order_id="ORD1001", item_id=item)["outcome"] == "created"
        assert create("rk-2", issue="product", order_id="ORD1001", item_id="IT_NOT_THERE")["outcome"] == "item_not_found"
        assert create("rk-3", issue="product", customer="C102", order_id="ORD1001", item_id=item)["outcome"] == "item_not_found"
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1

    def test_a_parent_ticket_must_belong_to_the_same_customer(self, temp_db, raw):
        first = create("rk-1")["ticket_id"]
        assert create("rk-2", parent_ticket_id=first)["outcome"] == "created"
        assert create("rk-3", customer="C102", parent_ticket_id=first)["outcome"] == "parent_not_found"


class TestIdempotency:
    def test_the_same_key_and_payload_returns_the_same_ticket_and_writes_nothing(self, temp_db, raw):
        first = create("rk-1", "same")
        again = create("rk-1", "same")
        assert again == {"outcome": "already_created", "ticket_id": first["ticket_id"]}
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1
        assert raw.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0] == 1

    def test_the_same_key_with_another_payload_is_a_conflict(self, temp_db, raw):
        create("rk-1", "same")
        assert create("rk-1", "edited")["outcome"] == "idempotency_conflict"
        assert create("rk-1", "same", issue="payment")["outcome"] == "idempotency_conflict"
        assert raw.execute("SELECT description FROM support_tickets").fetchone()[0] == "same"

    def test_a_retry_gets_the_original_answer_even_at_the_rate_limit(self, temp_db, raw):
        first = create("rk-1", rate_limit=(1, DAY))
        assert create("rk-2", "other", rate_limit=(1, DAY))["outcome"] == "rate_limited"
        assert create("rk-1", rate_limit=(1, DAY)) == {"outcome": "already_created", "ticket_id": first["ticket_id"]}

    def test_the_key_is_scoped_to_the_customer(self, temp_db, raw):
        assert create("shared", "a", customer="C101")["outcome"] == "created"
        assert create("shared", "a", customer="C102")["outcome"] == "created"

    def test_the_hash_ignores_the_customer_and_covers_exactly_the_five_fields(self, temp_db, raw):
        create("rk-1", "d", "payment", order_id="ORD1001")
        h = raw.execute("SELECT request_hash FROM support_tickets").fetchone()[0]
        expected = json.dumps({"description": "d", "issue_type": "payment", "item_id": None, "order_id": "ORD1001",
                               "parent_ticket_id": None}, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        assert h == hashlib.sha256(expected.encode()).hexdigest()


class TestDuplicateDetection:
    def test_a_new_key_for_an_active_issue_reports_the_existing_ticket_and_writes_nothing(self, temp_db, raw):
        item = an_item("ORD1001")
        first = create("rk-1", "defective", "product", order_id="ORD1001", item_id=item)
        second = create("rk-2", "defective again", "product", order_id="ORD1001", item_id=item)
        assert second == {"outcome": "duplicate_active", "ticket_id": first["ticket_id"], "status": "Open"}
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1
        assert raw.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0] == 1  # no comment, no addition offered
        intent = rows(raw, "SELECT state, last_outcome FROM ticket_write_intents WHERE request_key = 'rk-2'")[0]
        assert intent == {"state": "prepared", "last_outcome": "duplicate_active"}
        assert repository.list_unresolved_ticket_requests("C101") == []  # a definite outcome is not "unresolved"

    def test_duplicate_detection_is_independent_of_idempotency(self, temp_db):
        item = an_item("ORD1001")
        first = create("rk-1", "x", "product", order_id="ORD1001", item_id=item)
        assert create("rk-1", "x", "product", order_id="ORD1001", item_id=item)["outcome"] == "already_created"
        assert create("rk-2", "x", "product", order_id="ORD1001", item_id=item)["outcome"] == "duplicate_active"
        assert create("rk-2", "different text", "product", order_id="ORD1001", item_id=item)["outcome"] == "idempotency_conflict"
        assert first["outcome"] == "created"

    def test_the_duplicate_is_reported_even_to_a_rate_limited_customer(self, temp_db):
        item = an_item("ORD1001")
        first = create("rk-1", "x", "product", order_id="ORD1001", item_id=item, rate_limit=(1, DAY))
        again = create("rk-2", "x", "product", order_id="ORD1001", item_id=item, rate_limit=(1, DAY))
        assert again["outcome"] == "duplicate_active" and again["ticket_id"] == first["ticket_id"]

    def test_scope_is_customer_category_and_item(self, temp_db):
        item = an_item("ORD1001")
        create("rk-1", "x", "product", order_id="ORD1001", item_id=item)
        assert create("rk-2", "x", "return", order_id="ORD1001", item_id=item)["outcome"] == "created"   # another category
        assert create("rk-3", "x", "payment", order_id="ORD1001")["outcome"] == "created"                # another category
        assert create("rk-4", "x", "payment", order_id="ORD1001")["outcome"] == "duplicate_active"      # same order category
        assert create("rk-5", "x", "product", customer="C102")["outcome"] == "created"                   # another customer

    def test_order_less_and_general_tickets_are_never_merged(self, temp_db, raw):
        assert create("rk-1", "same words")["outcome"] == "created"
        assert create("rk-2", "same words")["outcome"] == "created"
        assert create("rk-3", "x", "payment")["outcome"] == "created"  # no verified order: an n: key
        assert create("rk-4", "x", "payment")["outcome"] == "created"
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 4

    def test_a_terminal_ticket_does_not_block_a_new_one(self, temp_db, raw):
        item = an_item("ORD1001")
        first = create("rk-1", "x", "product", order_id="ORD1001", item_id=item)
        force_status(temp_db, first["ticket_id"], "Closed")
        assert create("rk-2", "x", "product", order_id="ORD1001", item_id=item)["outcome"] == "created"


class TestRateLimitAtomicity:
    def test_the_count_and_the_insert_share_one_transaction_and_the_window_is_strict(self, temp_db, set_now):
        set_now(T0)
        for n in range(2):
            assert create(f"a{n}", rate_limit=(2, DAY))["outcome"] == "created"
        blocked = create("b", rate_limit=(2, DAY))
        assert blocked["outcome"] == "rate_limited" and blocked["retry_at"] == "2026-11-08T00:00:00Z"
        set_now(T0 + timedelta(seconds=DAY - 1))
        assert create("c", rate_limit=(2, DAY))["outcome"] == "rate_limited"
        set_now(T0 + timedelta(seconds=DAY))  # a row exactly one window old no longer counts
        assert create("d", rate_limit=(2, DAY))["outcome"] == "created"

    def test_a_rate_limited_request_can_be_retried_later_with_the_same_key(self, temp_db, raw, set_now):
        set_now(T0)
        create("a", rate_limit=(1, DAY))
        assert create("b", "waiting", rate_limit=(1, DAY))["outcome"] == "rate_limited"
        assert rows(raw, "SELECT state, last_outcome FROM ticket_write_intents WHERE request_key = 'b'")[0] == {
            "state": "prepared", "last_outcome": "rate_limited"}
        set_now(T0 + timedelta(seconds=DAY + 1))
        assert create("b", "waiting", rate_limit=(1, DAY))["outcome"] == "created"
        assert rows(raw, "SELECT state, last_outcome FROM ticket_write_intents WHERE request_key = 'b'")[0] == {
            "state": "committed", "last_outcome": None}

    def test_rate_limited_requests_do_not_count_toward_the_limit(self, temp_db, raw):
        create("a", rate_limit=(1, DAY))
        for n in range(5):
            create(f"x{n}", rate_limit=(1, DAY))
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1

    def test_concurrent_creations_cannot_both_pass_the_check(self, temp_db, raw):
        results: list[str] = []
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            try:
                results.append(create(f"t{n}", f"text {n}", rate_limit=(3, DAY))["outcome"])
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert sorted(results) == ["created"] * 3 + ["rate_limited"] * 7
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 3

    def test_the_legacy_and_the_fresh_path_share_one_count(self, temp_db, raw):
        repository.create_support_ticket("C101", "general", "legacy d", "legacy s", rate_limit=(2, DAY))
        assert create("a", rate_limit=(2, DAY))["outcome"] == "created"
        assert create("b", rate_limit=(2, DAY))["outcome"] == "rate_limited"
        assert repository.create_support_ticket("C101", "general", "d", "s", rate_limit=(2, DAY))["outcome"] == "rate_limited"


class TestLegacyPathKeepsItsSemantics:
    def test_signature_summary_and_row_are_unchanged_and_the_new_columns_are_filled(self, temp_db, raw):
        result = repository.create_support_ticket("C101", "payment", "charged twice", "Customer Issue\ncharged twice", "ORD1001")
        row = rows(raw, "SELECT * FROM support_tickets")[0]
        assert row["conversation_summary"] == "Customer Issue\ncharged twice" and row["description"] == "charged twice"
        assert row["request_key"] == "legacy:" + result["ticket_id"] and row["request_hash"] == "legacy"
        assert row["issue_key"] == "n:" + result["ticket_id"]
        assert rows(raw, "SELECT kind FROM ticket_events") == [{"kind": "created"}]

    def test_legacy_creations_are_never_deduplicated(self, temp_db, raw):
        for _ in range(3):
            repository.create_support_ticket("C101", "payment", "d", "s", "ORD1001")
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 3


class TestUncertainOutcomeAndDurableRecovery:
    def patch_connection(self, monkeypatch, *, commit_first, only_when):
        real = repository.get_connection
        state = {"armed": True}

        def get_connection_proxy():
            connection = real()
            if state["armed"] and only_when():
                state["armed"] = False
                return CommitProxy(connection, commit_first=commit_first)
            return connection

        monkeypatch.setattr(repository, "get_connection", get_connection_proxy)
        state["restore"] = lambda: monkeypatch.setattr(repository, "get_connection", real)
        return state

    def ticket_attempt(self):
        # the insert attempt is the second write transaction of a creation (the first persists the intent)
        calls = {"n": 0}

        def only_when():
            calls["n"] += 1
            return calls["n"] == 2
        return only_when

    def test_a_commit_that_succeeded_but_whose_answer_was_lost_is_recovered_without_a_duplicate(self, temp_db, raw, monkeypatch):
        self.patch_connection(monkeypatch, commit_first=True, only_when=self.ticket_attempt())
        with pytest.raises(DatabaseOutcomeUnknownError):
            create("rk-1", "committed but unanswered")
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1  # it really committed
        again = create("rk-1", "committed but unanswered")
        assert again["outcome"] == "already_created"
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1

    def test_a_commit_that_failed_leaves_a_recoverable_unresolved_intent_and_a_retry_creates_one_ticket(self, temp_db, raw, monkeypatch):
        self.patch_connection(monkeypatch, commit_first=False, only_when=self.ticket_attempt())
        with pytest.raises(DatabaseOutcomeUnknownError):
            create("rk-1", "never committed")
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0
        pending = repository.list_unresolved_ticket_requests("C101")
        assert [p["request_key"] for p in pending] == ["rk-1"] and pending[0]["payload"]["description"] == "never committed"
        assert repository.get_ticket_request_state("C101", "rk-1")["state"] == "prepared"
        result = repository.retry_ticket_request("C101", "rk-1")  # the stored key and the stored immutable payload
        assert result["outcome"] == "created"
        assert repository.list_unresolved_ticket_requests("C101") == []
        assert repository.get_ticket_request_state("C101", "rk-1") == {
            "state": "committed", "last_outcome": None, "ticket_id": result["ticket_id"]}
        assert repository.retry_ticket_request("C101", "rk-1") == {"outcome": "already_created", "ticket_id": result["ticket_id"]}
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1

    def test_an_error_before_the_commit_is_definite_and_nothing_is_written(self, temp_db, raw, monkeypatch):
        def failing_event(*a, **k):
            raise sqlite3.OperationalError("disk full (simulated, before the commit)")
        real_insert_event = repository._insert_event
        monkeypatch.setattr(repository, "_insert_event", failing_event)
        with pytest.raises(DatabaseOperationalError):
            create("rk-1", "x")
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0] == 0
        monkeypatch.setattr(repository, "_insert_event", real_insert_event)
        assert repository.retry_ticket_request("C101", "rk-1")["outcome"] == "created"  # the intent survived

    def test_recovery_survives_a_simulated_restart(self, temp_db, raw, monkeypatch):
        """Nothing is kept in process memory: a "new process" only needs the database path."""
        state = self.patch_connection(monkeypatch, commit_first=False, only_when=self.ticket_attempt())
        with pytest.raises(DatabaseOutcomeUnknownError):
            create("rk-1", "survives a restart")
        state["restore"]()
        # the recovery functions below read only the database (nothing about the request is kept in process memory)
        pending = repository.list_unresolved_ticket_requests("C101")
        assert pending and repository.retry_ticket_request("C101", pending[0]["request_key"])["outcome"] == "created"

    def test_recovery_is_scoped_to_the_customer(self, temp_db, raw, monkeypatch):
        state = self.patch_connection(monkeypatch, commit_first=False, only_when=self.ticket_attempt())
        with pytest.raises(DatabaseOutcomeUnknownError):
            create("rk-1", "c101 only")
        state["restore"]()
        assert repository.list_unresolved_ticket_requests("C102") == []
        assert repository.retry_ticket_request("C102", "rk-1") == {"outcome": "unknown_request"}
        assert repository.get_ticket_request_state("C102", "rk-1") == {"state": "unknown"}

    def test_the_stored_payload_is_final(self, temp_db, raw):
        create("rk-1", "original", rate_limit=(1, DAY))
        create("rk-2", "original two", rate_limit=(1, DAY))  # rate limited: the intent stays prepared
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE ticket_write_intents SET payload_json = '{}' WHERE request_key = 'rk-2'")
        raw.rollback()  # the aborted statement leaves this connection's transaction (and its write lock) open
        assert create("rk-2", "changed text", rate_limit=(1, DAY))["outcome"] == "idempotency_conflict"

    def test_a_committed_retry_after_a_definite_refusal_clears_the_refusal(self, temp_db, raw, set_now):
        set_now(T0)
        create("a", rate_limit=(1, DAY))
        create("b", "later", rate_limit=(1, DAY))
        assert repository.list_unresolved_ticket_requests("C101") == []  # a definite refusal is not unresolved
        set_now(T0 + timedelta(seconds=DAY))
        assert repository.retry_ticket_request("C101", "b", rate_limit=(1, DAY))["outcome"] == "created"


class TestAddTicketEvent:
    def make(self, temp_db, **kw):
        item = an_item("ORD1001")
        return create("create-" + kw.pop("key", "1"), "defective", "product", order_id="ORD1001", item_id=item, **kw)["ticket_id"]

    def add(self, ticket_id, key, body="more info", intent="information", customer="C101", **kw):
        return repository.add_ticket_event(customer, ticket_id, key, body, intent, **kw)

    def test_waiting_for_customer_plus_information_becomes_in_progress_with_a_full_audit_event(self, temp_db, raw, set_now):
        set_now(T0)
        tid = self.make(temp_db)
        force_status(temp_db, tid, "Waiting for customer")
        set_now(T0 + timedelta(minutes=5))
        result = self.add(tid, "add-1", "here is the invoice")
        assert result["outcome"] == "added" and result["status"] == "In Progress"
        assert result["transition"] == {"from": "Waiting for customer", "to": "In Progress"}
        events = rows(raw, "SELECT * FROM ticket_events ORDER BY seq")
        comment, change = events[1], events[2]
        assert comment["kind"] == "comment" and comment["intent"] == "information" and comment["body"] == "here is the invoice"
        assert change["kind"] == "status_change" and change["actor_type"] == "system" and change["actor_ref"] == "system"
        assert (change["from_status"], change["to_status"]) == ("Waiting for customer", "In Progress")
        assert change["transition_reason"] == "customer_supplied_requested_information"
        assert change["trigger_event_id"] == comment["event_id"] and change["request_key"] == "add-1#status"
        assert change["created_at"] == "2026-11-07T00:05:00Z"  # the server clock, never a client value
        ticket = rows(raw, "SELECT status, updated_at, description FROM support_tickets")[0]
        assert ticket == {"status": "In Progress", "updated_at": "2026-11-07T00:05:00Z", "description": "defective"}

    def test_the_original_description_is_never_touched(self, temp_db, raw):
        tid = self.make(temp_db)
        self.add(tid, "add-1", "a comment that looks like a new description")
        assert raw.execute("SELECT description FROM support_tickets").fetchone()[0] == "defective"

    @pytest.mark.parametrize("status,expect_review", [("Open", False), ("In Progress", False), ("On hold", True)])
    def test_information_leaves_these_statuses_unchanged(self, temp_db, raw, status, expect_review):
        tid = self.make(temp_db)
        force_status(temp_db, tid, status)
        result = self.add(tid, "add-1")
        assert result["outcome"] == "added" and result["status"] == status and result["transition"] is None
        assert result["staff_review_required"] is expect_review
        assert raw.execute("SELECT COUNT(*) FROM ticket_events WHERE kind = 'status_change'").fetchone()[0] == 0

    def test_a_resolved_ticket_with_a_comment_alone_never_reopens(self, temp_db, raw, set_now):
        set_now(T0)
        tid = self.make(temp_db)
        force_status(temp_db, tid, "Resolved", resolved_event_at="2026-11-07T00:00:00Z")
        result = self.add(tid, "add-1", "thank you!", "information")
        assert result["outcome"] == "added" and result["status"] == "Resolved" and result["transition"] is None

    def test_an_explicit_report_inside_the_window_reopens_atomically(self, temp_db, raw, set_now):
        set_now(T0)
        tid = self.make(temp_db)
        force_status(temp_db, tid, "Resolved", resolved_event_at="2026-11-07T00:00:00Z")
        set_now(T0 + timedelta(seconds=604799))
        result = self.add(tid, "add-1", "it is broken again", "issue_persists")
        assert result["outcome"] == "added" and result["status"] == "Reopened"
        change = rows(raw, "SELECT * FROM ticket_events WHERE kind = 'status_change' ORDER BY seq DESC")[0]
        assert change["transition_reason"] == "customer_reported_issue_persists" and change["from_status"] == "Resolved"

    def test_at_exactly_the_window_end_nothing_is_written(self, temp_db, raw, set_now):
        set_now(T0)
        tid = self.make(temp_db)
        force_status(temp_db, tid, "Resolved", resolved_event_at="2026-11-07T00:00:00Z")
        set_now(T0 + timedelta(seconds=604800))
        before = raw.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0]
        assert self.add(tid, "add-1", "back", "issue_persists") == {"outcome": "reopen_window_expired"}
        assert raw.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0] == before
        assert raw.execute("SELECT status FROM support_tickets").fetchone()[0] == "Resolved"

    def test_a_configurable_window(self, temp_db, raw, set_now):
        set_now(T0)
        tid = self.make(temp_db)
        force_status(temp_db, tid, "Resolved", resolved_event_at="2026-11-07T00:00:00Z")
        set_now(T0 + timedelta(seconds=100))
        assert self.add(tid, "add-1", "back", "issue_persists", reopen_window_seconds=100)["outcome"] == "reopen_window_expired"
        assert self.add(tid, "add-2", "back", "issue_persists", reopen_window_seconds=101)["outcome"] == "added"

    def test_without_a_trusted_resolved_event_the_rule_fails_closed(self, temp_db, raw, set_now):
        set_now(T0)
        tid = self.make(temp_db)
        force_status(temp_db, tid, "Resolved")  # no status_change event exists
        result = self.add(tid, "add-1", "still broken", "issue_persists")
        assert result["outcome"] == "reopen_unavailable" and result["status"] == "Resolved"
        assert result["staff_review_required"] is True
        assert raw.execute("SELECT status FROM support_tickets").fetchone()[0] == "Resolved"

    def test_reopening_is_refused_when_another_active_ticket_holds_the_same_issue(self, temp_db, raw, set_now):
        set_now(T0)
        tid = self.make(temp_db)
        force_status(temp_db, tid, "Resolved", resolved_event_at="2026-11-07T00:00:00Z")
        self.make(temp_db, key="2")  # a new ticket for the same item and category (the first is terminal)
        assert self.add(tid, "add-1", "back", "issue_persists") == {"outcome": "active_ticket_exists"}

    @pytest.mark.parametrize("status", ["Closed", "Cancelled"])
    def test_closed_and_cancelled_tickets_take_no_additions(self, temp_db, raw, status):
        tid = self.make(temp_db)
        force_status(temp_db, tid, status)
        assert self.add(tid, "add-1") == {"outcome": "ticket_closed"}
        assert raw.execute("SELECT COUNT(*) FROM ticket_events WHERE kind = 'comment'").fetchone()[0] == 0

    @pytest.mark.parametrize("status", ["New", "Pending", "Reopened"])
    def test_statuses_without_an_owner_approved_rule_write_nothing(self, temp_db, raw, status):
        """B-5 is unresolved: no rule is invented for these statuses."""
        tid = self.make(temp_db)
        force_status(temp_db, tid, status)
        assert self.add(tid, "add-1") == {"outcome": "status_rule_unspecified"}
        assert self.add(tid, "add-2", intent="issue_persists") == {"outcome": "status_rule_unspecified"}
        assert raw.execute("SELECT COUNT(*) FROM ticket_events WHERE kind = 'comment'").fetchone()[0] == 0

    @pytest.mark.parametrize("status", ["Open", "In Progress", "Waiting for customer", "On hold"])
    def test_issue_persists_is_not_applicable_outside_resolved(self, temp_db, status):
        tid = self.make(temp_db)
        force_status(temp_db, tid, status)
        assert self.add(tid, "add-1", intent="issue_persists") == {"outcome": "intent_not_applicable"}

    def test_a_foreign_and_a_fabricated_ticket_give_the_same_answer_and_write_nothing(self, temp_db, raw):
        tid = self.make(temp_db)
        assert self.add(tid, "add-1", customer="C102") == {"outcome": "not_found"}
        assert self.add("TKT_" + "0" * 32, "add-2") == {"outcome": "not_found"}
        assert raw.execute("SELECT COUNT(*) FROM ticket_events WHERE kind = 'comment'").fetchone()[0] == 0

    def test_idempotent_replay_and_conflict(self, temp_db, raw):
        tid = self.make(temp_db)
        first = self.add(tid, "add-1", "hello")
        again = self.add(tid, "add-1", "hello")
        assert again["outcome"] == "already_applied" and again["event_id"] == first["event_id"]
        assert self.add(tid, "add-1", "different") == {"outcome": "idempotency_conflict"}
        assert raw.execute("SELECT COUNT(*) FROM ticket_events WHERE kind = 'comment'").fetchone()[0] == 1

    def test_a_replayed_reopen_never_transitions_twice(self, temp_db, raw, set_now):
        set_now(T0)
        tid = self.make(temp_db)
        force_status(temp_db, tid, "Resolved", resolved_event_at="2026-11-07T00:00:00Z")
        self.add(tid, "add-1", "back", "issue_persists")
        assert self.add(tid, "add-1", "back", "issue_persists")["outcome"] == "already_applied"
        assert raw.execute("SELECT COUNT(*) FROM ticket_events WHERE kind = 'status_change' AND to_status = 'Reopened'"
                           ).fetchone()[0] == 1

    def test_the_comment_bound(self, temp_db, raw):
        tid = self.make(temp_db)
        for n in range(3):
            assert self.add(tid, f"add-{n}", comments_max=3)["outcome"] == "added"
        assert self.add(tid, "add-x", comments_max=3) == {"outcome": "comment_limit_reached"}
        assert self.add(tid, "add-0", comments_max=3)["outcome"] == "already_applied"  # replays are not blocked by the bound

    def test_comments_never_consume_the_creation_allowance(self, temp_db, raw):
        tid = self.make(temp_db, rate_limit=(1, DAY))
        for n in range(5):
            self.add(tid, f"add-{n}")
        assert create("other", "x", rate_limit=(2, DAY))["outcome"] == "created"

    def test_the_comment_and_the_transition_are_atomic(self, temp_db, raw, monkeypatch):
        tid = self.make(temp_db)
        force_status(temp_db, tid, "Waiting for customer")
        real = repository._insert_event
        calls = {"n": 0}

        def second_insert_fails(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:  # the status_change event, after the comment was already inserted
                raise sqlite3.OperationalError("injected")
            return real(*a, **k)

        monkeypatch.setattr(repository, "_insert_event", second_insert_fails)
        with pytest.raises(DatabaseOperationalError):
            self.add(tid, "add-1")
        assert raw.execute("SELECT COUNT(*) FROM ticket_events WHERE kind <> 'created'").fetchone()[0] == 0
        assert raw.execute("SELECT status FROM support_tickets").fetchone()[0] == "Waiting for customer"

    def test_validation(self, temp_db):
        tid = self.make(temp_db)
        with pytest.raises(ValueError):
            self.add(tid, "k", intent="Resolved")  # no status argument exists on any customer path
        with pytest.raises(ValueError):
            self.add(tid, "k", body="x" * 2001)
        with pytest.raises(ValueError):
            self.add(tid, "k", body="  ")
        with pytest.raises(TypeError):
            repository.add_ticket_event("C101", tid, "k", "b", "information", status="Resolved")

    def test_events_are_customer_scoped_and_ordered(self, temp_db, raw):
        tid = self.make(temp_db)
        self.add(tid, "add-1")
        got = repository.get_ticket_events("C101", tid)
        assert [e["seq"] for e in got["events"]] == [1, 2] and got["total"] == 2
        assert repository.get_ticket_events("C102", tid) == {"events": [], "total": 0}
        assert repository.get_ticket_events("C101", "TKT_" + "0" * 32) == {"events": [], "total": 0}
        assert repository.get_ticket_events("C101", tid, offset=1, limit=1)["events"][0]["seq"] == 2


class TestErasure:
    def seed(self, temp_db):
        item = an_item("ORD1001")
        a = create("e-1", "secret one", "product", order_id="ORD1001", item_id=item)["ticket_id"]
        repository.add_ticket_event("C101", a, "c-1", "secret comment", "information")
        b = create("e-2", "secret two")["ticket_id"]
        other = create("e-3", "other customer", customer="C102")["ticket_id"]
        return a, b, other

    def erase(self, key="er-1", **kw):
        return repository.erase_ticket_content("operator:op1", "AUTH-123", "privacy_request", "C101", key, **kw)

    def test_redacts_content_logs_counts_only_and_never_deletes_rows(self, temp_db, raw):
        a, b, other = self.seed(temp_db)
        result = self.erase()
        assert result["outcome"] == "erased" and result["tickets_redacted"] == 2 and result["events_redacted"] == 3
        got = {r["ticket_id"]: r for r in rows(raw, "SELECT * FROM support_tickets")}
        assert got[a]["description"] == "[erased]" and got[a]["conversation_summary"] is None and got[a]["redacted_at"]
        assert got[other]["description"] == "other customer" and got[other]["redacted_at"] is None
        comment = rows(raw, "SELECT body, redacted_at FROM ticket_events WHERE kind = 'comment'")[0]
        assert comment["body"] is None and comment["redacted_at"]
        assert raw.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0] == 4  # the audit skeleton survives
        intents = rows(raw, "SELECT payload_json, redacted_at FROM ticket_write_intents WHERE customer_id = 'C101'")
        assert all(i["payload_json"] is None and i["redacted_at"] for i in intents)
        assert raw.execute("SELECT payload_json FROM ticket_write_intents WHERE customer_id = 'C102'").fetchone()[0]
        log = rows(raw, "SELECT * FROM ticket_erasure_log")[0]
        assert log["tickets_redacted"] == 2 and log["scope_customer_id"] == "C101"
        assert "secret" not in json.dumps(log)  # the log holds no ticket text

    def test_idempotent_by_request_key(self, temp_db, raw):
        self.seed(temp_db)
        first = self.erase()
        again = self.erase()
        assert again["outcome"] == "already_applied" and again["erasure_id"] == first["erasure_id"]
        assert raw.execute("SELECT COUNT(*) FROM ticket_erasure_log").fetchone()[0] == 1

    def test_a_subset_of_tickets(self, temp_db, raw):
        a, b, _ = self.seed(temp_db)
        assert self.erase(ticket_ids=[b])["tickets_redacted"] == 1
        assert raw.execute("SELECT description FROM support_tickets WHERE ticket_id = ?", (a,)).fetchone()[0] == "secret one"

    def test_rate_limit_counts_survive_redaction(self, temp_db, raw):
        self.seed(temp_db)
        self.erase()
        assert create("e-9", "again", rate_limit=(2, DAY))["outcome"] == "rate_limited"

    def test_validation(self, temp_db):
        with pytest.raises(ValueError):
            repository.erase_ticket_content("op", "auth", "because", "C101", "k")
        with pytest.raises(ValueError):
            repository.erase_ticket_content("op", "auth", "privacy_request", "C101", "k", ticket_ids=[])

    def test_no_application_code_other_than_the_repository_reaches_erasure_or_events(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        for path in root.rglob("*.py"):
            if path.name == "repository.py":
                continue
            text = path.read_text(encoding="utf-8")
            assert "erase_ticket_content" not in text, path
            assert "add_ticket_event" not in text, path
            assert "ticket_events" not in text or path.name in ("schema.py", "migrations.py", "ticket_writer.py"), path

    def test_the_repository_never_deletes_events_and_only_the_erasure_updates_them(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / "app" / "db" / "repository.py").read_text(encoding="utf-8")
        assert "DELETE FROM ticket_events" not in source
        erase_source = source[source.index("def erase_ticket_content"):]
        before = source[:source.index("def erase_ticket_content")]
        assert "UPDATE ticket_events" in erase_source and "UPDATE ticket_events" not in before


class TestRateLimitAcrossRealProcesses:
    def test_separate_os_processes_cannot_both_pass_the_check(self, temp_db, raw, tmp_path):
        """NT-01 and NT-86 with real processes: the count is read from the database file inside BEGIN IMMEDIATE."""
        import os
        import subprocess
        import sys
        root = pathlib.Path(__file__).resolve().parents[1]
        script = tmp_path / "worker.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "from app.db import repository\n"
            "worker = sys.argv[1]\n"
            "out = []\n"
            "for n in range(3):\n"
            "    r = repository.create_ticket_from_description('C101', 'general', f'w{worker}-{n}', f'p{worker}-{n}', rate_limit=(4, 86400))\n"
            "    out.append(r['outcome'])\n"
            "print(','.join(out))\n", encoding="utf-8")
        env = dict(os.environ, APP_DB_PATH=temp_db)
        # a fixed clock is not injected here: all rows are created "now", well inside the 24 hour window
        procs = [subprocess.Popen([sys.executable, str(script), str(i)], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for i in range(4)]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            outputs += out.strip().split(",")
        assert outputs.count("created") == 4 and outputs.count("rate_limited") == 8
        assert raw.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 4
