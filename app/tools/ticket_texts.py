"""The fixed, code-owned sentences of the support-ticket flow (and the generic cancel line), in English, Hinglish and Hindi.

Why this exists. The ticket flow is driven by code, not by the model, so its sentences used to be English whatever
language the customer wrote (live evaluation: a Hindi "kisi insaan se baat karao" got an English "Happy to raise a
ticket..."). A conversation must stay in one language, so these sentences are written once per language here and chosen
by code from the session's language (``guard.customer_language``). No language model decides the language or translates.

Conventions: the English text is exactly what the engine said before this module existed, so English behaviour is
unchanged. Identifiers (order, item and ticket IDs, the issue type, the word ``cancel`` the customer types) are never
translated. The Hinglish and Hindi sentences are a DRAFT written by the assistant and have not been reviewed by a fluent
speaker; ``REVIEW_STATUS`` says so and a test pins it, so it cannot be forgotten.
"""

from __future__ import annotations

LANGUAGES = ("en", "hinglish", "hi")
REVIEW_STATUS = {"en": "approved", "hinglish": "draft: needs review by a fluent speaker",
                 "hi": "draft: needs review by a fluent speaker"}

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "ticket_limit": "You've reached the limit of support tickets for this session, so I can't create another one.",
        "ask_issue": ("Happy to raise a ticket. What is the issue you'd like it to be about? Please include the order "
                      "ID if there is one."),
        "order_system_down": "I can't reach the order system right now. Please try again shortly.",
        "order_not_found_check": ("I couldn't find that order on your account, so I haven't prepared a ticket. Could "
                                  "you check the order ID?"),
        "order_not_found": "I couldn't find that order on your account, so I haven't prepared a ticket.",
        "lead_limit": ("I'm not able to {limit} myself. Here's the ticket I can create so a human agent can follow up. "
                       "Please confirm to submit it."),
        "lead_plain": "Here's the ticket I can create. Please confirm to submit it.",
        "declined": "Okay, I won't raise a ticket. Nothing was changed.",
        "unclear": ("I'm not sure whether you'd like me to help you raise a support ticket. Please reply yes or no, or "
                    "type cancel to leave it."),
        "describe": ("Please describe the problem with your {item} (item {item_id}, order {order_id}) in your own "
                     "words. Nothing is saved until you confirm."),
        "problem_meaningless": "I need a few words about what is wrong. ",
        "problem_too_long": ("That message is {n} characters long and a ticket description can be at most {max}. "
                             "Please send a shorter description. "),
        "problem_other_target": ("That mentions another order or item, so I haven't saved it. This ticket is about "
                                 "item {item_id} on order {order_id}. "),
        "problem_new_return": ("That looks like a different return request rather than a description of the problem, "
                               "so I haven't saved it. If you want to start that return, type cancel first. "),
        "problem_unrelated": "I'm still waiting for a description of the problem, so I haven't saved that message. ",
        "problem_default": "I couldn't use that text. ",
        "again": ("Please describe the problem with the item in your own words, or type cancel to stop. Nothing is "
                  "saved until you confirm."),
        "pending_plain": ("Create a support ticket ({issue_type}) with this description: \"{preview}\". A human agent "
                          "will follow up."),
        "pending_order": ("Create a support ticket ({issue_type}, order {order_id}) with this description: "
                          "\"{preview}\". A human agent will follow up."),
        "pending_offered": ("Create a support ticket (return, order {order_id}, item {item_id}) with this description: "
                            "\"{preview}\". A human agent will follow up. I can't promise any particular outcome."),
        "created": "Your support ticket {ticket_id} has been created. A human agent will follow up.",
        "cancelled": "Okay, I've cancelled that. Nothing was changed.",
        "issue_offer": "If you'd like, I can raise a support ticket for this issue.",
    },
    "hinglish": {
        "ticket_limit": "Is session mein support tickets ki limit poori ho gayi hai, isliye main aur ticket nahi bana sakta.",
        "ask_issue": ("Zaroor, main ticket bana deta hoon. Aap kis baare mein ticket chahte hain? Agar order ID hai "
                      "toh woh bhi bata dijiye."),
        "order_system_down": "Abhi main order system tak nahi pahunch pa raha hoon. Kripya thodi der baad dobara koshish karein.",
        "order_not_found_check": ("Mujhe woh order aapke account mein nahi mila, isliye maine ticket taiyaar nahi kiya. "
                                  "Kya aap order ID check kar sakte hain?"),
        "order_not_found": "Mujhe woh order aapke account mein nahi mila, isliye maine ticket taiyaar nahi kiya.",
        "lead_limit": ("Main yeh khud nahi kar sakta. Yeh ticket main bana sakta hoon taaki koi human agent follow up "
                       "kare. Submit karne ke liye confirm karein."),
        "lead_plain": "Yeh ticket main bana sakta hoon. Submit karne ke liye confirm karein.",
        "declined": "Theek hai, main ticket nahi banaunga. Kuch bhi change nahi hua.",
        "unclear": ("Mujhe pata nahi aap ticket banwana chahte hain ya nahi. Kripya yes ya no likhein, ya chhodne ke "
                    "liye cancel likhein."),
        "describe": ("Kripya apne {item} (item {item_id}, order {order_id}) ki problem apne shabdon mein bataiye. Jab "
                     "tak aap confirm nahi karte, kuch save nahi hota."),
        "problem_meaningless": "Mujhe thode shabdon mein batayein ki kya problem hai. ",
        "problem_too_long": ("Yeh message {n} characters ka hai aur ticket description zyada se zyada {max} characters "
                             "ka ho sakta hai. Kripya chhota description bhejein. "),
        "problem_other_target": ("Isme doosre order ya item ka zikr hai, isliye maine ise save nahi kiya. Yeh ticket "
                                 "order {order_id} ke item {item_id} ke baare mein hai. "),
        "problem_new_return": ("Yeh problem ka description nahi, balki alag return request lag raha hai, isliye maine "
                               "ise save nahi kiya. Agar aap woh return shuru karna chahte hain, toh pehle cancel "
                               "likhein. "),
        "problem_unrelated": ("Main abhi bhi problem ke description ka intezaar kar raha hoon, isliye maine woh "
                              "message save nahi kiya. "),
        "problem_default": "Main woh text use nahi kar paaya. ",
        "again": ("Kripya item ki problem apne shabdon mein bataiye, ya rokne ke liye cancel likhein. Jab tak aap "
                  "confirm nahi karte, kuch save nahi hota."),
        "pending_plain": ("Ek support ticket banayein ({issue_type}) is description ke saath: \"{preview}\". Ek human "
                          "agent follow up karega."),
        "pending_order": ("Ek support ticket banayein ({issue_type}, order {order_id}) is description ke saath: "
                          "\"{preview}\". Ek human agent follow up karega."),
        "pending_offered": ("Ek support ticket banayein (return, order {order_id}, item {item_id}) is description ke "
                            "saath: \"{preview}\". Ek human agent follow up karega. Main kisi bhi nateeje ka vaada "
                            "nahi kar sakta."),
        "created": "Aapka support ticket {ticket_id} ban gaya hai. Ek human agent follow up karega.",
        "cancelled": "Theek hai, maine ise cancel kar diya. Kuch bhi change nahi hua.",
        "issue_offer": "Agar aap chahein, toh main is problem ke liye support ticket bana sakta hoon.",
    },
    "hi": {
        "ticket_limit": "इस सेशन में सपोर्ट टिकट की सीमा पूरी हो चुकी है, इसलिए मैं और टिकट नहीं बना सकता।",
        "ask_issue": "ज़रूर, मैं टिकट बना देता हूँ। आप किस बारे में टिकट चाहते हैं? अगर ऑर्डर आईडी है तो वह भी बताइए।",
        "order_system_down": "अभी मैं ऑर्डर सिस्टम तक नहीं पहुँच पा रहा हूँ। कृपया थोड़ी देर बाद फिर कोशिश करें।",
        "order_not_found_check": ("मुझे वह ऑर्डर आपके खाते में नहीं मिला, इसलिए मैंने टिकट तैयार नहीं किया। क्या आप "
                                  "ऑर्डर आईडी जाँच सकते हैं?"),
        "order_not_found": "मुझे वह ऑर्डर आपके खाते में नहीं मिला, इसलिए मैंने टिकट तैयार नहीं किया।",
        "lead_limit": ("मैं यह खुद नहीं कर सकता। यह टिकट मैं बना सकता हूँ ताकि कोई मानव एजेंट आपसे संपर्क करे। जमा "
                       "करने के लिए कृपया पुष्टि करें।"),
        "lead_plain": "यह टिकट मैं बना सकता हूँ। जमा करने के लिए कृपया पुष्टि करें।",
        "declined": "ठीक है, मैं टिकट नहीं बनाऊँगा। कुछ भी नहीं बदला गया।",
        "unclear": ("मुझे पता नहीं कि आप टिकट बनवाना चाहते हैं या नहीं। कृपया हाँ या नहीं लिखें, या छोड़ने के लिए "
                    "cancel लिखें।"),
        "describe": ("कृपया अपने {item} (आइटम {item_id}, ऑर्डर {order_id}) की समस्या अपने शब्दों में बताइए। आपके "
                     "पुष्टि करने तक कुछ भी सहेजा नहीं जाएगा।"),
        "problem_meaningless": "कृपया कुछ शब्दों में बताइए कि समस्या क्या है। ",
        "problem_too_long": ("यह संदेश {n} अक्षरों का है और टिकट विवरण अधिकतम {max} अक्षरों का हो सकता है। कृपया छोटा "
                             "विवरण भेजें। "),
        "problem_other_target": ("इसमें किसी दूसरे ऑर्डर या आइटम का ज़िक्र है, इसलिए मैंने इसे सहेजा नहीं। यह टिकट "
                                 "ऑर्डर {order_id} के आइटम {item_id} के बारे में है। "),
        "problem_new_return": ("यह समस्या का विवरण नहीं, बल्कि अलग रिटर्न अनुरोध लगता है, इसलिए मैंने इसे सहेजा नहीं। "
                               "अगर आप वह रिटर्न शुरू करना चाहते हैं तो पहले cancel लिखें। "),
        "problem_unrelated": "मैं अभी भी समस्या के विवरण का इंतज़ार कर रहा हूँ, इसलिए मैंने वह संदेश सहेजा नहीं। ",
        "problem_default": "मैं वह पाठ इस्तेमाल नहीं कर सका। ",
        "again": ("कृपया आइटम की समस्या अपने शब्दों में बताइए, या रोकने के लिए cancel लिखें। आपके पुष्टि करने तक कुछ "
                  "भी सहेजा नहीं जाएगा।"),
        "pending_plain": ("इस विवरण के साथ एक सपोर्ट टिकट बनाएँ ({issue_type}): \"{preview}\"। एक मानव एजेंट आपसे "
                          "संपर्क करेगा।"),
        "pending_order": ("इस विवरण के साथ एक सपोर्ट टिकट बनाएँ ({issue_type}, ऑर्डर {order_id}): \"{preview}\"। "
                          "एक मानव एजेंट आपसे संपर्क करेगा।"),
        "pending_offered": ("इस विवरण के साथ एक सपोर्ट टिकट बनाएँ (return, ऑर्डर {order_id}, आइटम {item_id}): "
                            "\"{preview}\"। एक मानव एजेंट आपसे संपर्क करेगा। मैं किसी भी नतीजे का वादा नहीं कर सकता।"),
        "created": "आपका सपोर्ट टिकट {ticket_id} बन गया है। एक मानव एजेंट आपसे संपर्क करेगा।",
        "cancelled": "ठीक है, मैंने इसे रद्द कर दिया। कुछ भी नहीं बदला गया।",
        "issue_offer": "अगर आप चाहें तो मैं इस समस्या के लिए सपोर्ट टिकट बना सकता हूँ।",
    },
}


def text(key: str, language: str = "en", **values: object) -> str:
    """The sentence ``key`` in ``language`` (English when the language is unknown), with ``values`` filled in."""
    table = TEXTS.get(language) or TEXTS["en"]
    return (table.get(key) or TEXTS["en"][key]).format(**values)
