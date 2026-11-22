"""Pure helpers of the fresh-description ticket flow (Phase 1.1). No state, no database, no model.

Owner-approved here (business rules 9.4, 9.5, 9.13):

* B-10: the CLOSED English list of bare category words, generic words and acknowledgements, matched on the ENTIRE message,
  case-insensitively, with leading and trailing whitespace and surrounding punctuation ignored for COMPARISON ONLY. No substring,
  stemming, plural, fuzzy or synonym matching. The customer's text is never modified. Hindi and Hinglish lists are inactive.
* E-1: the exact phrase "raise a ticket" is a ticket request, not a description (compared the same way as B-10).
* The description is validated, never changed: blank, NUL, a lone surrogate or more than 2,000 Unicode code points is rejected.

Proposed internal mechanics [R] (state names, the order of the checks) live in the engine, which calls these functions.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from app.orchestrator import guard

DESCRIPTION_LIMIT = 2000  # Unicode code points (OD-23); the database CHECK and ``TICKET_TEXT_MAX`` agree

# The approved closed English list (B-10): category-only words, generic words or phrases, acknowledgements.
CATEGORY_WORDS = frozenset({"return", "payment", "refund", "order", "damaged"})
GENERIC_WORDS = frozenset({"help", "support", "ticket", "complaint", "i need support"})
ACKNOWLEDGEMENTS = frozenset({"yes", "ok"})
BARE_ENTRIES = CATEGORY_WORDS | GENERIC_WORDS | ACKNOWLEDGEMENTS

RAISE_A_TICKET = "raise a ticket"  # E-1
_RAISE_A_TICKET_WORDS = re.compile(r"(?<!\w)raise a ticket(?!\w)", re.IGNORECASE)
_NEGATION = re.compile(r"(?<!\w)(?:not|never|no need|dont|don't|do not|won't|wont|can't|cant|cannot)(?!\w)", re.IGNORECASE)

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def comparison_form(text: object) -> str:
    """The form used ONLY to compare a message with the closed list: surrounding whitespace and punctuation removed, case folded.
    The original text is never replaced by this value."""
    if not isinstance(text, str):
        return ""
    chars = list(text)
    start, end = 0, len(chars)
    while start < end and (chars[start].isspace() or unicodedata.category(chars[start]).startswith("P")):
        start += 1
    while end > start and (chars[end - 1].isspace() or unicodedata.category(chars[end - 1]).startswith("P")):
        end -= 1
    return "".join(chars[start:end]).casefold()


def is_bare_description(text: object) -> bool:
    """True when the ENTIRE message equals an entry of the approved closed list (B-10)."""
    return comparison_form(text) in BARE_ENTRIES


def is_raise_a_ticket_phrase(text: object) -> bool:
    """E-1: the exact phrase "raise a ticket", compared as in B-10. Not a description and not on the closed list."""
    return comparison_form(text) == RAISE_A_TICKET


def contains_raise_a_ticket(text: object) -> bool:
    """E-1 while a description is awaited: the exact words "raise a ticket" on word boundaries, even inside an ordinary sentence
    ("I want to raise a ticket", "please raise a ticket"). No synonyms, stemming or other intent. A question ("how do I raise a
    ticket?") or a negated sentence ("don't raise a ticket") is not a request and keeps its normal handling. Kept apart from the
    B-10 closed list, and used only to classify a message while a draft is open (idle starts use ``is_ticket_request``)."""
    if not isinstance(text, str) or _RAISE_A_TICKET_WORDS.search(text) is None:
        return False
    return not guard.is_question(text) and _NEGATION.search(text) is None


def validate_description(text: object) -> str:
    """``"ok"``, ``"over_limit"`` or ``"bare"`` (blank, unusable or on the closed list). Never modifies the text.

    Over-limit is checked first: a text longer than 2,000 code points can never be bare. A text the repository would refuse
    (NUL, a lone surrogate, blank) is treated as bare so the customer is asked to describe the problem again.
    """
    if not isinstance(text, str):
        return "bare"
    if len(text) > DESCRIPTION_LIMIT:
        return "over_limit"
    if not text.strip() or "\x00" in text or _LONE_SURROGATE.search(text):
        return "bare"
    if is_bare_description(text):
        return "bare"
    return "ok"


# --- classification of a message while a draft exists (the order itself is a proposed mechanic [R]) ---------------------

def is_ticket_request(text: str) -> bool:
    """A customer-originated request for a ticket, identified from the CURRENT customer message only."""
    return is_raise_a_ticket_phrase(text) or guard.is_ticket_request(text)


def is_explicit_ticket_ask(text: str) -> bool:
    """An explicit ask to raise a ticket in plain words ("I want to raise a ticket", "please raise a ticket"), used ONLY to start the
    flow from idle. It reuses the existing deterministic reply classifier (only an explicit yes or a plain request to raise a
    ticket is affirmative), so a bare "yes", a question or a negated sentence never qualifies. It is not E-1 and is not used to
    classify a message while a description is awaited."""
    return guard.classify_offer_reply(text) == "affirmative" and "ticket" in (text or "").lower()


def is_state_changing_request(text: str) -> bool:
    """Another state-changing request: a ticket, a return start or something the assistant cannot do itself (an address
    change). An addition of information is Phase 3 and is not recognised here."""
    # A return start is recognised only from explicit words ("I want to return it", "start a return"); a bare imperative inside a
    # description ("return my damaged order") is not treated as a second request [R].
    return (is_ticket_request(text) or contains_raise_a_ticket(text) or guard._starts_new_return((text or "").lower())
            or guard.unsupported_request(text) is not None)


def is_ambiguous(text: str) -> bool:
    """A complaint phrased as a question: never captured automatically; the customer chooses (OD-26)."""
    return guard.is_question(text) and guard.has_issue_cue(text)


def is_read_only(text: str) -> bool:
    """A read-only support query or a topic change that changes no state: it is answered and never captured."""
    if guard.classify_scope(text, specific=guard.mentions_order_or_item(text)) in ("greeting", "thanks", "out_of_scope"):
        return True
    if guard.is_more_tickets_request(text) or guard.asks_about_the_ticket(text) or guard.asks_for_own_orders(text):
        return True
    return guard.is_question(text) and not guard.has_issue_cue(text)


def classify_draft_message(text: str) -> str:
    """``conflict``, ``read_only``, ``ambiguous`` or ``candidate`` (the control words and the typed order correction are
    classified by the engine before this, because they need the open card)."""
    if is_state_changing_request(text):
        return "conflict"
    if is_ambiguous(text):
        return "ambiguous"
    if is_read_only(text):
        return "read_only"
    return "candidate"


def allocate_request_key(random_hex: str) -> str:
    """A request key is an opaque server-generated token. It is never derived from customer text or supplied by the model."""
    return "rk_" + random_hex


def describe_order_label(order: dict) -> str:
    """The label of an order button: its ID and status (identifiers are never translated)."""
    status: Optional[str] = order.get("status")
    return f"{order['order_id']} ({status})" if status else str(order["order_id"])
