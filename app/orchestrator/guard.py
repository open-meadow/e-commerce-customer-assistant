"""Deterministic scope guard (docs/03 Section 8.5).

Runs before the LLM. It decides only cheap, safe things: greetings,
thanks, clearly out-of-scope requests, operations this assistant cannot
perform, and ambiguous problem reports that need a clarifying question.
Everything else goes to the orchestrator.
"""

from __future__ import annotations

import re
from typing import Optional

_ORDER_RE = re.compile(r"\bORD\d+\b", re.I)
_ITEM_RE = re.compile(r"\bIT\d+\b", re.I)
_WORD_RE = re.compile(r"[a-z0-9']+")

_GREETING = re.compile(r"^\s*(hi|hello|hey|hii+|good (morning|afternoon|evening)|namaste)\b[\s!.,?]*$", re.I)
_THANKS = re.compile(r"^\s*(thanks|thank you|thx|ok thanks|okay thanks|bye|goodbye)\b[\s!.,?a-z]*$", re.I)

IN_SCOPE_WORDS = frozenset("""order orders return returns refund refunds ticket tickets payment paid
delivery delivered shipping shipped product products price stock item items earbuds earbud headphones
charger monitor cable hdmi power bank battery warranty policy cancel exchange replacement support
agent damaged broken wrong missing late delay invoice account""".split())

_OUT_OF_SCOPE_CUES = ("weather", "joke", "poem", "recipe", "capital of", "president", "prime minister",
                      "write code", "python script", "javascript", "movie", "song", "lyrics", "football",
                      "cricket score", "translate", "horoscope", "bitcoin", "stock market", "homework",
                      "story about")

_UNSUPPORTED = (
    (re.compile(r"\b(cancel|cancell?ing)\b.*\border\b|\border\b.*\bcancel", re.I), "cancel an order"),
    (re.compile(r"\b(change|update|edit|modify)\b.*\b(address|phone|email|payment method)\b", re.I),
     "change account or delivery details"),
    (re.compile(r"\b(change|modify|edit|update)\b.*\border\b", re.I), "modify an order"),
    (re.compile(r"\b(replacement|replace my|exchange)\b", re.I), "arrange a replacement or exchange"),
    (re.compile(r"\b(delete|close)\b.*\baccount\b", re.I), "change your account"),
    (re.compile(r"\b(coupon|discount code|promo code)\b", re.I), "apply discounts"),
)

_ISSUE_CUES = ("not working", "broken", "damaged", "wrong", "late", "delay", "missing", "charged",
               "failed", "defect", "problem", "issue", "stopped", "no sound", "complain", "not received",
               "never arrived", "didn't arrive", "doesn't work", "doesnt work", "not delivered",
               "overcharged", "double charged", "faulty", "cracked", "refund not")
_INFO_CUES = ("policy", "what is", "how long", "how do i", "do you", "what are", "how many", "can i")

_ID_TOKEN = re.compile(r"\b(?:ORD|IT)\d+\b", re.I)
_REQUEST_WORDS = frozenset("""i want to would like need return returning item items from order my the please
a an it this send back start request refund for of can could you help me with hi hello hey thanks thank is are
be am and also just so that those these yes no yeah yep nope ok okay sure confirm confirmed""".split())
_CANCEL = re.compile(r"^\s*cancel[\s.!]*$", re.I)

_QUESTION_START = re.compile(
    r"^\s*(can i|could i|may i|how do i|how can i|what is|what are|what's|is it possible|am i able|when can i)\b",
    re.I)

SCOPE_REDIRECT = ("I can only help with your orders, returns, refunds, products and support tickets. "
                  "Is there something like that I can help with?")
GREETING_REPLY = ("Hello! I can check order status, answer refund and policy questions, look up products, "
                  "start return requests and raise support tickets. What can I help you with?")
THANKS_REPLY = "You're welcome! Let me know if there's anything else I can help with."


def mentions_order_or_item(text: str) -> bool:
    return bool(_ORDER_RE.search(text) or _ITEM_RE.search(text))


def has_issue_cue(text: str) -> bool:
    lower = text.lower()
    return any(cue in lower for cue in _ISSUE_CUES)


def unsupported_operation(text: str) -> Optional[str]:
    for pattern, label in _UNSUPPORTED:
        if pattern.search(text):
            return label
    return None


def unsupported_reply(label: str) -> str:
    return (f"I'm not able to {label} myself. I can look up orders, answer policy questions, start return "
            "requests and raise a support ticket so a human agent can follow up. "
            "Would you like me to create a ticket for this?")


def classify_scope(text: str, *, specific: bool) -> str:
    """Return greeting, thanks, out_of_scope, unsupported, ambiguous, issue or normal.

    ``specific`` says whether the message names something concrete (an
    order, item or catalog product), supplied by the caller.
    """
    if _GREETING.match(text):
        return "greeting"
    if _THANKS.match(text):
        return "thanks"
    lower = text.lower()
    words = set(_WORD_RE.findall(lower))
    if not (words & IN_SCOPE_WORDS) and not mentions_order_or_item(text):
        if any(cue in lower for cue in _OUT_OF_SCOPE_CUES):
            return "out_of_scope"
    if unsupported_operation(text) and not _QUESTION_START.match(text):
        # An imperative request ("cancel my order") is something this assistant cannot do.
        # A question about it ("can I cancel my order?") is answered from the policy text.
        return "unsupported"
    if has_issue_cue(text) and not any(c in lower for c in _INFO_CUES):
        long_enough = len(words) >= 8
        if not mentions_order_or_item(text) and not specific and not long_enough:
            return "ambiguous"
        return "issue"
    return "normal"


def infer_issue_type(text: str) -> str:
    lower = text.lower()
    if "return" in lower:
        return "return"
    if any(w in lower for w in ("payment", "charged", "refund", "paid")):
        return "payment"
    if any(w in lower for w in ("order", "delivery", "shipping", "arrive", "delivered")):
        return "order"
    if any(w in lower for w in ("product", "earbud", "headphone", "charger", "monitor", "battery", "cable")):
        return "product"
    return "general"


def has_explanation(text: str) -> bool:
    """True if the message says something beyond a bare return request.

    Order and item IDs and the usual request or acknowledgement words are
    ignored. This only decides whether to ask the customer for a reason;
    it never rewrites or selects text.
    """
    tokens = _WORD_RE.findall(_ID_TOKEN.sub(" ", text.lower()))
    return any(t not in _REQUEST_WORDS and len(t) > 1 for t in tokens)


def is_cancel(text: str) -> bool:
    """True for the typed word ``cancel`` (any case, optional punctuation)."""
    return _CANCEL.match(text) is not None


_QUESTIONISH = re.compile(
    r"^\s*(what|where|when|who|why|how|which|show|list|track|status|tell me|do you|does|is there|are there|"
    r"can you|could you|will you)\b", re.I)


def looks_unrelated_to_a_reason(text: str, order_id: str, item_id: str) -> bool:
    """True if a message sent while a return reason is awaited is not a reason.

    Greetings, thanks, out-of-scope or unsupported requests, questions and
    commands, and anything that names a different order or item are not
    stored as the reason. The caller keeps the draft and asks again.
    """
    kind = classify_scope(text, specific=mentions_order_or_item(text))
    if kind in ("greeting", "thanks", "out_of_scope", "unsupported"):
        return True
    named = {match.upper() for match in _ID_TOKEN.findall(text)}
    if named - {order_id.upper(), item_id.upper()}:
        return True
    return text.strip().endswith("?") or _QUESTIONISH.match(text) is not None
