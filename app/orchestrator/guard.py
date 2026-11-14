"""Deterministic scope guard (docs/03 Section 8.5).

Runs before the LLM. It decides only cheap, safe things: greetings,
thanks, clearly out-of-scope requests, operations this assistant cannot
perform, and ambiguous problem reports that need a clarifying question.
Everything else goes to the orchestrator.
"""

from __future__ import annotations

import re
import unicodedata
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
# An explicit request to raise a concern or complaint. Kept identical to
# ``app.llm.rule_based.TICKET_REQUEST_PHRASES`` (a test enforces that).
TICKET_REQUEST_PHRASES = ("raise my concern", "raise a concern", "have a concern", "have a complaint",
                          "make a complaint", "file a complaint", "lodge a complaint", "want to complain",
                          "need to complain", "report an issue", "report a problem")

TICKET_TEXT_LIMIT = 2000  # the database's ticket text limit (app.db.validation.TICKET_TEXT_MAX)
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


def is_ticket_request(text: str) -> bool:
    """True for an explicit request to raise a concern or complaint (not an informational question)."""
    lower = text.lower()
    return (any(phrase in lower for phrase in TICKET_REQUEST_PHRASES)
            and not any(cue in lower for cue in _INFO_CUES + ("how can i",)))


def unsupported_operation(text: str) -> Optional[str]:
    for pattern, label in _UNSUPPORTED:
        if pattern.search(text):
            return label
    return None


def unsupported_request(text: str) -> Optional[str]:
    """The thing the assistant cannot do that *text* asks for, or ``None``.

    Only an imperative request counts ("cancel my order"); a question about it ("can I cancel my order?") is
    answered from the policy text, exactly as ``classify_scope`` decides.
    """
    if _QUESTION_START.match(text) or _QUESTIONISH.match(text):
        return None
    return unsupported_operation(text)


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
            if is_ticket_request(text):
                return "normal"  # "I have a complaint" asks for a ticket: let the assistant start that flow
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


def _word_tokens(text: str) -> list[str]:
    """Lower-case words in any script.

    A word is a run of letters and digits (Unicode, so Devanagari and other scripts count) plus the
    combining marks that belong to it (vowel signs, nukta, virama) and apostrophes. Punctuation, including
    the Devanagari danda, separates words. For ASCII text this is the same as ``[a-z0-9']+``.
    """
    tokens: list[str] = []
    current: list[str] = []
    for ch in text.lower():
        joins_word = ch.isalnum() or ch == "'" or (
            current and (unicodedata.category(ch).startswith("M") or ch in "\u200c\u200d"))
        if joins_word:
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


# Nouns that name a product, not a reason. "I want to return the charger" says what, not why. Only used when the
# caller passes them as ``ignore_words``; the item's own name words are added by ``product_words``.
PRODUCT_WORDS = frozenset("""earbuds earbud headphones headphone charger monitor cable hdmi laptop phone speaker
keyboard mouse""".split())


def product_words(name: str) -> frozenset[str]:
    """The words of a product name, in the singular and as written, for ``has_explanation(ignore_words=...)``."""
    words: set[str] = set()
    for token in _word_tokens(name):
        words.add(token)
        if token.endswith("s") and len(token) > 3:
            words.add(token[:-1])
    return frozenset(words)


_RESOLVE_IGNORED = frozenset({"the", "and", "for", "with", "from", "pro", "new"})


def _singular_forms(tokens: list[str]) -> set[str]:
    return {w for t in tokens for w in ((t, t[:-1]) if t.endswith("s") and len(t) > 3 else (t,))}


def resolve_item_by_name(text: str, items: list[dict]) -> Optional[str]:
    """The ``item_id`` of the one item on an order that the customer's own words name, else ``None``.

    Items are told apart by the words of their product names that no other item on the order shares. Two
    matches, no match, or an unrecognised language is never guessed (``None``): the caller then asks the model
    or the customer. A single-item order resolves when nothing else is named. It only picks which item a return
    request is about; eligibility, the reason, the category and the Confirm are all decided later.
    """
    if not items:
        return None
    message = _singular_forms(_word_tokens(_ID_TOKEN.sub(" ", text)))
    per_item = {i["item_id"]: {w for w in _singular_forms(_word_tokens(str(i.get("product_name", ""))))
                               if len(w) > 2 and w not in _RESOLVE_IGNORED} for i in items}
    matched = []
    for item_id, words in per_item.items():
        others = set().union(*(w for other, w in per_item.items() if other != item_id)) if len(per_item) > 1 else set()
        if (words - others) & message:
            matched.append(item_id)
    if len(matched) == 1:
        return matched[0]
    if matched or len(items) != 1:
        return None
    (only_id,) = per_item
    return only_id if not ((message & _singular_forms(sorted(PRODUCT_WORDS))) - per_item[only_id]) else None


def has_explanation(text: str, ignore_words: frozenset[str] = frozenset()) -> bool:
    """True if the message says something beyond a bare return request.

    Order and item IDs, the usual request or acknowledgement words and ``ignore_words`` (product names:
    naming the item is not a reason) are ignored. Words are read in any script, so a reason written in
    Hindi counts. This only decides whether to ask the customer for a reason; it never rewrites or
    selects text.
    """
    tokens = _word_tokens(_ID_TOKEN.sub(" ", text))
    for index, t in enumerate(tokens):
        if t in _NEGATION_WORDS and index + 1 < len(tokens) and _is_ignored_product(tokens[index + 1], ignore_words):
            return True  # "no power" says what is wrong; the product word alone ("power bank") does not
        if t in _REQUEST_WORDS or _is_ignored_product(t, ignore_words):
            continue
        if _letter_count(t) > 1:
            return True
    return False


_NEGATION_WORDS = frozenset({"no", "not", "without", "never", "nahi", "nahin", "\u0928\u0939\u0940\u0902", "\u0928\u0939\u0940"})


def _is_ignored_product(token: str, ignore_words: frozenset[str]) -> bool:
    singular = token[:-1] if token.endswith("s") and len(token) > 3 else token
    return token in ignore_words or singular in ignore_words


def _letter_count(token: str) -> int:
    """Letters and digits in a token, not counting vowel signs, other combining marks or apostrophes.

    A single Devanagari consonant with its vowel sign ("\u0915\u093e") is one letter, and ``''`` or ``a'`` has
    none or one, so none of them counts as an explanation; "\u091f\u0942\u091f\u093e" (broken) and "don't" do.
    """
    return sum(1 for ch in token if ch.isalnum())


def has_words(text: str) -> bool:
    """True if the text contains at least one real word (not only punctuation, symbols or single letters)."""
    return any(_letter_count(t) > 1 for t in _word_tokens(_ID_TOKEN.sub(" ", text)))


def names_other_target(text: str, order_id: str, item_id: str) -> bool:
    """True if the text names an order or item ID other than the given ones."""
    named = {match.upper() for match in _ID_TOKEN.findall(text)}
    return bool(named - {order_id.upper(), item_id.upper()})


def is_cancel(text: str) -> bool:
    """True for the typed word ``cancel`` (any case, optional punctuation)."""
    return _CANCEL.match(text) is not None


def _lexicon(words: str) -> frozenset[str]:
    return frozenset(unicodedata.normalize("NFC", w) for w in words.split())


# Hindi (Devanagari) and Hinglish (Hindi in Latin letters) cues. A message that asks something, asks where the
# order is, or asks for the money back is not a return reason; a message that gives a reason ("because", or a
# defect word) is, even when it also mentions a refund. Matching is on whole words, after Unicode NFC.
_HI_QUESTION = _lexicon("""क्या कहाँ कहां कब कैसे कैसा कितना कितने कितनी कौन कौनसा कौन-सा क्यों
kya kaha kahan kahaan kab kaise kaisa kitna kitne kitni kaun kon kyu kyun kyon""")
_HI_TRACKING = _lexicon("""आएगा आयेगा आएगी पहुंचेगा पहुँचेगा ट्रैक स्टेटस aayega ayega aayegi pahunchega""")
_HI_MONEY = _lexicon("""रिफंड पैसा पैसे पैसों रुपये रुपए refund paisa paise paiso rupaye""")
_HI_ASKING = _lexicon("""चाहिए चाहिये चाहता चाहती वापस दो दीजिए दीजिये chahiye chahie chahta chahti wapas vapas
dijiye""")
_HI_REASON_CUES = _lexicon("""क्योंकि क्योकि खराब ख़राब खराबी टूटा टूटी टूटे गलत बेकार आवाज़ आवाज नहीं नही
kyunki kyuki kyonki kharab kharaab kharabi toota tuta tooti galat bekaar bekar awaaz aawaz avaaz nahi nahin because""")
_HI_PLEASANTRIES = _lexicon("""नमस्ते नमस्कार धन्यवाद शुक्रिया namaste namaskar dhanyavad dhanyawad shukriya""")


def _hindi_off_topic(text: str) -> bool:
    """True for a Hindi or Hinglish message that asks something, tracks an order, asks for money or only greets."""
    tokens = {unicodedata.normalize("NFC", t) for t in _word_tokens(text)}
    if not tokens or tokens & _HI_REASON_CUES:
        return False
    if tokens <= _HI_PLEASANTRIES:
        return True
    if tokens & (_HI_QUESTION | _HI_TRACKING):
        return True
    return bool(tokens & _HI_MONEY and tokens & _HI_ASKING)


_YES_STRONG = _lexicon("""yes yeah yep yup sure haan han ji \u0939\u093e\u0901 \u0939\u093e\u0902 \u091c\u0940 ahead""")
_YES_FILLER = _lexicon("""please pls plz thanks thank you ok okay go do it that raise a ticket i want to would like
need create make the one""")
_NO_STRONG = _lexicon("""no nope nah never nahi nahin \u0928\u0939\u0940\u0902 \u0928\u0939\u0940""")
_NO_FILLER = _lexicon("""thanks thank you not now please i don't dont want to it that need a ticket one anymore""")


def classify_offer_reply(text: str) -> str:
    """How a typed reply answers an offer made in text: ``"affirmative"``, ``"negative"`` or ``"ambiguous"``.

    Only an explicit yes (or a plain request to raise a ticket) is affirmative and only an explicit no is
    negative. An acknowledgement ("ok thanks", "please", "ok"), a question, a mixed or empty reply is ambiguous.
    This only answers the offer; a typed reply never confirms a pending action.
    """
    tokens = {unicodedata.normalize("NFC", t) for t in _word_tokens(text)}
    if not tokens:
        return "ambiguous"
    if tokens & _YES_STRONG and not tokens & _NO_STRONG and tokens <= (_YES_STRONG | _YES_FILLER):
        return "affirmative"
    if {"raise", "ticket"} <= tokens and tokens <= (_YES_STRONG | _YES_FILLER):
        return "affirmative"
    if not tokens & _YES_STRONG:
        if tokens & _NO_STRONG and tokens <= (_NO_STRONG | _NO_FILLER):
            return "negative"
        if tokens & {"don't", "dont"} and tokens <= (_NO_STRONG | _NO_FILLER):
            return "negative"
        if tokens == {"not", "now"}:
            return "negative"
    return "ambiguous"


def is_new_request_not_a_reply(text: str) -> bool:
    """True if a message sent while an offer is open is plainly a new request, not a (possibly unclear) answer.

    It names an order or item, is a question, or is longer than a short reply. Such a message is handled
    normally instead of being swallowed by the offer; a short acknowledgement ("ok thanks") is not.
    """
    return (mentions_order_or_item(text) or text.strip().endswith("?") or _QUESTIONISH.match(text) is not None
            or len(_word_tokens(text)) > 5)


_NEW_RETURN = re.compile(
    r"\b(?:want|wants|like|need|wish|going|plan|planning|ready)\s+to\s+return\b|\bstart\s+(?:a|the|my)\s+return\b")
_NEGATED_BEFORE = re.compile(r"\b(?:not|never|no longer)\b|n['\u2019]t\b")
_LOOKUP_TOPIC = re.compile(
    r"\b(?:orders?|track(?:ing)?|status|polic(?:y|ies)|price|stock|deliver(?:y|ed)?|where|when|my (?:returns?|tickets?))\b")
_PROBLEM_WORDS = re.compile(r"\b(?:not|no|never|without|cannot|won['\u2019]?t|doesn['\u2019]?t|didn['\u2019]?t|hasn['\u2019]?t)\b|n['\u2019]t\b")


def _starts_new_return(text: str) -> bool:
    """True if the message itself starts a distinct return request ("I want to return it because ...")."""
    lower = text.lower()
    for match in _NEW_RETURN.finditer(lower):
        if not _NEGATED_BEFORE.search(lower[max(0, match.start() - 25):match.start()]):
            return True
    return False


def _is_lookup_question(text: str) -> bool:
    """A question about an order, delivery or policy that describes no problem (so it is not ticket detail)."""
    lower = text.lower()
    asks = lower.strip().endswith("?") or _QUESTIONISH.match(lower) is not None
    return (asks and _LOOKUP_TOPIC.search(lower) is not None and not has_issue_cue(text)
            and _PROBLEM_WORDS.search(lower) is None)


def _hindi_lookup(text: str) -> bool:
    tokens = {unicodedata.normalize("NFC", t) for t in _word_tokens(text)}
    return bool(tokens & (_HI_QUESTION | _HI_TRACKING)) and not tokens & _HI_REASON_CUES


def ticket_detail_problem(text: str, order_id: str, item_id: str,
                          ignore_words: frozenset[str] = frozenset()) -> Optional[str]:
    """Why *text* cannot be the description of a support ticket for this order and item, or ``None``.

    Codes: ``meaningless`` (empty, punctuation, an acknowledgement or only the product name), ``too_long``,
    ``other_target`` (names another order or item), ``new_return`` (starts a distinct return request),
    ``unrelated`` (a greeting, an out-of-scope request or a lookup question). A question mark, a question word
    or a request for a replacement does not invalidate a description. Nothing is rewritten.
    """
    if not text or not text.strip():
        return "meaningless"
    if len(text) > TICKET_TEXT_LIMIT:
        return "too_long"
    named = {match.upper() for match in _ID_TOKEN.findall(text)}
    if named - {order_id.upper(), item_id.upper()}:
        return "other_target"
    if _starts_new_return(text):
        return "new_return"
    if not has_explanation(text, ignore_words):
        return "meaningless"
    if classify_scope(text, specific=mentions_order_or_item(text)) in ("greeting", "thanks", "out_of_scope"):
        return "unrelated"
    if _is_lookup_question(text) or _hindi_lookup(text):
        return "unrelated"
    return None


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
    if _hindi_off_topic(text):
        return True
    return text.strip().endswith("?") or _QUESTIONISH.match(text) is not None
