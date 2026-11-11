"""Offline, deterministic stand-in for an LLM.

This is **not** a language model. It maps customer text to tool calls
with fixed rules and renders tool results with templates, so the whole
backend can be run, demonstrated and tested with no API key and no
network. It is the default client. It is also what makes the unit tests
deterministic. It understands far less than a real model, and every real
behavior claim about language understanding belongs to a real provider
adapter, which this project has not verified (no credentials).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.llm.base import LLMResult, ToolCall
from app.llm.tasks import RELATION_LABELS

_ORDER_RE = re.compile(r"\bORD\d+\b", re.I)
_ITEM_RE = re.compile(r"\bIT\d+\b", re.I)
_PRODUCT_RE = re.compile(r"\bP\d{4}\b", re.I)
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset("""a an the is are was my i you it to of for in on and or not with this that
me do does did can please have has had be been am at by from so if then also just""".split())
_ISSUE_CUES = ("not working", "broken", "damaged", "wrong", "late", "delay", "missing",
               "charged", "failed", "defect", "problem", "issue", "stopped", "no sound",
               "complain", "refund")
_NEW_CUES = ("also", "another", "separately", "different", "by the way", "unrelated issue")
_INFO_CUES = ("policy", "what is", "how long", "how do i", "do you", "what are", "how many")
_RETURN_ACTION = ("want to return", "like to return", "need to return", "return my",
                  "return the", "return this", "start a return", "return it", "return order")
_TICKET_CUES = ("ticket", "escalate", "human", "support agent", "speak to someone", "talk to")


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP and len(w) > 2}


def _first(pattern: re.Pattern, text: str) -> Optional[str]:
    match = pattern.search(text)
    return match.group(0).upper() if match else None


def _section(prompt: str, name: str) -> str:
    match = re.search(rf"{name}:\n(.*?)(?:\n\n[A-Z_]+:\n|\Z)", prompt, re.S)
    return match.group(1).strip() if match else ""


class RuleBasedLLM:
    """Deterministic rules plus templates. See the module docstring."""

    name = "rule_based"

    # -- tool-free tasks ------------------------------------------------

    def complete(self, system: str, prompt: str, *, max_output_tokens: int) -> str:
        if "TASK: classify_relation" in system:
            return self._classify(prompt)
        if "TASK: summarize_ticket" in system:
            return self._summarize(prompt)
        return ""

    def _classify(self, prompt: str) -> str:
        active = _section(prompt, "ACTIVE_ISSUE")
        suspended = _section(prompt, "SUSPENDED_ISSUE")
        message = _section(prompt, "MESSAGE").lower()
        words = _words(message)
        if any(cue in message for cue in _INFO_CUES) and not any(c in message for c in _ISSUE_CUES):
            return "unrelated"
        has_suspended = suspended and suspended != "(none)"
        if has_suspended and (words & _words(suspended)) and not (words & _words(active)):
            return "switch_to_suspended_issue"
        if words & _words(active):
            return "same_issue"
        if any(cue in message for cue in _NEW_CUES) or any(c in message for c in _ISSUE_CUES):
            return "new_issue"
        return "related_but_unclear"

    def _summarize(self, prompt: str) -> str:
        messages = _section(prompt, "ISSUE_MESSAGES")
        facts = [l[2:] for l in _section(prompt, "FACTS").splitlines() if l.startswith("- ")]
        actions = [l[2:] for l in _section(prompt, "ACTIONS").splitlines() if l.startswith("- ")]
        parts = [f"Customer Issue\n{messages}"]
        if facts:
            parts.append("Relevant Context\n" + "\n".join(facts))
        if actions:
            parts.append("Actions Already Taken\n" + "\n".join(actions))
        parts.append("Current Status\nAwaiting support follow-up.")
        return "\n\n".join(parts)

    # -- tool turn ------------------------------------------------------

    def chat(self, system: str, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]], *, max_output_tokens: int) -> LLMResult:
        if not messages:
            return LLMResult(text="How can I help you today?")
        last = messages[-1]
        if last["role"] == "tool":
            return self._after_tool(messages, last)
        return self._decide(str(last.get("content", "")))

    def _decide(self, text: str) -> LLMResult:
        lower = text.lower()
        order, item = _first(_ORDER_RE, text), _first(_ITEM_RE, text)
        if "my tickets" in lower or "ticket status" in lower or "my support tickets" in lower:
            return LLMResult(tool_calls=(ToolCall("get_my_tickets", {}),))
        if any(cue in lower for cue in _TICKET_CUES):
            args: dict[str, Any] = {"issue_type": self._issue_type(lower)}
            if order:
                args["order_id"] = order
            return LLMResult(tool_calls=(ToolCall("start_support_ticket", args),))
        if any(phrase in lower for phrase in _RETURN_ACTION):
            if not order:
                return LLMResult(text="Which order would you like to return? Please share the order ID.")
            if item:
                return LLMResult(tool_calls=(ToolCall(
                    "start_return_request", {"order_id": order, "item_id": item}),))
            return LLMResult(tool_calls=(ToolCall("get_order_status", {"order_id": order}),))
        if "my returns" in lower or "return status" in lower or "return request" in lower:
            return LLMResult(tool_calls=(ToolCall("get_my_returns", {}),))
        if "my tickets" in lower or "ticket status" in lower:
            return LLMResult(tool_calls=(ToolCall("get_my_tickets", {}),))
        if "refund" in lower:
            if order:
                return LLMResult(tool_calls=(ToolCall("get_refund_policy", {"order_id": order}),))
            # No order named: answer the actual question from the policy text.
            return LLMResult(tool_calls=(ToolCall("search_policies", {"query": text}),))
        if order:
            return LLMResult(tool_calls=(ToolCall("get_order_status", {"order_id": order}),))
        if "my orders" in lower or "list orders" in lower or "all my orders" in lower:
            return LLMResult(tool_calls=(ToolCall("list_my_orders", {}),))
        if any(w in lower for w in ("where is my order", "track my order", "order status")):
            return LLMResult(text="Could you share the order ID (for example ORD1001)?")
        if _PRODUCT_RE.search(text) or any(
            w in lower for w in ("earbud", "headphone", "charger", "monitor", "power bank",
                                 "price", "spec", "stock", "hdmi", "battery")):
            return LLMResult(tool_calls=(ToolCall("lookup_product", {"query": text}),))
        return LLMResult(tool_calls=(ToolCall("search_policies", {"query": text}),))

    @staticmethod
    def _issue_type(lower: str) -> str:
        if "return" in lower:
            return "return"
        if any(w in lower for w in ("payment", "charged", "refund", "paid")):
            return "payment"
        if any(w in lower for w in ("order", "delivery", "shipping")):
            return "order"
        if any(w in lower for w in ("product", "earbud", "headphone", "charger", "monitor")):
            return "product"
        return "general"

    def _after_tool(self, messages: list[dict[str, Any]], last: dict[str, Any]) -> LLMResult:
        name = last.get("name", "")
        try:
            result = json.loads(last.get("content", "{}"))
        except ValueError:
            return LLMResult(text="I couldn't read that result. Please try again.")
        status, data = result.get("status"), result.get("data") or {}
        if status == "unavailable":
            code = result.get("code")
            if code == "catalog_unavailable":
                return LLMResult(text="Product information is temporarily unavailable, so I can't look that up "
                                      "right now. I can raise a support ticket if you like.")
            if code == "knowledge_unavailable":
                return LLMResult(text="I can't look up our FAQ and policies right now, so I can't confirm that. "
                                      "I can raise a support ticket if you like.")
            return LLMResult(text="I'm having trouble reaching the order system right now. Please try again shortly.")
        if status == "not_found":
            return LLMResult(text="I couldn't find that on your account.")
        if status == "invalid_input":
            return LLMResult(text="I need a valid order ID or detail to look that up. Could you check it?")
        if name == "get_order_status":
            user = next((m for m in reversed(messages) if m["role"] == "user"), {})
            user_text = str(user.get("content", ""))
            lower = user_text.lower()
            items = data.get("items", [])
            if any(p in lower for p in _RETURN_ACTION):
                if len(items) == 1:
                    return LLMResult(tool_calls=(ToolCall("start_return_request", {
                        "order_id": data["order_id"], "item_id": items[0]["item_id"]}),))
                names = ", ".join(f"{i['item_id']} ({i['product_name']})" for i in items)
                return LLMResult(text=f"Which item would you like to return? This order has: {names}.")
            return LLMResult(text=self._render_order(data))
        if name == "list_my_orders":
            lines = [f"{o['order_id']}: {o['status']}, ordered {o['order_date']}, total {o['total']}"
                     for o in data.get("orders", [])]
            return LLMResult(text="Your orders:\n" + "\n".join(lines) if lines else "You have no orders yet.")
        if name == "get_my_returns":
            lines = [f"{r['return_id']}: item {r['item_id']} on order {r['order_id']}, "
                     f"{r['status']} ({r['reason_category']})" for r in data.get("returns", [])]
            return LLMResult(text="Your return requests:\n" + "\n".join(lines) if lines
                             else "You have no return requests.")
        if name == "get_my_tickets":
            lines = [f"{t['ticket_id']}: {t['issue_type']}, {t['status']}, priority {t['priority']}"
                     for t in data.get("tickets", [])]
            return LLMResult(text="Your support tickets:\n" + "\n".join(lines) if lines
                             else "You have no support tickets.")
        if name == "lookup_product":
            products = data.get("products", [])
            if not products:
                return LLMResult(text="I couldn't find a matching product in the catalog.")
            return LLMResult(text="\n".join(
                f"{p['name']} ({p['product_id']}): {p['price']}, {p['stock_status']}. "
                f"{p['description']} Specs: {p['specs']}" for p in products[:3]))
        if name in ("search_policies", "get_refund_policy"):
            passages = data.get("passages", [])
            extra = ""
            order = data.get("order")
            if order:
                extra = (f"\nOrder {order['order_id']}: payment {order['payment_status']}, "
                         f"refund {order['refund_status']}"
                         + (f" of {order['refund_amount']}" if order.get("refund_amount") else "") + ".")
            if data.get("policy_unavailable"):
                return LLMResult(text="I can't look up our refund policy right now, so I can't confirm it. "
                                      "I can raise a support ticket if you like." + extra)
            if not passages:
                return LLMResult(text="I can't confirm that from our policies. I can raise a support ticket if you like." + extra)
            top = passages[0]
            return LLMResult(text=f"{top['text']} (Source: {top['source']}, {top['section']}){extra}")
        return LLMResult(text="Here is what I found.")

    @staticmethod
    def _render_order(order: dict[str, Any]) -> str:
        items = "; ".join(f"{i['item_id']} {i['product_name']} x{i['quantity']} at {i['price']}"
                          for i in order.get("items", []))
        text = (f"Order {order['order_id']}: {order['status']}. Ordered {order['order_date']}"
                + (f", delivered {order['delivered_at']}" if order.get("delivered_at") else "")
                + f". Total {order['total']}. Payment: {order['payment_status']}. "
                  f"Refund: {order['refund_status']}"
                + (f" ({order['refund_amount']})" if order.get("refund_amount") else "") + ".")
        return text + (f" Items: {items}." if items else "")
