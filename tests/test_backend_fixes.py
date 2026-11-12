"""Regression tests for the three defects found in the backend audit.

P2-2  A return that becomes ineligible between category selection and confirmation
      must produce a clear message, never one containing "None".
P2-3  Typing ``cancel`` after the assistant offered a ticket, before the issue
      description was given, must cancel the request and not become the description.
P2-4  A missing or invalid product catalog or FAQ/policy source must not break
      unrelated messages. Only the features that need the source say it is unavailable.

All databases are temporary. The real ``app/db/data/source`` files are never modified: the
resource tests point the loaders at temporary folders.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.backend import Backend, Settings
from app.catalog import CatalogError, products as products_module
from app.db import repository
from app.db.errors import DatabaseOperationalError
from app.orchestrator.engine import Engine
from app.rag import KnowledgeBaseError, load_chunks, retriever as retriever_module
from app.tools import CustomerContext, execute_tool
from app.catalog.products import Catalog
from backend_helpers import Client, ScriptedLLM

UTC = timezone.utc
REAL_PRODUCTS = Path(products_module.PRODUCTS_JSON)
REAL_SOURCE = Path(retriever_module.SOURCE_DIR)

REASON = "I want to return item IT101 from order ORD1001 because I changed my mind about it"


@pytest.fixture
def backend(temp_db):
    return Backend(llm=ScriptedLLM(), settings=Settings())


def rows(raw, table):
    return [dict(r) for r in raw.execute(f"SELECT * FROM {table}").fetchall()]


# ---------------------------------------------------------------------------
# P2-2: return window closes between category selection and confirmation
# ---------------------------------------------------------------------------

class TestWindowClosesBeforeConfirmation:
    """ORD1001 was delivered 2026-11-04T00:00:00Z, so a 'standard' (168 h) return closes
    2026-11-11T00:00:00Z. The seed baseline is 2026-11-07, so the test moves the database
    clock next to the closing time."""

    def to_pending(self, backend, set_now):
        set_now(datetime(2026, 11, 10, 23, 58, tzinfo=UTC))  # two minutes before the window closes
        c = Client(backend, "C101")
        pending = c.pick(c.say(REASON), "standard")
        assert pending.status == "needs_confirmation", pending.text
        return c, pending

    def test_the_message_states_the_window_and_the_date_without_placeholders(self, backend, raw, set_now):
        c, pending = self.to_pending(backend, set_now)
        set_now(datetime(2026, 11, 11, 0, 0, 1, tzinfo=UTC))  # one second after the window closed
        out = c.confirm(pending)
        assert out.status == "ok" and "RET_" not in out.text
        assert "168-hour" in out.text and "2026-11-11" in out.text
        assert "None" not in out.text and ", so I" in out.text and "closed on ," not in out.text
        assert rows(raw, "return_requests") == []

    def test_a_rejected_confirmation_is_not_counted_against_the_session_cap(self, backend, set_now):
        c, pending = self.to_pending(backend, set_now)
        set_now(datetime(2026, 11, 11, 0, 0, 1, tzinfo=UTC))
        c.confirm(pending)
        session = backend.registry.get(c.state._handle)
        assert session.committed["return_request"] == 0 and session.reserved["return_request"] == 0

    def test_the_boundary_second_is_still_allowed(self, backend, raw, set_now):
        c, pending = self.to_pending(backend, set_now)
        set_now(datetime(2026, 11, 11, 0, 0, 0, tzinfo=UTC))  # inclusive: exactly at the closing time
        assert "RET_" in c.confirm(pending).text
        assert len(rows(raw, "return_requests")) == 1

    def test_if_the_fresh_read_fails_the_message_is_still_clear(self, backend, raw, set_now, monkeypatch):
        c, pending = self.to_pending(backend, set_now)
        set_now(datetime(2026, 11, 11, 0, 0, 1, tzinfo=UTC))

        def boom(*args, **kwargs):
            raise DatabaseOperationalError("down")

        monkeypatch.setattr(repository, "check_return_eligibility", boom)
        out = c.confirm(pending)
        assert "return window" in out.text and "closed" in out.text and "None" not in out.text
        assert "RET_" not in out.text and rows(raw, "return_requests") == []

    def test_a_duplicate_created_meanwhile_is_reported_as_such(self, backend, raw, set_now):
        c, pending = self.to_pending(backend, set_now)
        repository.create_return_request("C101", "ORD1001", "IT101", "created elsewhere",
                                         reason_category="standard")
        out = c.confirm(pending)
        assert "already an active return" in out.text and "None" not in out.text
        assert len(rows(raw, "return_requests")) == 1  # only the one created elsewhere

    @pytest.mark.parametrize("verdict", [
        {"outcome": "outside_return_window"},
        {"outcome": "outside_return_window", "window_hours": None, "window_closes_at": None},
        {"outcome": "outside_return_window", "window_hours": 168},
        {"outcome": "outside_return_window", "window_closes_at": "2026-11-11T00:00:00Z"},
        {"outcome": "order_status_ineligible"},
        {"outcome": "order_status_ineligible", "order_status": None},
        {"outcome": "delivery_timestamp_missing"},
        {"outcome": "active_return_exists"},
        {"outcome": "item_not_found"},
        {"outcome": "order_not_found"},
        {"outcome": "something_new"},
    ])
    def test_no_rejection_message_ever_contains_a_placeholder(self, verdict):
        text = Engine._ineligible_text(verdict)
        assert text and "None" not in text and "{" not in text and "}" not in text

    def test_a_complete_verdict_still_gives_the_specific_message(self):
        text = Engine._ineligible_text({"outcome": "outside_return_window", "window_hours": 48,
                                        "window_closes_at": "2026-11-06T00:00:00Z"})
        assert "48-hour" in text and "2026-11-06" in text


# ---------------------------------------------------------------------------
# P2-3: cancel while the ticket description is awaited
# ---------------------------------------------------------------------------

class TestCancelWhileTicketDescriptionAwaited:
    def ask(self, backend):
        c = Client(backend, "C101")
        reply = c.say("I want to speak to a human agent")
        assert "what is the issue" in reply.text.lower()
        return c

    @pytest.mark.parametrize("word", ["cancel", "CANCEL", " Cancel ", "cancel!", "Cancel."])
    def test_cancel_ends_the_request_and_is_not_stored(self, backend, raw, word):
        c = self.ask(backend)
        out = c.say(word)
        session = backend.registry.get(c.state._handle)
        assert out.status == "ok" and "cancelled" in out.text and out.pending is None
        assert session.want_ticket is False and session.issues.active is None
        assert rows(raw, "support_tickets") == []

    def test_the_word_cancel_never_becomes_an_issue_or_a_description(self, backend, raw):
        c = self.ask(backend)
        c.say("cancel")
        later = c.say("My earbuds stopped working after two days")
        session = backend.registry.get(c.state._handle)
        # The next message is an ordinary issue report, not an automatic ticket proposal.
        assert later.status == "ok" and later.pending is None
        assert session.issues.active.messages == ["My earbuds stopped working after two days"]
        assert all("cancel" not in m.lower() for m in session.issues.active.messages)
        assert rows(raw, "support_tickets") == []

    def test_a_ticket_can_still_be_requested_after_cancelling(self, backend, raw):
        c = self.ask(backend)
        c.say("cancel")
        c.say("My earbuds stopped working after two days")
        proposal = c.say("create a ticket for this")
        assert proposal.status == "needs_confirmation"
        c.confirm(proposal)
        ticket = rows(raw, "support_tickets")[0]
        assert ticket["description"] == "My earbuds stopped working after two days"

    def test_without_cancel_the_next_message_is_still_the_description(self, backend, raw):
        c = self.ask(backend)
        proposal = c.say("My earbuds stopped working after two days")
        assert proposal.status == "needs_confirmation" and proposal.pending.kind == "support_ticket"
        c.confirm(proposal)
        assert rows(raw, "support_tickets")[0]["description"] == "My earbuds stopped working after two days"

    def test_cancel_with_nothing_awaited_is_an_ordinary_message(self, backend):
        reply = Client(backend, "C101").say("cancel")
        assert "Nothing was changed" not in reply.text

    def test_a_non_cancel_message_that_contains_the_word_is_a_description(self, backend):
        c = self.ask(backend)
        reply = c.say("please do not cancel my earbuds issue, they stopped working")
        assert reply.status == "needs_confirmation"


# ---------------------------------------------------------------------------
# P2-4: missing or invalid catalog / FAQ source
# ---------------------------------------------------------------------------

@pytest.fixture
def no_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(products_module, "PRODUCTS_JSON", tmp_path / "nothing" / "Products.json")
    products_module.default_catalog.cache_clear()
    yield
    monkeypatch.undo()
    products_module.default_catalog.cache_clear()


@pytest.fixture
def no_knowledge(monkeypatch, tmp_path):
    monkeypatch.setattr(retriever_module, "SOURCE_DIR", tmp_path / "nothing")
    retriever_module.default_knowledge_base.cache_clear()
    yield
    monkeypatch.undo()
    retriever_module.default_knowledge_base.cache_clear()


@pytest.fixture
def nothing_available(no_catalog, no_knowledge):
    return None


class TestLoadersReportTypedErrors:
    def write(self, folder, name, text):
        (folder / name).write_text(text, encoding="utf-8")

    def test_catalog_missing_file(self, tmp_path):
        with pytest.raises(CatalogError):
            Catalog.load(tmp_path / "missing.json")

    @pytest.mark.parametrize("content", ["{not json", "[]", "{}", '"text"', '[1, 2]',
                                         '[{"id": "P1", "name": "n", "category": "c", "price_inr": 0}]',
                                         '[{"id": "P1", "name": "n", "category": "c", "price_inr": true}]',
                                         '[{"id": "P1", "name": "n", "category": "c", "price_inr": 5},'
                                         ' {"id": "P1", "name": "n", "category": "c", "price_inr": 5}]',
                                         '[{"name": "no id"}]'])
    def test_catalog_invalid_content(self, tmp_path, content):
        path = tmp_path / "Products.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(CatalogError):
            Catalog.load(path)

    def test_catalog_undecodable_bytes(self, tmp_path):
        path = tmp_path / "Products.json"
        path.write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(CatalogError):
            Catalog.load(path)

    def test_knowledge_missing_folder(self, tmp_path):
        with pytest.raises(KnowledgeBaseError):
            load_chunks(tmp_path / "missing")

    @pytest.mark.parametrize("missing", ["FAQ.md", "Policies.md"])
    def test_knowledge_one_file_missing(self, tmp_path, missing):
        for name in ("FAQ.md", "Policies.md"):
            if name != missing:
                shutil.copy(REAL_SOURCE / name, tmp_path / name)
        with pytest.raises(KnowledgeBaseError):
            load_chunks(tmp_path)

    @pytest.mark.parametrize("faq,policies", [("no questions here", None), (None, "no sections here")])
    def test_knowledge_source_without_entries(self, tmp_path, faq, policies):
        self.write(tmp_path, "FAQ.md", faq if faq is not None else (REAL_SOURCE / "FAQ.md").read_text(encoding="utf-8"))
        self.write(tmp_path, "Policies.md",
                   policies if policies is not None else (REAL_SOURCE / "Policies.md").read_text(encoding="utf-8"))
        with pytest.raises(KnowledgeBaseError):
            load_chunks(tmp_path)

    def test_knowledge_undecodable_bytes(self, tmp_path):
        shutil.copy(REAL_SOURCE / "Policies.md", tmp_path / "Policies.md")
        (tmp_path / "FAQ.md").write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(KnowledgeBaseError):
            load_chunks(tmp_path)

    def test_the_real_sources_still_load(self):
        assert len(load_chunks(REAL_SOURCE)) == 25 and len(Catalog.load(REAL_PRODUCTS).all_products()) > 0

    def test_a_failed_load_is_not_cached_so_the_feature_recovers(self, monkeypatch, tmp_path):
        monkeypatch.setattr(products_module, "PRODUCTS_JSON", tmp_path / "Products.json")
        monkeypatch.setattr(retriever_module, "SOURCE_DIR", tmp_path)
        products_module.default_catalog.cache_clear()
        retriever_module.default_knowledge_base.cache_clear()
        try:
            with pytest.raises(CatalogError):
                products_module.default_catalog()
            with pytest.raises(KnowledgeBaseError):
                retriever_module.default_knowledge_base()
            shutil.copy(REAL_PRODUCTS, tmp_path / "Products.json")
            shutil.copy(REAL_SOURCE / "FAQ.md", tmp_path / "FAQ.md")
            shutil.copy(REAL_SOURCE / "Policies.md", tmp_path / "Policies.md")
            assert products_module.default_catalog().get_product("P1001") is not None
            assert retriever_module.default_knowledge_base().search("how long do refunds take")
        finally:
            monkeypatch.undo()
            products_module.default_catalog.cache_clear()
            retriever_module.default_knowledge_base.cache_clear()


class TestToolsDegradeIndividually:
    ctx = CustomerContext("C101", "ref")

    def test_product_lookup_reports_unavailable_without_inventing_products(self, no_catalog):
        for args in ({"query": "earbuds"}, {"product_id": "P1001"}):
            result = execute_tool(self.ctx, "lookup_product", args)
            assert (result.status, result.code) == ("unavailable", "catalog_unavailable")
            assert result.data == {} and "temporarily unavailable" in result.note

    def test_policy_search_reports_unavailable_without_inventing_policy(self, no_knowledge):
        result = execute_tool(self.ctx, "search_policies", {"query": "how long do refunds take"})
        assert (result.status, result.code) == ("unavailable", "knowledge_unavailable")
        assert result.data == {} and "cannot confirm" in result.note

    def test_refund_policy_without_an_order_is_unavailable(self, no_knowledge):
        result = execute_tool(self.ctx, "get_refund_policy", {})
        assert (result.status, result.code) == ("unavailable", "knowledge_unavailable")

    def test_refund_policy_with_an_order_still_gives_the_order_facts(self, temp_db, no_knowledge):
        result = execute_tool(self.ctx, "get_refund_policy", {"order_id": "ORD1001"})
        assert result.status == "ok" and result.data["passages"] == []
        assert result.data["policy_unavailable"] is True and result.data["order"]["order_id"] == "ORD1001"
        assert "cannot confirm" in result.note

    def test_order_status_still_works_without_the_catalog(self, temp_db, no_catalog):
        result = execute_tool(self.ctx, "get_order_status", {"order_id": "ORD1001"})
        assert result.status == "ok" and result.data["status"] == "Delivered"
        name = result.data["items"][0]["product_name"]
        assert "temporarily unavailable" in name and "Earbuds" not in name

    def test_unrelated_tools_are_unaffected(self, temp_db, nothing_available):
        assert execute_tool(self.ctx, "list_my_orders", {}).status == "ok"
        assert execute_tool(self.ctx, "get_my_returns", {}).status == "ok"
        assert execute_tool(self.ctx, "get_my_tickets", {}).status == "ok"

    def test_a_missing_catalog_does_not_affect_policy_search_and_vice_versa(self, no_catalog):
        assert execute_tool(self.ctx, "search_policies", {"query": "how long do refunds take"}).status == "ok"

    def test_a_missing_knowledge_source_does_not_affect_the_catalog(self, no_knowledge):
        assert execute_tool(self.ctx, "lookup_product", {"query": "earbuds"}).status == "ok"


class TestConversationContinues:
    """With BOTH sources missing, everything that does not need them keeps working."""

    def test_greeting_and_small_talk(self, backend, nothing_available):
        c = Client(backend, "C101")
        assert c.say("hello").status == "ok" and "help" in c.say("hi").text.lower()
        assert c.say("thanks").status == "ok"
        assert "only help" in c.say("tell me a joke").text

    def test_order_status_works(self, backend, nothing_available):
        reply = Client(backend, "C101").say("Where is my order ORD1001?")
        assert reply.status == "ok" and "Delivered" in reply.text and "ORD1001" in reply.text
        assert "temporarily unavailable" in reply.text and "Wireless Earbuds" not in reply.text

    def test_listing_orders_works(self, backend, nothing_available):
        assert "ORD1001" in Client(backend, "C101").say("show all my orders").text

    def test_a_product_question_says_product_information_is_unavailable(self, backend, no_catalog):
        reply = Client(backend, "C101").say("How much do the earbuds cost?")
        assert reply.status == "ok" and "Product information is temporarily unavailable" in reply.text
        assert "₹" not in reply.text  # no invented price

    def test_a_policy_question_says_it_cannot_confirm_and_cites_nothing(self, backend, no_knowledge):
        reply = Client(backend, "C101").say("What is your refund policy?")
        assert reply.status == "ok" and reply.citations == ()
        assert "can't confirm" in reply.text and "7 days" not in reply.text and "business days" not in reply.text

    def test_refund_status_of_an_order_still_answers_with_the_order_facts(self, backend, no_knowledge):
        reply = Client(backend, "C101").say("what is the refund status of ORD1001")
        assert "Order ORD1001" in reply.text and "can't confirm" in reply.text and reply.citations == ()

    def test_the_return_workflow_works_without_the_catalog(self, backend, raw, no_catalog):
        c = Client(backend, "C101")
        choice = c.say(REASON)
        assert choice.status == "needs_choice" and "the item" in choice.choice.question
        pending = c.pick(choice, "standard")
        assert pending.status == "needs_confirmation"
        assert "RET_" in c.confirm(pending).text
        assert len(rows(raw, "return_requests")) == 1

    def test_the_ticket_workflow_works_without_either_source(self, backend, raw, nothing_available):
        c = Client(backend, "C101")
        c.say("I want to speak to a human agent")
        proposal = c.say("My earbuds stopped working after two days")
        assert proposal.status == "needs_confirmation"
        assert "TKT_" in c.confirm(proposal).text and len(rows(raw, "support_tickets")) == 1

    def test_the_health_check_is_unaffected(self, backend, nothing_available):
        assert backend.check_backend().status == "ready"

    def test_the_features_recover_when_the_sources_return(self, backend, monkeypatch, tmp_path):
        monkeypatch.setattr(products_module, "PRODUCTS_JSON", tmp_path / "Products.json")
        products_module.default_catalog.cache_clear()
        try:
            c = Client(backend, "C101")
            assert "temporarily unavailable" in c.say("How much do the earbuds cost?").text
            shutil.copy(REAL_PRODUCTS, tmp_path / "Products.json")
            assert "₹" in c.say("How much do the earbuds cost?").text
        finally:
            monkeypatch.undo()
            products_module.default_catalog.cache_clear()


class TestRealDefectsAreNotHidden:
    """The handling is deliberately narrow: only CatalogError and KnowledgeBaseError."""

    def test_an_unexpected_error_in_a_tool_still_surfaces_as_an_internal_error(self, backend, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("programming error")

        monkeypatch.setattr(repository, "list_customer_orders", boom)
        reply = Client(backend, "C101").say("show all my orders")
        assert reply.status == "error" and reply.error_code == "internal" and "programming error" not in reply.text

    def test_an_unexpected_catalog_error_is_not_swallowed(self, backend, monkeypatch):
        import app.catalog as catalog_package

        def boom():
            raise RuntimeError("not a CatalogError")

        monkeypatch.setattr(catalog_package, "default_catalog", boom)
        reply = Client(backend, "C101").say("hello")
        assert reply.status == "error" and reply.error_code == "internal"

    def test_an_unexpected_knowledge_error_is_not_swallowed(self, backend, monkeypatch):
        import app.rag as rag_package

        def boom():
            raise RuntimeError("not a KnowledgeBaseError")

        monkeypatch.setattr(rag_package, "default_knowledge_base", boom)
        reply = Client(backend, "C101").say("What is your refund policy?")
        assert reply.status == "error" and reply.error_code == "internal"
