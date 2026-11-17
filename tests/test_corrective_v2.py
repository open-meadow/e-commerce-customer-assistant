"""Corrective iteration v2: other customers' orders (H11), return status versus order status (H22).

H11 (generic request for another customer's orders): the v5 rule "if they give no order ID, call list_my_orders" made the
model present the signed-in customer's own orders as the answer. The fix is an exception inside that rule, and a named
order ID is still always looked up (a blanket sentence earlier made C027 stop looking the order up). Backend ownership
stays the control: nothing here changes what any tool can return.

H22 (what happened with my return for ORD1010): the seed has an order marked Returned with a Pending refund of Rs 1,999
and NO return request. The accurate answer reports both facts and does not treat Returned as proof that a request was
processed. Offline tests only; the live behaviour is measured by the evaluations. No network.
"""

from __future__ import annotations

import json

import pytest

import app.backend  # noqa: F401
from app.db import repository
from app.orchestrator.engine import SYSTEM_PROMPT as PROMPT
from app.tools.context import CustomerContext
from app.tools.registry import execute_tool


def run(customer, name, **args):
    return execute_tool(CustomerContext(customer, "ref"), name, args)


class TestOtherCustomersOrdersInThePrompt:
    def test_the_exception_sits_inside_the_no_order_id_rule(self):
        no_id = PROMPT.index("If they give no order ID, call list_my_orders")
        exception = PROMPT.index("The exception is a request to see another customer's orders or account")
        named = PROMPT.index("If the customer names an order ID, always call get_order_status")
        assert no_id < exception < named

    def test_the_exception_declines_briefly_and_does_not_redirect(self):
        assert "(by a customer ID, a name, or 'every customer')" in PROMPT
        assert "do not call list_my_orders for it" in PROMPT
        assert "say in one short sentence that you can only help with the signed-in customer's own account" in PROMPT
        assert "do not list orders or offer to list them unless they ask for their own" in PROMPT

    def test_a_named_order_is_always_looked_up_whoever_it_belongs_to(self):
        assert "A named order ID is always looked up first, whoever it belongs to." in PROMPT
        assert "If the customer names an order ID, always call get_order_status for it" in PROMPT
        assert "say plainly that you could not find it on their account" in PROMPT

    def test_the_blanket_sentence_that_broke_c027_is_still_absent(self):
        assert "if asked for another customer's information" not in PROMPT.lower()

    def test_the_prompt_stays_proportionate(self):
        assert len(PROMPT) < 7000


class TestReturnStatusInThePrompt:
    def test_the_status_intent_uses_both_the_returns_and_the_named_order(self):
        assert "is asking about its status: call get_my_returns and, if they name an order, get_order_status for it too" in PROMPT
        assert "report what each says" in PROMPT

    def test_returned_is_not_proof_and_no_record_is_not_proof_of_nothing(self):
        assert "An order marked Returned is not proof that a return request exists or was processed" in PROMPT
        assert "no request on file is not proof that nothing happened" in PROMPT

    def test_start_return_request_is_still_never_used_for_a_question_or_a_status(self):
        assert "Never use start_return_request for (2) or (3)" in PROMPT


class TestToolNotes:
    def test_an_empty_returns_list_says_what_it_does_and_does_not_prove(self, temp_db):
        result = run("C105", "get_my_returns")
        assert json.loads(result.to_json())["data"] == {"returns": [], "total_returns": 0}
        note = result.note
        assert "No return request is on file" in note and "also check that order with get_order_status" in note
        assert "not proof that a return request exists or was processed" in note
        assert "not proof that nothing happened" in note

    def test_a_list_with_a_return_carries_no_such_note(self, temp_db):
        repository.create_return_request("C103", "ORD1005", "IT105", "it overheats", reason_category="standard")
        result = run("C103", "get_my_returns")
        assert result.note is None and json.loads(result.to_json())["data"]["total_returns"] == 1

    def test_an_order_marked_returned_says_what_to_report_and_what_not_to_claim(self, temp_db):
        result = run("C105", "get_order_status", order_id="ORD1010")
        data = json.loads(result.to_json())["data"]
        assert data["status"] == "Returned" and data["refund_status"] == "Pending" and data["refund_amount"] == "₹1,999"
        note = result.note
        assert "This order is marked Returned" in note and "exactly as given" in note
        assert "do not say the return was processed, approved or refunded unless the data says so" in note
        assert "may or may not be on file" in note

    def test_an_ordinary_order_carries_only_the_old_note(self, temp_db):
        assert run("C101", "get_order_status", order_id="ORD1001").note == "Prices are the prices on the order."

    def test_the_data_of_both_tools_is_unchanged(self, temp_db):
        data = json.loads(run("C105", "get_order_status", order_id="ORD1010").to_json())["data"]
        assert set(data) == {"order_id", "status", "payment_status", "refund_status", "refund_amount", "order_date",
                             "delivered_at", "total", "items"}

    def test_the_seed_really_has_a_returned_order_without_a_request(self, temp_db):
        assert not repository.get_customer_returns("C105")
        orders = {o["order_id"]: o for o in repository.list_customer_orders("C105")}
        assert orders["ORD1010"]["status"] == "Returned"


class TestOwnershipIsUntouched:
    def test_another_customers_returned_order_is_not_found_and_carries_no_note_or_data(self, temp_db):
        result = run("C101", "get_order_status", order_id="ORD1010")
        assert result.status == "not_found" and result.code == "order_not_found"
        body = result.to_json()
        assert "Returned" not in body and "Pending" not in body and "1,999" not in body

    def test_another_customers_empty_returns_list_reveals_nothing_about_their_orders(self, temp_db):
        result = run("C101", "get_my_returns")
        assert "ORD" not in result.to_json()  # no order id at all
        assert result.note == run("C105", "get_my_returns").note  # the note is generic: the same for every customer

    def test_no_tool_takes_a_customer_id(self):
        from app.tools.registry import llm_tool_definitions
        for tool in llm_tool_definitions():
            assert not any("customer" in name.lower() for name in tool["parameters"]["properties"])
