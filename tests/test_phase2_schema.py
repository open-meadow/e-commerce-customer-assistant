"""Phase 2: constraints, triggers and indexes of the rebuilt schema (design sections 2 and 7)."""
from __future__ import annotations

import sqlite3

import pytest

from conftest import build_database
from app.db.connection import get_connection

TS = "2026-11-01T00:00:00Z"


@pytest.fixture
def db(tmp_path):
    path = build_database(str(tmp_path / "p2.db"))
    connection = get_connection(path)
    yield connection
    connection.close()


def ticket(db, tid="T1", *, issue="general", order=None, item=None, key=None, status="Open", desc="d", summary=None,
           cid="C101", request_key=None):
    db.execute(
        "INSERT INTO support_tickets (ticket_id, customer_id, order_id, item_id, issue_type, issue_key, description, "
        "conversation_summary, status, priority, request_key, request_hash, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Normal', ?, 'h', ?, ?)",
        (tid, cid, order, item, issue, key if key is not None else "n:" + tid, desc, summary, status,
         request_key or "k-" + tid, TS, TS))


def event(db, eid="E1", tid="T1", seq=1, kind="created", actor="customer", **kw):
    columns = {"event_id": eid, "ticket_id": tid, "customer_id": "C101", "seq": seq, "kind": kind, "actor_type": actor,
               "actor_ref": "customer:C101", "request_key": "ek-" + eid, "request_hash": "h", "created_at": TS}
    columns.update(kw)
    db.execute(f"INSERT INTO ticket_events ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
               tuple(columns.values()))


class TestIssueKey:
    def test_the_key_must_be_derivable_from_the_columns(self, db):
        item = db.execute("SELECT item_id FROM order_items WHERE order_id = 'ORD1001' LIMIT 1").fetchone()[0]
        ticket(db, "T1", issue="product", order="ORD1001", item=item, key=f"i:ORD1001:{item}")
        ticket(db, "T2", issue="payment", order="ORD1001", key="o:ORD1001")
        ticket(db, "T3", issue="general", order="ORD1001")  # n: key is always allowed
        for n, (issue, order, it, key) in enumerate([
            ("product", "ORD1001", item, "o:ORD1001"),          # a product ticket never gets an o: key
            ("payment", "ORD1001", None, f"i:ORD1001:{item}"),  # an order ticket never gets an i: key
            ("product", "ORD1001", item, "i:ORD1001:OTHER"),    # the key must equal what the columns say
            ("payment", None, None, "o:NULL"),                  # NULL branches must not slip through (NULL passes a CHECK)
        ]):
            with pytest.raises(sqlite3.IntegrityError):
                ticket(db, f"X{n}", issue=issue, order=order, item=it, key=key)

    def test_an_item_needs_an_order_and_belong_to_it(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, "T1", issue="product", item="IT1", key="n:T1")
        other = db.execute("SELECT item_id FROM order_items WHERE order_id <> 'ORD1001' LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):  # the composite FK keeps an item of another order out
            ticket(db, "T2", issue="product", order="ORD1001", item=other, key=f"i:ORD1001:{other}")

    def test_identifiers_with_a_colon_are_refused(self, db):
        db.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, "T1", issue="payment", order="ORD:1", key="o:ORD:1")


class TestActiveUniqueness:
    def _item(self, db):
        return db.execute("SELECT item_id FROM order_items WHERE order_id = 'ORD1001' LIMIT 1").fetchone()[0]

    def test_one_active_ticket_per_customer_category_and_issue_key(self, db):
        item = self._item(db)
        ticket(db, "T1", issue="product", order="ORD1001", item=item, key=f"i:ORD1001:{item}")
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, "T2", issue="product", order="ORD1001", item=item, key=f"i:ORD1001:{item}")
        ticket(db, "T3", issue="return", order="ORD1001", item=item, key=f"i:ORD1001:{item}")  # another category

    @pytest.mark.parametrize("terminal", ["Resolved", "Closed", "Cancelled"])
    def test_terminal_tickets_do_not_block_a_new_one(self, db, terminal):
        item = self._item(db)
        ticket(db, "T1", issue="product", order="ORD1001", item=item, key=f"i:ORD1001:{item}", status=terminal)
        ticket(db, "T2", issue="product", order="ORD1001", item=item, key=f"i:ORD1001:{item}")

    @pytest.mark.parametrize("active", ["New", "Open", "In Progress", "Pending", "Waiting for customer", "On hold", "Reopened"])
    def test_every_non_terminal_status_is_active(self, db, active):
        ticket(db, "T1", issue="payment", order="ORD1001", key="o:ORD1001", status=active)
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, "T2", issue="payment", order="ORD1001", key="o:ORD1001")

    def test_two_customers_are_never_merged(self, db):
        db.execute("INSERT INTO customers (customer_id, name, email) VALUES ('C999', 'X', 'x999@example.com')")
        ticket(db, "T1", issue="general")
        ticket(db, "T2", issue="general", cid="C999")

    def test_the_request_key_is_unique_per_customer(self, db):
        ticket(db, "T1", request_key="same")
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, "T2", request_key="same")


class TestTicketValues:
    def test_ten_statuses_are_accepted_and_others_rejected(self, db):
        for n, status in enumerate(["New", "Open", "In Progress", "Pending", "Waiting for customer", "On hold",
                                    "Resolved", "Reopened", "Closed", "Cancelled"]):
            ticket(db, f"T{n}", status=status)
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, "TX", status="Done")

    def test_the_summary_is_nullable_and_still_bounded_when_present(self, db):
        ticket(db, "T1", summary=None)
        ticket(db, "T2", summary="s" * 2000)
        for n, bad in enumerate(["s" * 2001, "", " \t\n", "a\x00b"]):
            with pytest.raises(sqlite3.IntegrityError):
                ticket(db, f"X{n}", summary=bad)

    @pytest.mark.parametrize("text", ["उ" * 2000, "\U0001F600" * 2000, "a" * 1999 + "क"])
    def test_exactly_2000_code_points_are_accepted_and_2001_rejected(self, db, text):
        ticket(db, "T1", desc=text)
        assert db.execute("SELECT description FROM support_tickets").fetchone()[0] == text  # stored byte for byte
        with pytest.raises(sqlite3.IntegrityError):
            ticket(db, "T2", desc=text + "x")

    def test_no_statement_trims_or_normalises_a_stored_description(self, db):
        text = "  leading\ttab\nnewline  *Markdown* <b>x</b>   trailing \n"
        ticket(db, "T1", desc=text)
        assert db.execute("SELECT description FROM support_tickets").fetchone()[0] == text

    def test_updated_at_cannot_precede_created_at(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO support_tickets (ticket_id, customer_id, issue_type, issue_key, description, request_key, "
                "request_hash, created_at, updated_at) VALUES ('T1', 'C101', 'general', 'n:T1', 'd', 'k', 'h', ?, ?)",
                ("2026-11-02T00:00:00Z", TS))


class TestImmutableText:
    def test_any_update_of_the_description_or_summary_aborts(self, db):
        ticket(db, "T1", summary="legacy summary")
        for sql in ("UPDATE support_tickets SET description = 'changed'",
                    "UPDATE support_tickets SET conversation_summary = 'changed'",
                    "UPDATE support_tickets SET conversation_summary = NULL"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(sql)

    def test_only_the_exact_erasure_shape_is_allowed(self, db):
        ticket(db, "T1", summary="legacy summary")
        db.execute("UPDATE support_tickets SET description = '[erased]', conversation_summary = NULL, redacted_at = ? "
                   "WHERE ticket_id = 'T1'", (TS,))
        row = db.execute("SELECT description, conversation_summary, redacted_at FROM support_tickets").fetchone()
        assert tuple(row) == ("[erased]", None, TS)
        with pytest.raises(sqlite3.IntegrityError):  # a second erasure-shaped update is no longer the NULL -> timestamp move
            db.execute("UPDATE support_tickets SET description = '[erased]', conversation_summary = NULL, redacted_at = ?",
                       ("2026-11-03T00:00:00Z",))

    def test_status_and_other_columns_remain_updatable(self, db):
        ticket(db, "T1")
        db.execute("UPDATE support_tickets SET status = 'Resolved', updated_at = ? WHERE ticket_id = 'T1'", (TS,))


class TestEvents:
    def test_events_are_append_only(self, db):
        ticket(db, "T1")
        event(db)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE ticket_events SET actor_ref = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            db.execute("DELETE FROM ticket_events")

    def test_the_comment_rules(self, db):
        ticket(db, "T1")
        event(db, "E1")
        event(db, "E2", seq=2, kind="comment", body="hello", intent="information")
        for n, kw in enumerate([
            dict(kind="comment", body="x"),                                    # a comment needs an explicit intent
            dict(kind="comment", body="", intent="information"),               # and a nonblank body
            dict(kind="comment", body="x" * 2001, intent="information"),       # at most 2,000 characters
            dict(kind="created", intent="information"),                        # only comments carry an intent
            dict(kind="created", staff_review_required=1),                     # the flag is for comments only
        ]):
            with pytest.raises(sqlite3.IntegrityError):
                event(db, f"X{n}", seq=10 + n, **kw)

    def test_status_change_audit_columns(self, db):
        ticket(db, "T1")
        event(db, "E1")
        event(db, "E2", seq=2, kind="comment", body="c", intent="information")
        good = dict(kind="status_change", actor="system", from_status="Waiting for customer", to_status="In Progress",
                    transition_reason="customer_supplied_requested_information", trigger_event_id="E2")
        event(db, "E3", seq=3, **good)
        for n, change in enumerate([
            dict(actor="customer"),                                           # a customer never appears on a status_change
            dict(from_status="In Progress"),                                  # from must differ from to
            dict(transition_reason=None),                                     # the reason is required
            dict(trigger_event_id=None),                                      # a customer-driven transition names its comment
            dict(to_status="Done"),                                           # the ten-value vocabulary
            dict(transition_reason="staff_action"),                           # staff_action needs an agent actor
        ]):
            with pytest.raises(sqlite3.IntegrityError):
                event(db, f"Y{n}", seq=10 + n, **{**good, **change})
        with pytest.raises(sqlite3.IntegrityError):  # a non-status event carries no status fields
            event(db, "Z", seq=30, kind="created", from_status="Open")

    def test_the_audit_trail_is_ordered_per_ticket(self, db):
        ticket(db, "T1")
        event(db, "E1", seq=1)
        with pytest.raises(sqlite3.IntegrityError):
            event(db, "E2", seq=1)  # UNIQUE (ticket_id, seq)

    def test_only_the_redaction_shape_may_update_an_event(self, db):
        ticket(db, "T1")
        event(db, "E1")
        event(db, "E2", seq=2, kind="comment", body="secret", intent="information")
        db.execute("UPDATE ticket_events SET body = NULL, redacted_at = ? WHERE event_id = 'E2'", (TS,))
        assert db.execute("SELECT body, redacted_at FROM ticket_events WHERE event_id = 'E2'").fetchone()[1] == TS


class TestErasureLogAndIntents:
    def test_the_erasure_log_is_append_only_and_holds_no_text(self, db):
        db.execute("INSERT INTO ticket_erasure_log (erasure_id, performed_at, operator_ref, authorization_ref, reason_code, "
                   "scope_customer_id, tickets_redacted, events_redacted, request_key) "
                   "VALUES ('ERS_1', ?, 'op', 'auth', 'privacy_request', 'C101', 1, 2, 'rk')", (TS,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE ticket_erasure_log SET operator_ref = 'x'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM ticket_erasure_log")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO ticket_erasure_log VALUES ('ERS_2', ?, 'op', 'auth', 'because', 'C101', 0, 0, 'rk2')", (TS,))
        columns = {r["name"] for r in db.execute("PRAGMA table_info(ticket_erasure_log)")}
        assert not columns & {"description", "body", "text", "customer_name", "email"}

    def test_an_intent_payload_is_immutable_and_never_deleted(self, db):
        db.execute("INSERT INTO ticket_write_intents (customer_id, request_key, payload_json, request_hash, created_at) "
                   "VALUES ('C101', 'k1', '{}', 'h', ?)", (TS,))
        for sql in ("UPDATE ticket_write_intents SET payload_json = '{\"x\":1}'",
                    "UPDATE ticket_write_intents SET request_hash = 'other'",
                    "UPDATE ticket_write_intents SET request_key = 'k2'",
                    "DELETE FROM ticket_write_intents"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
        db.execute("UPDATE ticket_write_intents SET last_outcome = 'rate_limited', resolved_at = ?", (TS,))  # state fields move
        db.execute("UPDATE ticket_write_intents SET payload_json = NULL, redacted_at = ?", (TS,))  # the erasure shape

    def test_committed_means_a_ticket_id_is_present(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO ticket_write_intents (customer_id, request_key, payload_json, request_hash, state, created_at) "
                       "VALUES ('C101', 'k1', '{}', 'h', 'committed', ?)", (TS,))
