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

_GREETING = re.compile(
    r"^\s*(?:(?:hi|hello|hey|hii+|good (?:morning|afternoon|evening)|namaste|namaskar)\b|"
    r"\u0928\u092e\u0938\u094d\u0924\u0947|\u0928\u092e\u0938\u094d\u0915\u093e\u0930)[\s!.,?]*$", re.I)
_THANKS = re.compile(
    r"^\s*(?:(?:thanks|thank you|thx|ok thanks|okay thanks|bye|goodbye|shukriya|dhanyavaad|dhanyawad|dhanyavad)\b"
    r"[\s!.,?a-z]*|(?:\u0936\u0941\u0915\u094d\u0930\u093f\u092f\u093e|\u0927\u0928\u094d\u092f\u0935\u093e\u0926)"
    r"[\s!.,?]*)$", re.I)

IN_SCOPE_WORDS = frozenset("""order orders return returns refund refunds ticket tickets payment paid
delivery delivered shipping shipped product products price stock item items earbuds earbud headphones
charger monitor cable hdmi power bank battery warranty policy cancel exchange replacement support
agent damaged broken wrong missing late delay invoice account""".split())

_OUT_OF_SCOPE_CUES = ("weather", "joke", "poem", "recipe", "capital of", "president", "prime minister",
                      "write code", "python script", "javascript", "movie", "song", "lyrics", "football",
                      "cricket score", "translate", "horoscope", "bitcoin", "stock market", "homework",
                      "story about", "mausam", "chutkula", "kavita", "gaana", "rashifal", "\u092e\u094c\u0938\u092e",
                      "\u091a\u0941\u091f\u0915\u0941\u0932\u093e", "\u0915\u0935\u093f\u0924\u093e",
                      "\u0917\u093e\u0928\u093e", "\u0930\u093e\u0936\u093f\u092b\u0932")

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
GREETING_REPLY = ("Hello! I'm the store's automated support assistant. I can check order status, answer refund "
                  "and policy questions, look up products, start return requests and raise support tickets. "
                  "What can I help you with?")
THANKS_REPLY = "You're welcome! Let me know if there's anything else I can help with."


def mentions_order_or_item(text: str) -> bool:
    return bool(_ORDER_RE.search(text) or _ITEM_RE.search(text))


# The cues above as whole words or exact phrases. Substring matching counted "tissue" (issue), "translate", "latest" and
# "chocolate" (late) as store problems. Plural and inflected forms are written out instead.
_ISSUE_CUE_RE = re.compile(
    r"\b(?:not working|broken|damaged|wrong|late|delay(?:s|ed)?|missing|(?:over)?charged|failed|defect\w*|problems?"
    r"|issues?|stopped|no sound|complain\w*|not received|never arrived|didn['’]?t arrive|doesn['’]?t work"
    r"|not delivered|faulty|cracked|refund not)\b", re.I)


def has_issue_cue(text: str) -> bool:
    return bool(_ISSUE_CUE_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Ticket scope: is this a store complaint or service request, a personal or unrelated request, or unclear?
# ---------------------------------------------------------------------------
# A ticket is proposed only for "store" text. Neither a store word alone (a product name, an order ID, "money", "paid")
# nor a generic complaint word alone ("problem", "issue", "help") is enough: store text names something bought or a
# store service AND says what went wrong, or asks for something the assistant cannot do itself, or asks for a person
# about a named store matter. Store evidence wins over personal words ("the wedding gift I ordered arrived broken").
# Deterministic: no model decides it.

# Three tiers of "what is this about", from strongest to weakest:
#   TRANSACTION: something ordered, bought, shipped or refunded (an order ID counts as one). With any stated complaint it
#                is a store matter even when personal words are present ("the wedding gift I ordered arrived broken").
#   PRODUCT:     a product noun. With a complaint it is a store matter unless the message is also personal.
#   WEAK:        money, paid, payment, charge, box, cod, a bare "returned"... everyday words. They never authorise a ticket
#                alone, and with a generic complaint word ("late", "wrong") they are still not enough; only a
#                payment-specific complaint ("deducted", "charged twice", "paisa kat gaya") counts, and only when the message
#                is not personal.
_TRANSACTION_RE = re.compile(
    r"\b(?:orders?|ordered|ordr|oder|odr|parcels?|parcle|packages?|pakage|packet|delivery|delivered|deliver|delivry"
    r"|delivary|courier|shipment|shipping|shipped|purchase[ds]?|bought|buyed|refunds?|refnd|invoice|seller|packaging"
    r"|return (?:request|pickup|pick-up)|(?:my|the|this|that) returns?|mangwaya|mangwayi|mangaya|mangayi|kharida"
    r"|kharidi)\b"
    r"|ऑर्डर|पार्सल|डिलीवरी|रिफंड|रिफ़ंड|आइटम|रिटर्न", re.I)
_PRODUCT_RE = re.compile(
    r"\b(?:box|items?|products?|prodcut|earbuds?|headphones?|charger|monitor|cable|hdmi|laptop|phone|speaker|keyboard"
    r"|mouse|power bank|saman|samaan)\b|प्रोडक्ट|सामान", re.I)
_WEAK_SUBJECT_RE = re.compile(
    r"\b(?:payments?|paid|money|charge[ds]?|cod|warranty|packing|returns?|returned|exchange|replacement|paisa|paise"
    r"|wapas)\b|पैसे|भुगतान|पेमेंट", re.I)
# A complaint that is itself about money. It can stand with a weak subject.
_PAYMENT_COMPLAINT_RE = re.compile(
    r"\b(?:deducted|(?:over|double)[ -]?charged|charged (?:twice|two times|double|extra|more)|not refunded"
    r"|refund (?:not|still not|never)|payment (?:failed|declined|stuck|pending|issue|problem)"
    r"|kat gay\w*|kata|kaat liya|paise kat\w*|paisa kat\w*)\b|कट ग(?:ए|या|ई)", re.I)

_COMPLAINT_RE = re.compile(
    r"\b(?:broken|broke|snapped|break[s]?|damaged?|defective|faulty|cracked|crackling|noisy|hangs|freezes|heating up"
    r"|scratch(?:ed|es)?|dented|torn|leak(?:s|ing)?|overheat\w*|not coming|not come yet"
    r"|burn(?:ing|t|s)?|sparks?|sparking|smok(?:e|es|ing)|dead|not working|stopped|stops|isn['’]?t working"
    r"|not (?:connecting|pairing|syncing|starting|opening|responding)|(?:can['’]?t|cannot|unable to) (?:use|charge|connect"
    r"|pair|turn on|switch on|open|play|hear)|won['’]?t (?:connect|pair|start|open|charge|turn on)"
    r"|(?:doesn|don|didn|won)['’]?t (?:work|turn on|switch on|charge|power on)"
    r"|(?:does|do|did|will) not (?:work|turn on|switch on|charge|power on)|dont (?:work|turn on|charge)"
    r"|not (?:turning|switching|powering) on|not charging|no sound|missing|empty|wrong|fake|counterfeit"
    r"|late|delay(?:s|ed)?|not delivered|never (?:arrived|came|received|delivered)|(?:didn|hasn|haven)['’]?t"
    r" (?:arrive[d]?|come|came|receive[d]?|get|got)|(?:did|has|have) not (?:arrive[d]?|come|receive[d]?|get)"
    r"|not (?:arrived|come|came|received|reached)|not what i (?:expected|ordered)|not as (?:described|shown|advertised)"
    r"|disappointed|(?:poor|bad|cheap) quality|(?:doesn['’]?t|does not|dont) fit|too (?:small|big|tight|loose)"
    r"|uncomfortable|lost|stolen|wet|(?:over|double)[ -]?charged|charged (?:twice|two times|double|extra|more)"
    r"|deducted|not refunded|refund (?:not|still not|never)|rude|half|partial(?:ly)?|incomplete|short(?:age)?|less"
    r"|pending|stuck)\b"
    r"|\b(?:kharab|kharaab|toot\w*|tuta|tuti|tute|kaam nahi\w*|kaam nahin\w*|chal nahi\w*|chalta nahi|band ho gay\w*"
    r"|on nahi ho\w*|nahi aay\w*|nahin aay\w*|nahi aaya|nahi mil\w*|nahin mil\w*|galat|kat gay\w*|kata|kaat liya"
    r"|gayab|phat gay\w*|jal gay\w*|garam ho\w*|adha|aadha|adhe|aadhe|kam mila|poora nahi\w*"
    r"|pura nahi\w*)\b"
    r"|खराब|टूट|काम नहीं|आधा|आधे|पूरा नहीं"
    r"|नहीं आ(?:या|ई|ए)|नहीं मिल(?:ा|ी|े)"
    r"|गलत|कट ग(?:ए|या|ई)|गायब", re.I)
# खराब टूट "काम नहीं" "नहीं आया/आई/आए" "नहीं मिला/मिली/मिले" गलत "कट गए/गया/गई" गायब

# A service request about something bought that the assistant cannot do itself ("cancel ORD1014", "exchange it").
_SERVICE_REQUEST_RE = re.compile(
    r"\b(?:cancel\w*|exchange|exchanged|replace\w*|replacement|reschedule\w*|re-?deliver\w*|modify|"
    r"change (?:the |my )?(?:address|number|date|item)|update (?:the |my )?address)\b|रद्द", re.I)

# Asks for a person about a named store matter ("I want to talk to a human about my refund").
_PERSON_REQUEST_RE = re.compile(
    r"\b(?:human|person|agent|manager|representative|someone|somebody|real person|insaan|kisi se baat)\b"
    r"|इंसान|एजेंट", re.I)

_PERSONAL_RE = re.compile(
    r"\b(?:married|marry|marriage|wedding|engagement|girlfriend|boyfriend|wife|husband|fiance|fiancee|relationship"
    r"|breakup|break up|divorce|dating|crush|love life|confidence|confident|nervous|anxiety|anxious|stress(?:ed)?"
    r"|depress\w*|lonely|loneliness|motivation|self[- ]esteem|exams?|homework|assignment|interview|career|resume"
    r"|coding|code|python|javascript|programming|recipe|capital of|president|prime minister|weather|horoscope"
    r"|shaadi|shadi|biwi|pyaar|pyar|rishta|naukri|padhai|pariksha|ghabrahat|tension)\b"
    r"|शादी|घबराहट|तनाव|रिश्त"
    r"|नौकरी|परीक्षा|प्यार"
    r"|आत्मविश्वास", re.I)
# शादी घबराहट तनाव रिश्त नौकरी परीक्षा प्यार आत्मविश्वास


def ticket_scope(text: str, *, names_product: bool = False) -> str:
    """``store``, ``personal`` or ``unclear`` for the text a support ticket would be about.

    ``store``: the text names something bought or a store service AND states a concrete complaint, or asks for something
    the assistant cannot do itself (cancel an order, a replacement), or asks for a person about it. How much evidence is
    needed depends on the tier of the subject (see the tier comment above): a transaction word with a complaint is
    enough even beside personal words; a product noun with a complaint is enough unless the message is also personal;
    an everyday word such as money or paid needs a payment-specific complaint and a non-personal message.
    ``personal``: personal, relationship, coaching or unrelated words with no such store evidence.
    ``unclear``: anything else, including a bare order ID, a bare product name, "help me", "I have a problem" or abuse.
    ``names_product``: the caller recognised a catalogue product name in the text (counts as a product noun only).
    """
    text = unicodedata.normalize("NFC", text or "")
    lower = text.lower()
    personal = bool(_PERSONAL_RE.search(text) or any(cue in lower for cue in _OUT_OF_SCOPE_CUES))
    wanted = bool(_COMPLAINT_RE.search(text) or _PERSON_REQUEST_RE.search(text) or _SERVICE_REQUEST_RE.search(text))
    transaction = bool(mentions_order_or_item(text) or _TRANSACTION_RE.search(text))
    product = bool(names_product or _PRODUCT_RE.search(text))
    if transaction and wanted:
        return "store"
    if product and wanted and not personal:
        return "store"
    if _WEAK_SUBJECT_RE.search(text) and _PAYMENT_COMPLAINT_RE.search(text) and not personal:
        return "store"
    operation = unsupported_operation(text)
    if operation and not personal and (transaction or product
                                       or operation in ("change account or delivery details", "change your account")):
        return "store"
    return "personal" if personal else "unclear"


ACCOUNT_CHANGE = "change account or delivery details"

# The categories the clarifying question lists (order, product, delivery, payment, refund, return). A reply that names one of
# them ANSWERS that question, even without a complaint word ("for refund", "yes for refund").
_TOPIC_ANSWER_RE = re.compile(
    r"\b(?:orders?|ordr|oder|products?|prodcut|items?|deliver\w*|delivry|delivary|payments?|pament|refunds?|refnd"
    r"|returns?|parcels?|packages?|courier|shipment|paisa|paise|mangwaya|kharida|saman)\b"
    r"|ऑर्डर|प्रोडक्ट|डिलीवरी|भुगतान|पेमेंट|रिफंड|रिफ़ंड|रिटर्न|पार्सल|पैसे|सामान", re.I)


def answers_ticket_question(text: str) -> bool:
    """True if the reply names one of the categories the ticket question listed."""
    return bool(_TOPIC_ANSWER_RE.search(unicodedata.normalize("NFC", text or "")))


def has_store_signal(text: str) -> bool:
    """True if the message names a store category or states a complaint. Abuse, "ok", "yes" and chatter do not."""
    text = unicodedata.normalize("NFC", text or "")
    return bool(_TOPIC_ANSWER_RE.search(text) or _COMPLAINT_RE.search(text) or mentions_order_or_item(text))


def describes_enough(text: str) -> bool:
    """True if a reply to "write the details" is more than a word: three words, or a number or an ID."""
    return len(_word_tokens(text)) >= 3 or any(ch.isdigit() for ch in text or "")


def is_question(text: str) -> bool:
    """True for a question (it is answered, never taken as the description of a problem)."""
    return bool(re.search(r"[?]\s*$", text or "") or _QUESTION_START.match(text or "") or _QUESTIONISH.match(text or ""))


def ticket_scope_answered(latest: str, combined: str, *, names_product: bool = False, service: bool = False) -> str:
    """``ticket_scope`` for a ticket text whose LAST message answers a question the assistant asked.

    ``service``: the customer was asked to write the details of a store service request (the first message already said
    what they want): their details count unless the details are themselves personal.
    Otherwise a reply that names a listed category settles an ``unclear`` text as ``store`` (and a ``personal`` one only when
    a transaction word is present); a reply that names none changes nothing.
    """
    if service:
        return "personal" if ticket_scope(latest) == "personal" else "store"
    base = ticket_scope(combined, names_product=names_product)
    if base == "store" or not answers_ticket_question(latest):
        return base
    transaction = bool(mentions_order_or_item(combined) or _TRANSACTION_RE.search(combined))
    if base == "unclear" or (base == "personal" and transaction):
        return "store"
    return base


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


def names_product(text: str, product_name: str) -> bool:
    """The customer's own words name this product (any distinctive word of its catalogue name, plural or singular)."""
    message = _singular_forms(_word_tokens(_ID_TOKEN.sub(" ", text or "")))
    words = {w for w in _singular_forms(_word_tokens(product_name or "")) if len(w) > 2 and w not in _RESOLVE_IGNORED}
    return bool(words & message)


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


_RETURN_IMPERATIVE = re.compile(r"(?:^|\b(?:please|pls|plz)\s+)return\s+(?:it|this|that|them|my)\b")
_RETURN_HINGLISH = re.compile(r"\breturn\s+(?:karna|karni|karne|krna|krni|karwana|karwani|karo)\b")
_RETURN_HINDI = re.compile("रिटर्न\\s*(?:करना|करनी)")
_QUESTION_OPENER = re.compile(
    r"^\s*(?:can|could|may|might|is|are|do|does|did|will|would|should|how|what|when|why|where|which|who)\b")


def wants_to_return(text: str) -> bool:
    """True if the customer's own message explicitly asks to return something ("I want to return it", "please return this",
    "return karna hai"). A question about whether they can, a policy question and a negated sentence are not requests. Only
    the customer's words count, never the model's reply. It decides nothing by itself: the engine uses it to settle a return
    request the model answered in prose, through the same checks as ``start_return_request``."""
    lower = (text or "").lower()
    if not lower.strip() or lower.strip().endswith("?") or _QUESTION_OPENER.match(lower):
        return False
    if _starts_new_return(lower):
        return True
    for pattern in (_RETURN_IMPERATIVE, _RETURN_HINGLISH, _RETURN_HINDI):
        for match in pattern.finditer(lower):
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


# ---------------------------------------------------------------------------
# Phase 0 (context retention): do not make the customer repeat what they already said
# ---------------------------------------------------------------------------

_REPEAT_COMPLAINT = re.compile(
    r"\balready\s+(?:told|gave|given|said|shared|provided|mentioned|sent|explained|wrote)\b"
    r"|\b(?:told|gave|sent)\s+you\s+(?:my|the|this|that|already)\b"
    r"|\bwhy\s+(?:do|should|must|would)\s+i\s+(?:have\s+to\s+|need\s+to\s+)?(?:repeat|say|tell|give|explain|send)\b"
    r"|\brepeat(?:ing)?\s+(?:myself|everything|it|this|that)\b"
    r"|\b(?:again\s+and\s+again|over\s+and\s+over)\b"
    r"|\bpehle\s+(?:hi\s+)?(?:bata|bol|de)\b|\bmaine\s+(?:pehle|already)\b",
    re.I)
_ASKS_FOR_ORDER_ID = re.compile(
    r"\b(?:provide|share|give|send|tell|confirm|enter|resend|repeat|type|check)\b[^.?!\n]{0,60}"
    r"\border\s*(?:id|number|no\.?|#)"
    r"|\border\s*(?:id|number)\b[^.?!\n]{0,40}\b(?:again|please)\b"
    r"|\bwhat(?:'s|\s+is)\s+(?:your|the)\s+order\s*(?:id|number)\b", re.I)

# The customer also asks for their own orders in the same message (so a "not found" for someone else's order must not
# stop the listing they asked for).
_OWN_ORDERS = re.compile(
    r"\b(?:my|our)\s+(?:recent\s+|other\s+|own\s+)?orders\b|\b(?:meri|mere|apne)\s+orders?\b|"
    r"\ball\s+(?:the\s+)?orders\b", re.I)


def asks_for_own_orders(text: str) -> bool:
    return isinstance(text, str) and bool(_OWN_ORDERS.search(text))


_ORDER_ID_FORMS = re.compile(r"\bORD(?:(\d+)|[-_ ](\d{3,}))\b", re.I)


def order_ids_in(text: str) -> list[str]:
    """Order IDs written in the text, normalised to ``ORD<digits>``, each once, in order. The data's own format is
    ``ORD`` plus four digits (ORD1001 to ORD1015); ``ORD-1010``, ``ord 1010`` and ``ORD_1010`` are read as ORD1010."""
    found: list[str] = []
    for match in _ORDER_ID_FORMS.finditer(text or ""):
        order_id = "ORD" + (match.group(1) or match.group(2))
        if order_id not in found:
            found.append(order_id)
    return found


# A duration in a reply (English or Hinglish unit) and in a tool result (English unit). Only the NUMBER is compared, so a
# Hinglish "3-7 business din" is supported by the policy text "3-7 business days" and an invented "24 hours" is not.
_NUMBER_WORDS = {"a": "1", "an": "1", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                 "seven": "7", "eight": "8", "nine": "9", "ten": "10", "twelve": "12"}
_WORD = "|".join(_NUMBER_WORDS)
_REPLY_DURATION = re.compile(
    rf"((?:\d+|\b(?:{_WORD})\b)(?:\s*(?:[-\u2013]|to|or)\s*(?:\d+|\b(?:{_WORD})\b))?)\s*"
    r"(?:business\s+|working\s+)?"
    r"(?:(?:days?|hours?|weeks?|months?|din|ghante|ghanta|hafte|hafta|mahine)\b|"
    r"\u0926\u093f\u0928|\u0918\u0902\u091f\u0947|\u0918\u0902\u091f\u093e|\u0938\u092a\u094d\u0924\u093e\u0939|"
    r"\u0939\u092b\u094d\u0924\u0947|\u092e\u0939\u0940\u0928\u0947)", re.I)
_SOURCE_DURATION = re.compile(
    r"(\d+(?:\s*[-\u2013]\s*\d+)?)[\s-]*(?:business\s+|working\s+)?(?:days?|hours?|weeks?|months?)\b", re.I)


def _duration_key(number: str) -> str:
    """``3 - 7``, ``3\u20137`` and ``three to seven`` all become ``3-7``; ``a``/``one`` become ``1``."""
    words = re.sub(r"\s*(?:[-\u2013]|to|or)\s*", "-", number.strip().lower())
    return "-".join(_NUMBER_WORDS.get(part, part) for part in words.split("-"))


def duration_keys(texts: list[str]) -> set[str]:
    """The durations (numbers only) stated in tool results or policy text. Only verified sources go in here: never a
    customer's message, and never the assistant's own earlier prose."""
    return {_duration_key(m.group(1)) for t in texts if isinstance(t, str) for m in _SOURCE_DURATION.finditer(t)}


def strip_unsupported_durations(text: str, supported: set[str]) -> str:
    """Drop the sentences of a reply that state a duration the policy or a tool result did not give. A timeline is only
    ever stated when verified information supports it; what a customer says cannot establish one. ``supported`` comes from
    ``duration_keys``. Returns the text unchanged when every duration is supported; an empty string when nothing
    supported is left."""
    parts = re.split(r"(?<=[.!?\u0964])(\s+)", text or "")
    kept: list[str] = []
    dropped = False
    for index in range(0, len(parts), 2):
        sentence = parts[index]
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        if all(_duration_key(m.group(1)) in supported for m in _REPLY_DURATION.finditer(sentence)):
            kept.append(sentence + separator)
        else:
            dropped = True
    return "".join(kept).strip() if dropped else text


# What a reply may say about a refund. The refund_status in the data is one of Not Applicable, Pending, Processed, Failed.
# Pending: recorded, not paid, no promise of an outcome or a date. Processed: the store has processed it; when it shows in
# the customer's account depends on the bank, so "received" or "completed" is not known. Failed: it did not go through.
# (Completed is a status of a RETURN REQUEST in this system, never of a refund.)
_REFUND_WORDS = re.compile(r"refund|money|reimburs|paisa|paise|amount|\u20b9|\brs\b|rupee|\u0930\u093f\u092b\u0902\u0921|\u092a\u0948\u0938\u0947|"
                           r"\u092a\u0948\u0938\u093e", re.I)
_REFUND_FUTURE = re.compile(
    r"(?:\b(?:will|'ll|going to|gonna|should|expect(?:ed)? to|set to|due to)\s+(?:be\s+)?"
    r"(?:receiv\w*|get(?:ting)?|credit\w*|refund\w*|paid|sent|reflect\w*|arriv\w*|deposit\w*|returned)\b"
    r"|\bmilne\s+wal[ai]\b|\bmil\s*(?:jaa?y?eg[ai]|jayega|jayegi)\b|\bmileg[ai]\b|\bwapas\s+mil(?:ega|egi|ne)\b"
    r"|\bcredit\s+ho\s+(?:jaa?y?eg[ai]|jayega|jayegi|jaega)\b|\baaye?g[ai]\b"
    r"|\u0915\u094d\u0930\u0947\u0921\u093f\u091f\s+\u0939\u094b\s+\u091c\u093e\u090f\u0917\u093e"
    r"|\u092e\u093f\u0932(?:\u0947\u0917\u093e|\u0947\u0917\u0940|\u0928\u0947 \u0935\u093e\u0932\u093e|"
    r"\u0928\u0947 \u0935\u093e\u0932\u0940|\u091c\u093e\u090f\u0917\u093e|\u091c\u093e\u090f\u0917\u0940)|"
    r"\u0906\u090f\u0917\u093e)", re.I)
_REFUND_DONE = re.compile(
    r"(?:\b(?:has|have|had|was|were|is|are)\s+(?:been\s+)?(?:credited|refunded|completed|received|deposited|"
    r"transferred|reflected)\b|\balready\s+(?:refunded|credited|paid|received)\b|\bin your (?:bank )?account\b"
    r"|\brefund\s+(?:is\s+)?complete[d]?\b|\bcredit\s+ho\s+gaya\b|\bmil\s+gaya\b|\bwapas\s+mil\s+gaya\b|\baa\s+gaya\b"
    r"|\u092e\u093f\u0932 \u0917\u092f\u093e|\u0915\u094d\u0930\u0947\u0921\u093f\u091f \u0939\u094b \u0917\u092f\u093e|"
    r"\u092a\u0942\u0930\u093e \u0939\u094b \u0917\u092f\u093e)", re.I)


def reword_refund_claims(text: str, replacement: Optional[str], *, future: bool) -> str:
    """Replace each sentence that talks about a refund and promises it (``future``) or says it is done or received. The
    first such sentence becomes ``replacement`` (the status said plainly, by code); the others are dropped. Unchanged when
    no sentence does either."""
    parts = re.split(r"(?<=[.!?\u0964])(\s+)", text or "")
    out: list[str] = []
    changed = used = False
    for index in range(0, len(parts), 2):
        sentence = parts[index]
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        hit = _REFUND_WORDS.search(sentence) and ((future and _REFUND_FUTURE.search(sentence))
                                                  or _REFUND_DONE.search(sentence))
        if hit:
            changed = True
            if replacement and not used:
                out.append(replacement + separator)
                used = True
            continue
        out.append(sentence + separator)
    return "".join(out).strip() if changed else text


REPEAT_REMINDER = (
    "Reminder from the system: the customer says they already gave this information, so do not ask them to "
    "repeat it. Look for it earlier in this conversation. If an order ID appears there, call get_order_status "
    "for it. Otherwise call list_my_orders now and work from the customer's own orders. Acknowledge the "
    "repetition in one short sentence.")


def is_repeat_complaint(text: str) -> bool:
    """The customer's message complains that they are being asked to repeat themselves."""
    return isinstance(text, str) and bool(_REPEAT_COMPLAINT.search(text))


def asks_for_order_id(reply: str) -> bool:
    """The model's reply asks the customer to supply an order ID or number."""
    return isinstance(reply, str) and bool(_ASKS_FOR_ORDER_ID.search(reply))


# ---------------------------------------------------------------------------
# The customer's language, decided by code (no model call)
# ---------------------------------------------------------------------------

LANGUAGES = ("en", "hinglish", "hi")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_LATIN_WORDS = re.compile(r"[a-z]+")
_LATIN_WORDS_2 = re.compile(r"[a-z]{2,}")
# The same detector the evaluation harness uses to judge reply language (tests keep the two in step).
_HINGLISH_WORDS = frozenset("""mera meri mere mujhe mujhko hai hain nahi nahin kya kab kaha kahan kaise kyun kyu bhai
yaar aap aapka aapki aapke aapko tum tumhara tumhari tumhe mein hoon karna karo karu kare chahiye wapas abhi tak aaya
aayega aata aati batao bata dijiye kijiye sakta sakte sakti raha rahi rahe gaya gayi hua hui kitne kitna din ye yeh
woh aur lekin agar toh matlab kuch koi kisi insaan baat karao ho chuka bilkul bekaar bakwas theek madad tha thi ka ki ke ko kaisa kaisi aaj kal""".split())
# Words that, two or more in one message and with at most one Hinglish word, make it clearly English.
_ENGLISH_FUNCTION_WORDS = frozenset("""the is are was were can could would should do does did have has i you your my we
it this that what where when how why which who please from with for about still not and any there will been be an
want need get give send take make tell show check know like just also now
sorry meant mean actually thanks thank""".split())


def detect_language(text: str) -> str:
    """``hi`` (three or more Devanagari letters), ``hinglish`` (two or more Hinglish words) or ``en``."""
    if len(_DEVANAGARI.findall(text or "")) >= 3:
        return "hi"
    words = set(_LATIN_WORDS.findall((text or "").lower()))
    return "hinglish" if len(words & _HINGLISH_WORDS) >= 2 else "en"


_HINDI_COURTESY = frozenset({"namaste", "namaskar", "shukriya", "dhanyavaad", "dhanyawad", "dhanyavad"})
_COURTESY_FILLER = frozenset({"ji", "bhai", "aapka", "bahut", "bohot", "hai", "aur"})


def decisive_language(text: str) -> Optional[str]:
    """The language the message itself settles, or None when it is ambiguous ("ok", "ji haan", "mera order", an ID alone).

    Devanagari or two Hinglish words mean Hindi or Hinglish. A message that is clearly English (two or more common
    English function words and at most one Hinglish word, or four or more words and none of them Hinglish) is English.
    """
    found = detect_language(text)
    if found != "en":
        return found
    lowered = (text or "").lower()
    words = set(_LATIN_WORDS.findall(lowered))
    hinglish_hits = len(words & _HINGLISH_WORDS)
    if len(words & _ENGLISH_FUNCTION_WORDS) >= 2 and hinglish_hits <= 1:
        return "en"
    if hinglish_hits == 0 and len(_LATIN_WORDS_2.findall(lowered)) >= 4:  # four or more words, none of them Hinglish
        return "en"
    if words & _HINDI_COURTESY and words <= _HINDI_COURTESY | _COURTESY_FILLER:
        return "hinglish"  # "namaste", "shukriya ji": a lone Hindi courtesy word settles the language
    return None


def customer_language(text: str, previous: str = "en") -> str:
    """The language to answer in: the latest message decides whenever it can, whatever the session used before. Only a
    genuinely ambiguous message keeps the previous language."""
    decided = decisive_language(text)
    if decided is not None:
        return decided
    return previous if previous in LANGUAGES else "en"


_DAMAGE = re.compile(
    r"\b(?:damag\w*|broken|defect\w*|faulty|cracked|dead|not working|stopped working|doesn'?t work|isn'?t working"
    r"|wrong (?:item|product|one)|kharab|kharaab|toota|tuta|tooti|galat (?:item|product|saman|samaan))\b"
    r"|\u0916\u0930\u093e\u092c|\u091f\u0942\u091f|\u0917\u0932\u0924 (?:\u0938\u093e\u092e\u093e\u0928|\u092a\u094d\u0930\u094b\u0921\u0915\u094d\u091f)", re.I)


def reports_damage(text: str) -> bool:
    """The customer says the item arrived damaged, faulty or wrong (English, Hinglish or Hindi cue words)."""
    return bool(_DAMAGE.search(text or ""))
