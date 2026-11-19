"""The customer-facing sentences of a return-timing answer, in English, Hinglish and Hindi.

Why this exists. A small model asked to put English tool text "in the customer's language" sometimes answers an
English question in Hinglish and often drops sentences it should keep (the live ablation of 2026-11-16: six
repetitions per variant, 6 of 12 current-prompt replies drifted, 0 of 59 under any single wording change). So the
wording that matters is written once, here, in each language, chosen by code from the customer's own message
(``guard.customer_language``), and either handed to the model ready to copy or appended when the reply left it out.
No language model decides the language and none translates.

Conventions: times are the English UTC text the tool builds ("11 November 2026, 00:00 UTC") in every language, so
no time zone is ever converted and the same moment reads the same everywhere. The Hinglish and Hindi sentences are a
DRAFT written by the assistant and have not yet been reviewed by a fluent speaker; ``REVIEW_STATUS`` says so and a
test pins it, so it cannot be forgotten.
"""

from __future__ import annotations

LANGUAGES = ("en", "hinglish", "hi")
REVIEW_STATUS = {"en": "approved", "hinglish": "draft: needs review by a fluent speaker",
                 "hi": "draft: needs review by a fluent speaker"}

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "ordinary_open": "The ordinary return period (7 days from delivery) is open until {when} ({left} left).",
        "ordinary_closed": "The ordinary return period (7 days from delivery) closed on {when}.",
        "deadline_open": ("The deadline to report a damaged or wrong item and start a return (48 hours from delivery) "
                          "is open until {when} ({left} left)."),
        "deadline_closed": ("The deadline to report a damaged or wrong item and start a return (48 hours from "
                            "delivery) closed on {when}."),
        "limits": ("I can only check the delivery timing: I cannot check whether this product can be returned at "
                   "all, the item's condition, or its packaging and accessories."),
        "photos": ("For a damaged or wrong item the FAQ asks for clear photos of the issue. I cannot collect photos "
                   "here, but I can raise a support ticket."),
        "closed_support": ("That deadline has passed. I can raise a support ticket so a person can review what "
                           "happened; a ticket is a request for review, and I can't promise any particular outcome."),
        "support_offer": ("If you'd like, I can raise a support ticket so a person can review it; a ticket is a "
                          "request for review, and I can't promise any particular outcome."),
        "less_than_hour": "less than 1 hour", "one_hour": "about 1 hour", "hours": "about {n} hours",
        "days": "about {n} days",
    },
    "hinglish": {
        "ordinary_open": "Ordinary return period (delivery se 7 din) {when} tak khula hai ({left} baaki).",
        "ordinary_closed": "Ordinary return period (delivery se 7 din) {when} ko khatam ho gaya.",
        "deadline_open": ("Damaged ya galat item report karke return shuru karne ki deadline (delivery se 48 ghante) "
                          "{when} tak khuli hai ({left} baaki)."),
        "deadline_closed": ("Damaged ya galat item report karke return shuru karne ki deadline (delivery se 48 ghante) "
                            "{when} ko khatam ho gayi."),
        "limits": ("Main sirf delivery ka timing check kar sakta hoon. Yeh product return ho sakta hai ya nahi, item "
                   "ki condition, aur original packaging aur accessories main check nahi kar sakta."),
        "photos": ("Damaged ya galat item ke liye FAQ mein issue ki saaf photos maangi gayi hain. Main yahan photos "
                   "nahi le sakta, lekin support ticket bana sakta hoon."),
        "closed_support": ("Yeh deadline nikal chuki hai. Main support ticket bana sakta hoon taaki koi insaan aapki "
                           "baat dekh sake; ticket review ki request hai, aur main kisi nateeje ka vaada nahi kar "
                           "sakta."),
        "support_offer": ("Chahein toh main support ticket bana sakta hoon taaki koi insaan ise dekh sake; ticket "
                          "review ki request hai, aur main kisi nateeje ka vaada nahi kar sakta."),
        "less_than_hour": "1 ghante se kam", "one_hour": "lagbhag 1 ghanta", "hours": "lagbhag {n} ghante",
        "days": "lagbhag {n} din",
    },
    "hi": {
        "ordinary_open": "सामान्य रिटर्न अवधि (डिलीवरी से 7 दिन) {when} तक खुली है ({left} बाकी)।",
        "ordinary_closed": "सामान्य रिटर्न अवधि (डिलीवरी से 7 दिन) {when} को समाप्त हो गई।",
        "deadline_open": ("खराब या गलत सामान की रिपोर्ट करके रिटर्न शुरू करने की समय-सीमा (डिलीवरी से 48 घंटे) "
                          "{when} तक खुली है ({left} बाकी)।"),
        "deadline_closed": ("खराब या गलत सामान की रिपोर्ट करके रिटर्न शुरू करने की समय-सीमा (डिलीवरी से 48 घंटे) "
                            "{when} को समाप्त हो गई।"),
        "limits": ("मैं केवल डिलीवरी का समय जाँच सकता हूँ। यह उत्पाद रिटर्न हो सकता है या नहीं, सामान की स्थिति, "
                   "और मूल पैकेजिंग व एक्सेसरीज़ मैं नहीं जाँच सकता।"),
        "photos": ("खराब या गलत सामान के लिए FAQ में समस्या की स्पष्ट फ़ोटो माँगी गई हैं। मैं यहाँ फ़ोटो नहीं ले सकता, "
                   "लेकिन सपोर्ट टिकट बना सकता हूँ।"),
        "closed_support": ("यह समय-सीमा निकल चुकी है। मैं सपोर्ट टिकट बना सकता हूँ ताकि कोई व्यक्ति आपकी स्थिति देख सके; "
                           "टिकट समीक्षा का अनुरोध है, और मैं किसी परिणाम का वादा नहीं कर सकता।"),
        "support_offer": ("चाहें तो मैं सपोर्ट टिकट बना सकता हूँ ताकि कोई व्यक्ति इसे देख सके; टिकट समीक्षा का अनुरोध है, "
                          "और मैं किसी परिणाम का वादा नहीं कर सकता।"),
        "less_than_hour": "1 घंटे से कम", "one_hour": "लगभग 1 घंटा", "hours": "लगभग {n} घंटे",
        "days": "लगभग {n} दिन",
    },
}

# A reply that already carries one of these says the sentence in its own words; nothing is appended then.
LIMITS_MARKERS = ("condition", "packag", "accessor", "स्थिति", "पैकेजिंग", "एक्सेसरी")
PHOTO_MARKERS = ("photo", "फोटो", "फ़ोटो", "तस्वीर")
TICKET_MARKERS = ("ticket", "टिकट")


def _lang(language: str) -> dict[str, str]:
    return TEXTS[language if language in TEXTS else "en"]


def time_left(language: str, seconds: float) -> str:
    """Rounded-down time left in an open period, as words (never a bare number: a bare 0 reads as expired)."""
    t = _lang(language)
    hours = int(seconds // 3600)
    if hours < 1:
        return t["less_than_hour"]
    if hours == 1:
        return t["one_hour"]
    if hours < 48:
        return t["hours"].format(n=hours)
    return t["days"].format(n=hours // 24)


def period_sentence(language: str, kind: str, is_open: bool, when: str, seconds_left: float) -> str:
    """``kind`` is ``ordinary`` or ``deadline``."""
    t = _lang(language)
    if is_open:
        return t[f"{kind}_open"].format(when=when, left=time_left(language, seconds_left))
    return t[f"{kind}_closed"].format(when=when)


def limits_sentence(language: str) -> str:
    return _lang(language)["limits"]


def photos_sentence(language: str) -> str:
    return _lang(language)["photos"]


def closed_support_sentence(language: str) -> str:
    return _lang(language)["closed_support"]


def has_ticket(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in TICKET_MARKERS)


def has_limits(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in LIMITS_MARKERS)


def has_photos(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in PHOTO_MARKERS)


def paragraphs(language: str, ordinary: dict, deadline: dict) -> dict[str, str]:
    """One short paragraph for each kind of customer message, from two period descriptions.

    Each description is ``{"open": bool, "when": str, "seconds_left": float}``. The rules, by customer message:

    * no reason given: the ordinary period. The 48-hour deadline only if it is still open (a closed one is not
      the customer's concern when nothing was said about damage). The limits when something can still happen;
      a support offer when the ordinary period has closed too.
    * item damaged or wrong: the deadline only, never the ordinary period (no steering to another reason). Open: the
      photo sentence. Closed: that it has passed, without implying photos can restore it, and a support offer.
    * any other reason: the ordinary period and the limits (or the support offer when it has closed).
    """
    t = _lang(language)
    ordinary_sentence = period_sentence(language, "ordinary", ordinary["open"], ordinary["when"], ordinary["seconds_left"])
    deadline_sentence = period_sentence(language, "deadline", deadline["open"], deadline["when"], deadline["seconds_left"])
    ordinary_tail = t["limits"] if ordinary["open"] else t["support_offer"]
    no_reason = [ordinary_sentence]
    if deadline["open"]:
        no_reason.append(deadline_sentence)
    no_reason.append(ordinary_tail)
    damaged = [deadline_sentence, t["photos"] if deadline["open"] else t["closed_support"]]
    return {"no_reason_given": " ".join(no_reason),
            "item_damaged_or_wrong": " ".join(damaged),
            "any_other_reason": " ".join([ordinary_sentence, ordinary_tail])}
