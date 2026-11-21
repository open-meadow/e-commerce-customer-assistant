"""Regenerate the Hinglish and Hindi review sheet (Markdown and CSV) from the catalogue, ``app/tools/flow_texts.py``.

    uv run python scripts/localization_review.py            # writes docs/localization_review_hinglish_hindi.{md,csv}

Run it whenever a sentence in the catalogue is added or changed. Every catalogue key needs an entry in ``WHEN`` (when the
sentence is shown); a test fails if one is missing, so a new sentence cannot reach reviewers undescribed. ``HIGH`` and
``MEDIUM`` say which rows most need a fluent reader and why. The reviewer fills in the CSV's verdict, notes and suggestion
columns; nothing here reads them back.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tools import flow_texts  # noqa: E402

WHEN = {
    # ticket flow
    "ticket_rate_limited": "The customer's account already holds the configured number of new tickets in the rolling window: no ticket, says when another is possible and what they can still do (no human promise)",
    "ticket_id_unverified": "A reply mentioned a ticket reference that no verified record of this customer supports: the sentence is removed and this is said when nothing else is left",
    'ticket_cat_question': 'A ticket was proposed but no single category is clear from what the customer wrote (or the proposed category contradicts it): asks which kind of problem it is',
    'ticket_cat_order': 'Button label of the ticket category question',
    'ticket_cat_return': 'Button label of the ticket category question',
    'ticket_cat_payment': 'Button label of the ticket category question',
    'ticket_cat_product': 'Button label of the ticket category question',
    'ticket_cat_general': 'Button label of the ticket category question',
    'ticket_cat_cancel': 'Button label of the ticket category question: stop',
    'ticket_order_question': "Several of the customer's own orders are named in what they wrote: asks which one the ticket is about",
    'ticket_order_none': 'Button label of the which-order question: no order',
    'tickets_none': 'The customer asked about their tickets and has none (rendered by code from the database, not by the model)',
    'tickets_header': 'The customer asked about their tickets: count line (rendered by code from the database)',
    'tickets_line': "One ticket in the list of the customer's tickets (no order linked)",
    'tickets_line_order': "One ticket in the list of the customer's tickets (an order linked)",
    'tickets_more': 'More tickets exist than are listed: says how many are shown',
    'tickets_page_range': "A further page of the customer's tickets (rendered by code): which tickets this page shows",
    'tickets_more_hint': 'More tickets exist than were listed: how to ask for the next page (the engine recognises the phrase shown in each language)',
    'pending_paused': 'The customer typed not yet / wait / hold on while a ticket card is open: the card stays, nothing is saved',
    'pending_explain': "The customer asked a question while a ticket card is open: the card's own text is repeated, the draft is kept",
    "ask_issue": "The customer asks for a ticket or a person but has not said what it is about",
    "ticket_out_of_scope": "A ticket was proposed for a personal or unrelated request: no ticket, says what tickets are for",
    "ticket_scope_ask": "A ticket was proposed but the text does not yet say what went wrong with a store matter",
    "ticket_describe_ask": "The customer is asked to write, in their own words, what went wrong or what they need (a reply that said nothing about the store, or a yes to the ticket offer)",
    "ticket_service_ask": "The customer asked for something the assistant cannot do itself (a new address, phone or email): offers a ticket and asks them to write the details",
    "ticket_declined": "The customer said no to the ticket offer: nothing is created",
    "order_system_down": "The order database cannot be reached",
    "order_not_found_check": "The order named for a ticket is not on the customer's account",
    "order_not_found": "Same, in the ticket-after-refusal path (shorter)",
    "lead_limit": "A ticket is proposed for something the assistant cannot do itself ({limit} is English: cancel, change...)",
    "lead_plain": "A ticket card is shown for the customer to confirm",
    "declined": "The customer says no to raising a ticket",
    "unclear": "The reply to the ticket offer is unclear",
    "describe": "The customer said yes: asks them to describe the problem",
    "problem_meaningless": "Prefix when the description has no usable words",
    "problem_too_long": "Prefix when the description is over the length limit",
    "problem_other_target": "Prefix when the text names a different order or item",
    "problem_new_return": "Prefix when the text looks like a new return request",
    "problem_unrelated": "Prefix when the text is unrelated to the problem",
    "problem_default": "Prefix for any other unusable text",
    "again": "Added after the prefixes: asks for the description again",
    "pending_plain": "Confirmation card text (ticket without an order)",
    "pending_order": "Confirmation card text (ticket about an order)",
    "pending_offered": "Confirmation card text (ticket after a closed return window)",
    "created": "After the customer confirms: the ticket exists",
    "cancelled": "The customer cancels or declines a pending card, or types cancel",
    "issue_offer": "Appended after an answer about a problem the customer has: offers a ticket",
    # lookup
    "not_on_account": "The customer typed an order ID that is not on their account (the engine's own reply)",
    "which_order": "A repeat complaint with several orders and none named: asks which one",
    # generic
    "invalid_confirmation": "A confirmation request that is malformed",
    "write_unconfirmed": "The database could not finish saving a confirmed request",
    "write_failed": "Unexpected failure while saving a confirmed request",
    "choice_gone": "The customer clicks a choice that is no longer open",
    "choice_expired": "The customer clicks a choice that has expired",
    "choice_gone_short": "Any other unavailable choice",
    "refuse_unknown": "Confirming a request that does not exist",
    "refuse_expired": "Confirming a request after its 10 minutes",
    "refuse_claimed": "Confirming a request that is already being processed",
    "refuse_done": "Confirming a request twice",
    "refuse_cancelled": "Confirming a request that was cancelled",
    "refuse_cap": "The session's limit of requests is reached",
    "refuse_default": "Any other refusal of a confirmation",
    "remind_pending": "The customer types while a confirmation card is open",
    "remind_choice": "The customer types while a choice menu is open",
    "unavailable": "The language model is unreachable (every flow)",
    "too_many_calls": "One message needed more tool calls than allowed",
    "ambiguous_help": "A vague complaint with no order or product: asks what it is about",
    "grounding": "The model's reply claimed something was done: replaced by this",
    "no_answer": "The model returned nothing usable",
    "no_timeline": "A reply's only content was a timeline no policy or tool supports",
    # return journey
    "need_ids": "A return was proposed with unusable order or item IDs",
    "return_limit": "The session's limit of return requests is reached",
    "reason_notice": "Added to every reason prompt: the next message is stored exactly as typed",
    "reason_ask": "A return was asked for with no reason",
    "reason_too_long": "The reason is over 1000 characters",
    "reason_unusable": "The reason text cannot be used",
    "reason_waiting": "The customer sends something that is not a reason while a reason is awaited",
    "return_aborted": "The customer changes their mind while giving a reason",
    "category_question": "The category menu's question",
    "cat_damaged": "Menu button: the item arrived damaged",
    "cat_wrong_product": "Menu button: the wrong product arrived",
    "cat_standard": "Menu button: any other reason",
    "cat_cancel": "Menu button: never mind",
    "catname_damaged": "Category name used inside other sentences",
    "catname_wrong_product": "Category name used inside other sentences",
    "catname_standard": "Category name used inside other sentences",
    "category_cancel": "The customer picks 'never mind' in the menu",
    "return_card_lead": "The return confirmation card is shown",
    "return_card_summary": "The text on the return confirmation card (the reason is the customer's own, unchanged)",
    "return_submitted": "After the customer confirms: the return request exists",
    "window_closed_before": "The return window is closed (shown before any reason is asked); ticket offer follows",
    "ticket_offer_review": "The offer of a support ticket after a refused return",
    "ticket_plain": "The plain ticket sentence at the end of an ineligible message",
    "cat_window_closed": "The chosen reason's window closed but the standard one is open",
    "cat_window_closed_both": "Both the chosen reason's window and the standard one are closed",
    "ineligible_order_not_found": "Return refused: order not on the account",
    "ineligible_item_not_found": "Return refused: item not on that order",
    "ineligible_status": "Return refused: the order is not delivered ({status} is the order's status word)",
    "ineligible_status_plain": "Return refused: the order is not delivered (no status available)",
    "ineligible_active": "Return refused: an active return already exists",
    "ineligible_no_delivery_date": "Return refused: no delivery date on record",
    "outside_window_body": "Return refused: the window for this kind of return closed on a date",
    "outside_window_body_generic": "Return refused: the window closed (no date available)",
    "cannot_start": "Return refused for any other reason",
    "which_item": "The model named the wrong item on an order: asks which item",
    "which_order_for_item": "Several delivered orders hold the product and none was named: asks which order",
    "which_order_to_return": "A return was asked for, several delivered orders could fit and none was named: asks which order (IDs and statuses only)",
    "refund_pending": "A reply promised a pending refund: replaced by the status said plainly",
    "refund_processed": "A reply said a processed refund had arrived: replaced by the status said plainly",
    "refund_failed": "A reply promised a failed refund: replaced by the status and a ticket offer",
    # issue management, greeting, thanks, scope, fallback, read-only ambiguity
    "greeting": "The customer greets the assistant (hello, namaste, नमस्ते)",
    "thanks": "The customer says thanks or goodbye (thanks, shukriya, धन्यवाद)",
    "scope_redirect": "The message is off topic (weather, jokes...): redirects to what the assistant can help with",
    "timed_out": "Added when an open question or choice expired before the customer answered",
    "relation_question": "A new problem may or may not belong to the one already being handled: asks which",
    "relation_same": "Button: same issue",
    "relation_new": "Button: a separate issue",
    "relation_left": "The customer picked 'never mind' on that question",
    "third_question": "A third problem arrives while two are open: asks which to set aside",
    "third_drop_active": "Button: drop the issue being handled now",
    "third_drop_suspended": "Button: drop the issue on hold",
    "third_keep": "Button: keep both, ignore the new one",
    "third_kept": "The customer chose to keep both and ignore the new problem",
    "notice_dropped_active": "Added after the customer drops the active issue",
    "notice_dropped_hold": "Added after the customer drops the issue on hold",
    "notice_returning": "Added when the conversation returns to an earlier issue",
    "notice_on_hold": "Added when the earlier issue is put on hold for a separate new one",
    "item_fallback": "Used inside other sentences when an item's product name is unavailable",
    "which_order_status": "Several orders hold the product the customer named and none was typed: asks which order (IDs and statuses only; nothing is read first)",
}
HIGH = {  # wording where tone or meaning can change what the customer believes or does
    "refund_pending": "must not read as a promise or a refusal; mixes the English status word Pending",
    "refund_processed": "must not suggest the money has arrived; mixes the English status word Processed",
    "refund_failed": "must not promise; offers a ticket",
    "ticket_offer_review": "a ticket records the request: no review, reply, human follow-up or outcome may be implied (OD-05: no working human route exists)",
    "tickets_more_hint": "DRAFT, pending fluent human review: this sentence names the paging phrase the engine also recognises in the same language (aur tickets dikhao / और टिकट दिखाइए, plus close variants). Both the wording and the recognised phrases are machine-written and NOT approved",
    "return_card_lead": "the customer confirms on the strength of this sentence ('eligible')",
    "return_card_summary": "the customer confirms on this card; the stored reason is shown unchanged",
    "grounding": "tells the customer nothing was done",
    "lead_limit": "drops the English {limit} phrase in Hinglish and Hindi",
    "reason_notice": "tells the customer their next message is stored exactly as typed",
    "created": "tells the customer a ticket exists",
    "return_submitted": "tells the customer a return request exists ('requested', not approved)",
    "which_order": "lists only IDs and statuses; the question must be neutral",
    "not_on_account": "must not hint whose the order is",
    "ticket_out_of_scope": "refuses a ticket; must be polite and must not sound like a ticket exists; NEW: DRAFT, awaiting fluent review",
    "ticket_scope_ask": "asks what went wrong; must not imply a ticket was prepared; NEW: DRAFT, awaiting fluent review",
    "ticket_describe_ask": "asks the customer to write the description; must say their words are used as written and that nothing is saved before Confirm; NEW: DRAFT, awaiting fluent review",
    "ticket_service_ask": "offers a ticket for something the assistant cannot do; must not promise the change; asks for the details in the customer's words; NEW: DRAFT, awaiting fluent review",
}
MEDIUM = {
    "which_order_to_return": "neutral question listing IDs and statuses; NEW, added after the first review sheet: DRAFT, awaiting fluent review",
    "greeting": "first thing a customer reads; lists what the assistant can do", "thanks": "friendly register",
    "scope_redirect": "polite refusal wording", "timed_out": "tells the customer something expired",
    "relation_question": "neutral question; the buttons must match it", "third_question": "asks which problem to set aside",
    "third_drop_active": "UI button label", "third_drop_suspended": "UI button label", "third_keep": "UI button label",
    "relation_same": "UI button label", "relation_new": "UI button label",
    "which_order_status": "neutral question listing IDs and statuses only",
    "cat_damaged": "UI button label", "cat_wrong_product": "UI button label", "cat_standard": "UI button label",
    "cat_cancel": "UI button label ('never mind': 'Rehne dijiye' / 'रहने दीजिए')",
    "catname_damaged": "reads inside other sentences", "catname_wrong_product": "reads inside other sentences",
    "catname_standard": "same word in Hinglish and English; Hindi 'सामान्य रिटर्न'",
    "cat_window_closed": "long, date and number inside the sentence", "cat_window_closed_both": "long, two dates and numbers",
    "window_closed_before": "long, date and number inside the sentence",
    "outside_window_body": "date and number inside the sentence", "ineligible_status": "English status word inside the sentence",
    "refuse_expired": "tells the customer something expired", "refuse_done": "tells the customer it was not repeated",
    "choice_expired": "tells the customer something expired", "declined": "polite refusal wording",
    "unclear": "asks for yes or no (Hindi uses हाँ/नहीं)", "ask_issue": "friendly register",
    "ambiguous_help": "apology register ('Mujhe afsos hai' / 'मुझे खेद है')",
}




def esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def priority_of(key: str) -> str:
    if key in HIGH:
        return f"**High**: {HIGH[key]}. AWAITING FLUENT HUMAN REVIEW"
    if key in MEDIUM:
        return f"Medium: {MEDIUM[key]}"
    return "Low"


def render(out_dir: Optional[Path] = None) -> tuple[Path, Path]:
    """Write the Markdown and CSV review sheets; returns their paths."""
    out_dir = Path(out_dir) if out_dir else ROOT / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    missing = [k for k in flow_texts.TEXTS["en"] if k not in WHEN]
    if missing:
        raise SystemExit(f"describe these catalogue keys in WHEN first: {missing}")
    rows = []
    for n, key in enumerate(flow_texts.TEXTS["en"], 1):
        rows.append(f"| {n} | `{key}` | {esc(WHEN[key])} | {esc(flow_texts.TEXTS['en'][key])} | "
                    f"{esc(flow_texts.TEXTS['hinglish'][key])} | {esc(flow_texts.TEXTS['hi'][key])} | {priority_of(key)} |")
    count = len(rows)
    high = sum(1 for k in flow_texts.TEXTS["en"] if k in HIGH)
    doc = f"""# Hinglish and Hindi wording for review

Status: **DRAFT** for every Hinglish and Hindi row. Written by the assistant, not yet read by a fluent speaker
(`REVIEW_STATUS` in `app/tools/flow_texts.py` says so and a test pins it). English is the reference and is exactly what the
engine said before localisation. All {count} sentences live in ONE catalogue, `app/tools/flow_texts.py`. (The eligibility
paragraphs that carry times are in `app/tools/return_texts.py`: they were reviewed with the first sheet and are unchanged.)
This sheet is generated by `scripts/localization_review.py`; a CSV with blank verdict, notes and suggestion columns for the
reviewer is generated beside it.

How to review: for each row check (1) it reads naturally, (2) the tone is polite and equally formal across rows (the draft
uses "aap"/"kripya"/"कृपया" and "main"/"मैं" for the assistant), (3) it promises no more than the English does (a ticket is a
request for review; a pending refund is not a promise), (4) the Hinglish spelling is what customers write, (5) the same thing
is called the same word everywhere.

Terminology used throughout. Hinglish keeps the English words return, order, item, ticket, confirm, support, agent, window,
status because customers write them that way. Hindi uses रिटर्न, ऑर्डर, आइटम, टिकट, पुष्टि, सपोर्ट, एजेंट, अवधि, स्थिति. Identifiers,
status words (Pending, Processed, Failed, Shipped), IDs and the typed word `cancel` are never translated. Placeholders in
braces are filled by code.

Every row is awaiting a fluent reader; the {high} rows marked High are **awaiting fluent human review** first (the three refund-status sentences, which replace any promise a
reply makes about a refund; the ticket offer and the two confirmation cards, which the customer acts on; the sentences that say
a return or ticket now exists; the storing notice; the "nothing was done" reply; the not-on-account reply and the which-order
question). Medium rows are interface labels, sentences with dates or numbers inside them, and apology, expiry and greeting
wording. Known compromises: `lead_limit` drops the English {{limit}} phrase in Hinglish and Hindi; `catname_standard` is the same
word in English and Hinglish; Hindi `पात्र` (eligible) in the return card is formal.

| # | Key | When it is shown | English | Hinglish (draft) | Hindi (draft) | Review priority |
|---|---|---|---|---|---|---|
""" + "\n".join(rows) + "\n"
    md = out_dir / "localization_review_hinglish_hindi.md"
    md.write_text(doc, encoding="utf-8", newline="\n")
    csv_path = out_dir / "localization_review_hinglish_hindi.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["number", "key", "when_shown", "english", "hinglish_draft", "hindi_draft", "review_priority",
                         "reviewer_verdict (ok / change / unclear)", "reviewer_notes", "reviewer_suggested_hinglish",
                         "reviewer_suggested_hindi"])
        for n, key in enumerate(flow_texts.TEXTS["en"], 1):
            writer.writerow([n, key, WHEN[key], flow_texts.TEXTS["en"][key], flow_texts.TEXTS["hinglish"][key],
                             flow_texts.TEXTS["hi"][key], priority_of(key).replace("**", ""), "", "", "", ""])
    return md, csv_path


if __name__ == "__main__":
    paths = render()
    print("wrote", *[str(p) for p in paths])
