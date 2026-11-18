"""Fixed, code-owned sentences for two situations the model must not improvise, in English, Hinglish and Hindi.

* ``not_on_account``: the customer named an order ID that is not on their account. The reply says so, echoes only the ID the
  customer typed, reveals nothing else (an order that does not exist and another customer's order are indistinguishable),
  and OFFERS the customer's own orders without listing them.
* ``which_order``: the customer complains about repeating themselves but has more than one order and never said which. The
  reply lists only IDs and statuses from the customer's own orders and asks which one; it never picks one for them.

Chosen by code from the session language (``guard.customer_language``); no model call. The English text is the reference.
The Hinglish and Hindi sentences are a DRAFT written by the assistant and have not been reviewed by a fluent speaker;
``REVIEW_STATUS`` says so and a test pins it.
"""

from __future__ import annotations

LANGUAGES = ("en", "hinglish", "hi")
REVIEW_STATUS = {"en": "approved", "hinglish": "draft: needs review by a fluent speaker",
                 "hi": "draft: needs review by a fluent speaker"}

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "not_on_account": "I couldn't find {ids} on your account. If you'd like, I can list your own orders.",
        "which_order": "Sorry about the repetition. Here are your orders: {orders}. Which one is this about?",
    },
    "hinglish": {
        "not_on_account": ("Mujhe {ids} aapke account mein nahi mila. Agar aap chahein, toh main aapke apne orders ki "
                           "list dikha sakta hoon."),
        "which_order": ("Dobara poochne ke liye maaf kijiye. Yeh aapke orders hain: {orders}. Aap kis order ke baare "
                        "mein baat kar rahe hain?"),
    },
    "hi": {
        "not_on_account": ("मुझे {ids} आपके खाते में नहीं मिला। अगर आप चाहें तो मैं आपके अपने ऑर्डर की सूची दिखा "
                           "सकता हूँ।"),
        "which_order": ("दोबारा पूछने के लिए क्षमा करें। ये आपके ऑर्डर हैं: {orders}। आप किस ऑर्डर के बारे में बात "
                        "कर रहे हैं?"),
    },
}


def text(key: str, language: str = "en", **values: object) -> str:
    table = TEXTS.get(language) or TEXTS["en"]
    return (table.get(key) or TEXTS["en"][key]).format(**values)
