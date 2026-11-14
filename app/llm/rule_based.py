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
# Words that say a return is wanted ("I want to return it") rather than asked about ("can I return it?").
_RETURN_EXPLICIT = ("want to return", "like to return", "need to return", "start a return")
_CLAUSE_BREAK = re.compile(r"[.,;:!?]|\b(?:but|so|and|because|however|since|although)\b")
_NEGATION = re.compile(r"\b(?:not|never|no longer|without|cannot|dont|wont|cant)\b|n['\u2019]t\b")
_QUESTION_LEAD = re.compile(
    r"^\s*(?:(?:so|and|ok|okay|well|hi|hello|hey)\b[,\s]*)*"
    r"(?:can i|could i|may i|should i|am i|is it|is there|are there|do i|does|did|will i|would i|"
    r"what|when|how|which|why|where|who)\b")
_QUESTION_WORD = re.compile(r"\b(?:how|what|when|whether|which|why|if)\b")
_POLITE_REQUEST = re.compile(r"\b(?:can|could|would|will) you\b|\bplease\b")
_TICKET_CUES = ("ticket", "escalate", "human", "support agent", "speak to someone", "talk to")
# Explicit requests to raise a concern or complaint. Kept identical to ``guard.TICKET_REQUEST_PHRASES``
# (a test enforces that): the guard lets these through to the model instead of asking a vague-problem question.
TICKET_REQUEST_PHRASES = ("raise my concern", "raise a concern", "have a concern", "have a complaint",
                          "make a complaint", "file a complaint", "lodge a complaint", "want to complain",
                          "need to complain", "report an issue", "report a problem")
_TICKET_INFO_CUES = _INFO_CUES + ("can i", "how can i")
# "How many orders do I have?", "Count my orders", "Do I have any orders?" ask about the customer's own
# order count. Policy questions that merely contain "how many" ("how many days ...") do not match.
_COUNT_RE = re.compile(r"\b(?:how many|number of|count)\b.*\borders?\b"
                       r"|\bdo i have (?:any|an)\b.*\borders?\b"
                       r"|\bhave i (?:placed|made|got) (?:any|an)\b.*\borders?\b")
_COUNT_BLOCK_WORDS = frozenset("""day days week weeks hour hours return returns returned refund refunds
delivery deliver delivered shipping ship shipped shipment policy limit allowed maximum minimum free
cancel""".split())
_FIRST_PERSON = frozenset({"i", "my", "me"})


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP and len(w) > 2}


def _first(pattern: re.Pattern, text: str) -> Optional[str]:
    match = pattern.search(text)
    return match.group(0).upper() if match else None


_CAN_T_WAIT = re.compile(r"\b(?:can['’]?t|cannot) wait\b")  # "I can't wait to return it" is eager, not negated
_TARGET_CUT = re.compile(r"\b(?:for|because|as|since|with|from|due|by|in|on|at|within|after|before)\b")
_TARGET_GENERIC = frozenset("""the a an my this that these those it them one item items order orders product
products thing things purchase please now today again whole entire all everything of and""".split())


def return_clauses(lower: str) -> list[tuple[str, str]]:
    """Each clause that contains return wording, with what that clause is doing.

    A message is cut at punctuation and at words such as "but", "so" and "because". Every clause that
    mentions returning an item is read on its own: ``"request"`` (the customer wants to return something),
    ``"negated"`` ("I don't want to return it") or ``"question"`` (eligibility, the window, how returns work).
    A negation, question word or exclamation in one clause never changes the reading of another.
    """
    clauses: list[tuple[str, str]] = []
    position = 0
    pieces: list[tuple[str, str]] = []
    for match in _CLAUSE_BREAK.finditer(lower):
        pieces.append((lower[position:match.start()], match.group(0)))
        position = match.end()
    pieces.append((lower[position:], ""))
    for clause, terminator in pieces:
        starts = [clause.find(phrase) for phrase in _RETURN_ACTION if phrase in clause]
        if not starts:
            continue
        before = _CAN_T_WAIT.sub(" ", clause[:min(starts)])
        explicit = any(phrase in clause for phrase in _RETURN_EXPLICIT)
        if _QUESTION_LEAD.match(clause):
            intent = "question"
        elif _NEGATION.search(before):
            intent = "negated"
        elif _QUESTION_WORD.search(before):
            intent = "question"
        elif terminator == "?" and not explicit and not _POLITE_REQUEST.search(lower):
            intent = "question"
        else:
            intent = "request"
        clauses.append((intent, clause))
    return clauses


def return_intent(lower: str) -> Optional[str]:
    """What a message that mentions returning an item is doing.

    ``"request"``, ``"negated"`` or ``"question"``, or ``None`` when no return wording is present. Every
    clause is read separately (see ``return_clauses``): any affirmative request wins, so "I don't want to return
    the monitor, I want to return the charger" is a request; otherwise a question wins over a negation. This only
    picks a route; a return still needs the customer's order, item, reason, category and an explicit Confirm
    click, and the item is chosen by ``select_return_item`` or by asking.
    """
    intents = [intent for intent, _ in return_clauses(lower)]
    if not intents:
        return None
    for wanted in ("request", "question"):
        if wanted in intents:
            return wanted
    return "negated"


def _name_tokens(text: str) -> set[str]:
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w
            for w in _WORD_RE.findall(text.lower()) if len(w) > 2 and w not in _STOP}


def select_return_item(lower: str, items: list[dict[str, Any]]) -> Optional[str]:
    """The item ID an affirmative return request clearly refers to, or ``None`` (ask the customer).

    Items are matched by the words of their product name in the request clauses only. An item the customer
    said they do not want to return is never chosen, an unrecognised product word never falls back to the only
    item, and two matches are ambiguous. A single-item order is chosen only when the request names nothing else.
    """
    clauses = return_clauses(lower)
    requests = [c for intent, c in clauses if intent == "request"]
    negated_words: set[str] = set()
    for intent, clause in clauses:
        if intent == "negated":
            negated_words |= _name_tokens(clause)
    target_words: set[str] = set()
    residual: set[str] = set()
    for clause in requests:
        starts = [clause.find(phrase) + len(phrase) for phrase in _RETURN_ACTION if phrase in clause]
        after = clause[min(starts):]
        target_words |= _name_tokens(after)
        head = _TARGET_CUT.split(after, maxsplit=1)[0]
        residual |= {w for w in _WORD_RE.findall(_ITEM_RE.sub(" ", _ORDER_RE.sub(" ", head)))
                     if w not in _TARGET_GENERIC and w not in _STOP}
    # Words of the whole message after the first request clause also count ("the monitor and the charger").
    if requests:
        tail = lower[lower.find(requests[0]) + len(requests[0]):]
        target_words |= _name_tokens(re.split(r"[.!?;]", tail, maxsplit=1)[0]) if _is_list_tail(tail) else set()
    matches = [i for i in items if _name_tokens(str(i.get("product_name", ""))) & target_words]
    if len(matches) == 1:
        named = _name_tokens(str(matches[0].get("product_name", "")))
        return None if named & negated_words else matches[0]["item_id"]
    if matches or len(items) != 1 or residual:
        return None
    if _name_tokens(str(items[0].get("product_name", ""))) & negated_words:
        return None
    return items[0]["item_id"]


def _is_list_tail(tail: str) -> bool:
    """True when the text after a request clause just continues the list ("... and the charger")."""
    return tail.lstrip().startswith("and ")


def _asks_order_count(lower: str) -> bool:
    """True when the customer asks how many orders they have (not a policy question)."""
    if not _COUNT_RE.search(lower):
        return False
    words = set(_WORD_RE.findall(lower))
    if words & _COUNT_BLOCK_WORDS or any(p in lower for p in ("can i", "could i", "may i")):
        return False
    return bool(words & _FIRST_PERSON)


def _is_ticket_request(lower: str) -> bool:
    """True for an explicit request to raise a concern or complaint (not an informational question)."""
    return (any(phrase in lower for phrase in TICKET_REQUEST_PHRASES)
            and not any(cue in lower for cue in _TICKET_INFO_CUES))


def _order_from_history(history: list[dict[str, Any]]) -> Optional[str]:
    """The order ID the customer most recently typed, or ``None`` when there is none or it is ambiguous.

    Only the customer's own earlier messages are read: the assistant's replies can contain example or
    listed IDs the customer never chose ("for example ORD1001"). If the latest message that names an
    order names more than one, nothing is guessed. The caller still sends the ID through the normal
    tool, so ownership is checked as for any other order lookup.
    """
    for message in reversed(history):
        if message.get("role") != "user":
            continue
        ids = list(dict.fromkeys(m.upper() for m in _ORDER_RE.findall(str(message.get("content", "")))))
        if ids:
            return ids[0] if len(ids) == 1 else None
    return None


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
        return self._decide(str(last.get("content", "")), messages[:-1])

    def _decide(self, text: str, history: Optional[list[dict[str, Any]]] = None) -> LLMResult:
        lower = text.lower()
        order, item = _first(_ORDER_RE, text), _first(_ITEM_RE, text)
        if "my tickets" in lower or "ticket status" in lower or "my support tickets" in lower:
            return LLMResult(tool_calls=(ToolCall("get_my_tickets", {}),))
        if any(cue in lower for cue in _TICKET_CUES) or _is_ticket_request(lower):
            args: dict[str, Any] = {"issue_type": self._issue_type(lower)}
            if order:
                args["order_id"] = order
            return LLMResult(tool_calls=(ToolCall("start_support_ticket", args),))
        intent = return_intent(lower)
        if intent == "negated":
            return LLMResult(text="Okay, I won't start a return. Let me know if there's anything else I can help with.")
        if intent == "question":  # eligibility and window questions are policy questions, not return requests
            return LLMResult(tool_calls=(ToolCall("search_policies", {"query": text[:200]}),))
        if intent == "request":
            if not order:
                order = _order_from_history(history or [])  # "return it": the order the customer last named
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
        if "my orders" in lower or "list orders" in lower or "all my orders" in lower or _asks_order_count(lower):
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
            if return_intent(lower) == "request":
                chosen = select_return_item(lower, items)
                if chosen:
                    return LLMResult(tool_calls=(ToolCall("start_return_request", {
                        "order_id": data["order_id"], "item_id": chosen}),))
                names = ", ".join(f"{i['item_id']} ({i['product_name']})" for i in items)
                return LLMResult(text=f"Which item would you like to return? This order has: {names}.")
            return LLMResult(text=self._render_order(data))
        if name == "list_my_orders":
            lines = [f"{o['order_id']}: {o['status']}, ordered {o['order_date']}, total {o['total']}"
                     for o in data.get("orders", [])]
            user = next((m for m in reversed(messages) if m["role"] == "user"), {})
            if _asks_order_count(str(user.get("content", "")).lower()):
                total = data.get("total_orders", len(lines))  # the tool's own total, not the rows it shows
                text = f"You have {total} {'order' if total == 1 else 'orders'}."
                if lines:
                    text += "\n" + "\n".join(lines)
                    if total > len(lines):
                        text += f"\n(Showing {len(lines)} of {total}.)"
                return LLMResult(text=text)
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
