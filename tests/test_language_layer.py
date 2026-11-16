"""The language-aware layer: the customer's language is decided by code, the wording that matters is written once per
language, and what a reply leaves out is added in the customer's own language. No model call decides or translates.

Why: a live ablation (six repetitions per variant, two English questions) showed a small model answering English
questions in Hinglish when asked to put English tool text "in the customer's language" (6 of 12 replies under the
current prompt, 0 of 59 under any single wording change), and dropping sentences from lists. Offline: scripted models
only. No network.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import real_eval as ev  # noqa: E402
import app.backend  # noqa: E402,F401
from app.backend import Backend, Settings  # noqa: E402
from app.llm.base import LLMResult, ToolCall  # noqa: E402
from app.orchestrator import guard  # noqa: E402
from app.tools import return_texts as rt  # noqa: E402
from app.tools.context import CustomerContext  # noqa: E402
from app.tools.registry import execute_tool  # noqa: E402
from backend_helpers import Client, ScriptedLLM  # noqa: E402

EVAL = Path(__file__).resolve().parents[1] / "app" / "db" / "data" / "eval"
CHECK = "check_return_eligibility"
UTC_OPEN = "11 November 2026, 00:00 UTC"
UTC_CLOSED = "6 November 2026, 00:00 UTC"
ORDINARY_OPEN = {"open": True, "when": UTC_OPEN, "seconds_left": 96 * 3600}
ORDINARY_CLOSED = {"open": False, "when": "4 November 2026, 00:00 UTC", "seconds_left": 0}
DEADLINE_OPEN = {"open": True, "when": "7 November 2026, 12:00 UTC", "seconds_left": 12 * 3600}
DEADLINE_CLOSED = {"open": False, "when": UTC_CLOSED, "seconds_left": 0}


def every_message() -> list[str]:
    out: list[str] = []
    for name in ("queries_conversational.csv", "queries.csv"):
        with open(EVAL / name, encoding="utf-8", newline="") as handle:
            out += [row["query"] for row in csv.DictReader(handle)]
    for name in ("conversations.json", "conversations_identity.json", "conversations_eligibility.json",
                 "heldout/heldout_v1.json"):
        out += [t["say"] for c in json.loads((EVAL / name).read_text(encoding="utf-8")) for t in c["turns"]]
    return out


class TestDetectionParityWithTheHarness:
    def test_the_production_detector_agrees_with_the_harness_on_every_evaluation_message(self):
        messages = every_message()
        assert len(messages) > 150
        assert [guard.detect_language(m) for m in messages] == [ev.detect_language(m) for m in messages]

    def test_the_two_lexicons_are_the_same(self):
        assert guard._HINGLISH_WORDS == ev._HINGLISH_WORDS

    def test_non_english_messages_are_found_and_english_ones_are_not(self):
        found = {m for m in every_message() if guard.detect_language(m) != "en"}
        assert len(found) >= 20
        assert "Can I still return my earbuds from ORD1001?" not in found
        assert "kya main ORD1001 ke earbuds abhi bhi return kar sakta hu?" in found


class TestCustomerLanguage:
    @pytest.mark.parametrize("text", ["Can I still return my earbuds from ORD1001?", "Where is ORD1002?",
                                      "How long does a refund take?", "Where is my order, bhai?",
                                      "I want a refund, yaar", "Is it too late to return the charger?",
                                      "Has ORD1014 shipped yet?", "Is ORD1002 on its way?",
                                      "ORD1015 still hasn't come"])
    def test_a_clearly_english_message_overrides_a_hinglish_or_hindi_session(self, text):
        for previous in ("hinglish", "hi", "en"):
            assert guard.customer_language(text, previous) == "en"

    @pytest.mark.parametrize("text, expected", [
        ("kya main ORD1001 ke earbuds abhi bhi return kar sakta hu?", "hinglish"),
        ("mera order kab aayega bhai", "hinglish"), ("मुझे रिफंड चाहिए", "hi"), ("चार्जर खराब है", "hi")])
    def test_hinglish_and_hindi_are_recognised_whatever_the_session_was(self, text, expected):
        for previous in ("en", "hinglish", "hi"):
            assert guard.customer_language(text, previous) == expected

    @pytest.mark.parametrize("text", ["ok", "yes", "thanks", "ORD1001", "mera order", "ji haan", "kya?", "",
                                      "ORD1007 wala", "sorry, I meant ORD1005"])
    def test_a_genuinely_ambiguous_message_keeps_the_previous_language(self, text):
        for previous in ("en", "hinglish", "hi"):
            assert guard.customer_language(text, previous) == previous

    def test_an_unknown_previous_language_falls_back_to_english(self):
        assert guard.customer_language("ok", "klingon") == "en" and guard.customer_language("ok") == "en"

    def test_one_hinglish_word_in_an_english_sentence_stays_english(self):
        assert guard.customer_language("Can you please check my order bhai", "hinglish") == "en"

    def test_the_session_follows_the_customer_both_ways(self, temp_db):
        backend = Backend(llm=ScriptedLLM(chat=lambda s, m, t: LLMResult(text="ok")), settings=Settings())
        c = Client(backend, "C101")
        session = backend.registry.get(c.state._handle)
        for text, expected in (("mera order kab aayega bhai", "hinglish"), ("ok", "hinglish"),
                               ("Where is ORD1002?", "en"), ("ji haan", "en"), ("मेरा ऑर्डर कहाँ है?", "hi"),
                               ("Can I still return the earbuds from ORD1001?", "en")):
            c.say(text)
            assert session.language == expected, text


class TestDamageCues:
    @pytest.mark.parametrize("text", ["My charger came broken", "the earbuds arrived damaged", "it is faulty",
                                      "item kharab hai", "galat item aaya", "चार्जर खराब है", "I got the wrong item",
                                      "it stopped working"])
    def test_a_damage_or_wrong_item_report_is_found(self, text):
        assert guard.reports_damage(text)

    @pytest.mark.parametrize("text", ["Can I still return my earbuds?", "kya main return kar sakta hu?", "", "Where is it?"])
    def test_an_ordinary_question_is_not(self, text):
        assert not guard.reports_damage(text)


PLACEHOLDER = re.compile(r"\{[a-z]+\}")


class TestTemplatesHaveParity:
    def test_all_three_languages_define_the_same_keys(self):
        assert set(rt.TEXTS) == set(rt.LANGUAGES) == {"en", "hinglish", "hi"}
        assert set(rt.TEXTS["hinglish"]) == set(rt.TEXTS["en"]) == set(rt.TEXTS["hi"])

    def test_every_template_has_the_same_placeholders_in_every_language(self):
        for key in rt.TEXTS["en"]:
            found = {lang: sorted(PLACEHOLDER.findall(rt.TEXTS[lang][key])) for lang in rt.LANGUAGES}
            assert found["hinglish"] == found["en"] == found["hi"], key

    def test_the_review_status_is_recorded_and_not_forgotten(self):
        assert rt.REVIEW_STATUS["en"] == "approved"
        assert rt.REVIEW_STATUS["hinglish"].startswith("draft") and rt.REVIEW_STATUS["hi"].startswith("draft")

    def test_each_language_is_recognised_as_itself_by_the_detector(self):
        for lang, expected in (("en", "en"), ("hinglish", "hinglish"), ("hi", "hi")):
            for key, template in rt.TEXTS[lang].items():
                if key in ("less_than_hour", "one_hour", "hours", "days"):
                    continue
                sentence = template.format(when=UTC_OPEN, left=rt.time_left(lang, 5 * 3600))
                assert guard.detect_language(sentence) == expected, (lang, key, sentence)

    def test_english_and_hinglish_have_no_devanagari_and_hindi_has_plenty(self):
        devanagari = re.compile(r"[ऀ-ॿ]")
        for key, text in rt.TEXTS["en"].items():
            assert not devanagari.search(text) and not devanagari.search(rt.TEXTS["hinglish"][key]), key
        assert all(len(devanagari.findall(rt.TEXTS["hi"][k])) >= 3 for k in rt.TEXTS["hi"])

    @pytest.mark.parametrize("lang", rt.LANGUAGES)
    def test_every_period_sentence_carries_the_utc_text_unchanged(self, lang):
        for kind in ("ordinary", "deadline"):
            for is_open in (True, False):
                sentence = rt.period_sentence(lang, kind, is_open, UTC_OPEN, 5 * 3600)
                assert UTC_OPEN in sentence and "UTC" in sentence

    @pytest.mark.parametrize("lang", rt.LANGUAGES)
    def test_time_left_never_reads_as_a_bare_number_or_zero(self, lang):
        for seconds in (0, 1800, 3599, 3600, 7200, 47 * 3600, 48 * 3600, 156 * 3600):
            text = rt.time_left(lang, seconds)
            assert text and not text.strip().isdigit() and not text.startswith("0")
        assert rt.time_left("en", 1800) == "less than 1 hour" and rt.time_left("en", 156 * 3600) == "about 6 days"
        assert rt.time_left("hinglish", 1800) == "1 ghante se kam" and rt.time_left("hi", 7200) == "लगभग 2 घंटे"

    def test_an_unknown_language_falls_back_to_english(self):
        assert rt.limits_sentence("klingon") == rt.limits_sentence("en")


@pytest.mark.parametrize("lang", rt.LANGUAGES)
class TestParagraphRules:
    def test_no_reason_given_and_a_closed_deadline_does_not_mention_the_deadline(self, lang):
        p = rt.paragraphs(lang, ORDINARY_OPEN, DEADLINE_CLOSED)["no_reason_given"]
        assert UTC_OPEN in p and UTC_CLOSED not in p and rt.has_limits(p)
        assert not rt.has_photos(p)

    def test_no_reason_given_and_an_open_deadline_states_both(self, lang):
        p = rt.paragraphs(lang, ORDINARY_OPEN, DEADLINE_OPEN)["no_reason_given"]
        assert UTC_OPEN in p and DEADLINE_OPEN["when"] in p and rt.has_limits(p) and not rt.has_photos(p)

    def test_no_reason_given_and_a_closed_ordinary_period_offers_support_not_limits(self, lang):
        p = rt.paragraphs(lang, ORDINARY_CLOSED, DEADLINE_CLOSED)["no_reason_given"]
        assert ORDINARY_CLOSED["when"] in p and rt.has_ticket(p) and not rt.has_limits(p) and not rt.has_photos(p)

    def test_damage_with_an_open_deadline_gives_the_deadline_and_the_photo_sentence_only(self, lang):
        p = rt.paragraphs(lang, ORDINARY_OPEN, DEADLINE_OPEN)["item_damaged_or_wrong"]
        assert DEADLINE_OPEN["when"] in p and rt.has_photos(p) and UTC_OPEN not in p

    def test_damage_with_a_closed_deadline_says_it_passed_offers_support_and_never_mentions_photos(self, lang):
        p = rt.paragraphs(lang, ORDINARY_OPEN, DEADLINE_CLOSED)["item_damaged_or_wrong"]
        assert UTC_CLOSED in p and rt.has_ticket(p) and not rt.has_photos(p) and UTC_OPEN not in p

    def test_any_other_reason_never_mentions_the_deadline_or_photos(self, lang):
        p = rt.paragraphs(lang, ORDINARY_OPEN, DEADLINE_OPEN)["any_other_reason"]
        assert UTC_OPEN in p and DEADLINE_OPEN["when"] not in p and not rt.has_photos(p)

    def test_the_photo_sentence_promises_nothing_and_offers_a_ticket(self, lang):
        assert rt.has_ticket(rt.photos_sentence(lang))

    def test_the_closed_deadline_wording_does_not_imply_photos_can_restore_it(self, lang):
        assert not rt.has_photos(rt.closed_support_sentence(lang))


class TestTheToolSpeaksTheCustomersLanguage:
    def facts(self, language):
        ctx = CustomerContext("C101", "ref", language)
        data = json.loads(execute_tool(ctx, CHECK, {"order_id": "ORD1001", "item_id": "IT101"}).to_json())["data"]
        return data

    def test_english_hinglish_and_hindi_paragraphs_are_returned_ready_made(self, temp_db):
        for language in rt.LANGUAGES:
            data = self.facts(language)
            assert data["language"] == language
            paragraph = data["facts_to_state"]["no_reason_given"]
            assert guard.detect_language(paragraph) == language and UTC_OPEN in paragraph

    def test_the_default_context_is_english(self, temp_db):
        ctx = CustomerContext("C101", "ref")
        assert ctx.language == "en"
        data = json.loads(execute_tool(ctx, CHECK, {"order_id": "ORD1001", "item_id": "IT101"}).to_json())["data"]
        assert guard.detect_language(data["facts_to_state"]["no_reason_given"]) == "en"

    def test_a_bad_language_is_refused(self):
        with pytest.raises(ValueError):
            CustomerContext("C101", "ref", "klingon")

    def test_the_note_tells_the_model_not_to_translate_or_reword(self, temp_db):
        note = execute_tool(CustomerContext("C101", "ref"), CHECK, {"order_id": "ORD1001", "item_id": "IT101"}).note
        assert "already written in the customer's language" in note and "do not translate it" in note
        assert "do not reword it" in note and "in the customer's language, keeping" not in note

    def test_the_closed_deadline_is_absent_from_the_general_paragraph_in_real_data(self, temp_db):
        paragraph = self.facts("en")["facts_to_state"]["no_reason_given"]
        assert "6 November 2026" not in paragraph and "11 November 2026, 00:00 UTC" in paragraph


class Sequence(ScriptedLLM):
    interprets_language = True

    def __init__(self, *answers):
        super().__init__(chat=self._next)
        self.answers = list(answers)
        self.seen: list[list[dict]] = []

    def _next(self, system, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.answers.pop(0)


def ask(order, item, call_id="e1"):
    return LLMResult(tool_calls=(ToolCall(CHECK, {"order_id": order, "item_id": item}, call_id),))


def reply_to(temp_customer, message, *model_steps, session_language=None):
    llm = Sequence(*model_steps)
    backend = Backend(llm=llm, settings=Settings())
    c = Client(backend, temp_customer)
    if session_language:
        backend.registry.get(c.state._handle).language = session_language
    return c.say(message), llm


class TestTheEngineAddsWhatTheReplyLeftOut:
    def test_the_limits_are_added_in_english_when_the_reply_dropped_them(self, temp_db):
        reply, _ = reply_to("C101", "Can I still return my earbuds from ORD1001?", ask("ORD1001", "IT101"),
                            LLMResult(text="You can request a return until 11 November 2026, 00:00 UTC."))
        assert reply.text.startswith("You can request a return until")
        assert reply.text.endswith(rt.limits_sentence("en")) and "\n\n" in reply.text

    def test_the_limits_are_added_in_hinglish_for_a_hinglish_customer(self, temp_db):
        reply, _ = reply_to("C101", "kya main ORD1001 ke earbuds abhi bhi return kar sakta hu?", ask("ORD1001", "IT101"),
                            LLMResult(text="Haan, 11 November 2026, 00:00 UTC tak return kar sakte hain."))
        assert reply.text.endswith(rt.limits_sentence("hinglish"))

    def test_the_limits_are_added_in_hindi_for_a_hindi_customer(self, temp_db):
        reply, _ = reply_to("C101", "क्या मैं ORD1001 के ईयरबड्स अभी भी रिटर्न कर सकता हूँ?", ask("ORD1001", "IT101"),
                            LLMResult(text="हाँ, 11 November 2026, 00:00 UTC तक।"))
        assert reply.text.endswith(rt.limits_sentence("hi"))

    def test_a_reply_that_already_says_it_is_not_given_a_second_copy(self, temp_db):
        said = "Until 11 November 2026, 00:00 UTC. I cannot check the item's condition or its packaging."
        reply, _ = reply_to("C101", "Can I still return my earbuds from ORD1001?", ask("ORD1001", "IT101"),
                            LLMResult(text=said))
        assert reply.text == said

    def test_an_english_message_overrides_a_hinglish_session_for_the_added_sentence(self, temp_db):
        reply, llm = reply_to("C101", "Can I still return my earbuds from ORD1001?", ask("ORD1001", "IT101"),
                              LLMResult(text="Until 11 November 2026, 00:00 UTC."), session_language="hinglish")
        assert reply.text.endswith(rt.limits_sentence("en"))
        tool = [json.loads(m["content"]) for m in llm.seen[1] if m["role"] == "tool"][0]
        assert tool["data"]["language"] == "en"

    def test_an_ambiguous_message_uses_the_session_language_for_the_added_sentence(self, temp_db):
        reply, llm = reply_to("C101", "ORD1001 IT101", ask("ORD1001", "IT101"),
                              LLMResult(text="11 November 2026, 00:00 UTC tak."), session_language="hinglish")
        assert reply.text.endswith(rt.limits_sentence("hinglish"))

    def test_photos_are_added_when_damage_is_reported_and_the_deadline_is_open(self, temp_db):
        reply, _ = reply_to("C103", "My charger from ORD1005 came broken. Can I still return it?", ask("ORD1005", "IT106"),
                            LLMResult(text="The reporting deadline is open until 7 November 2026, 12:00 UTC."))
        assert reply.text.endswith(rt.photos_sentence("en")) and rt.limits_sentence("en") not in reply.text

    def test_photos_are_added_in_hinglish_for_a_hinglish_damage_report(self, temp_db):
        reply, _ = reply_to("C103", "ORD1005 ka charger kharab aaya hai, kya main abhi return kar sakta hu?",
                            ask("ORD1005", "IT106"), LLMResult(text="Deadline 7 November 2026, 12:00 UTC tak khuli hai."))
        assert reply.text.endswith(rt.photos_sentence("hinglish"))

    def test_a_closed_deadline_gets_the_support_offer_and_never_the_photo_sentence(self, temp_db):
        reply, _ = reply_to("C101", "My earbuds from ORD1001 arrived damaged. Can I still return them?",
                            ask("ORD1001", "IT101"),
                            LLMResult(text="The reporting deadline closed on 6 November 2026, 00:00 UTC."))
        assert reply.text.endswith(rt.closed_support_sentence("en")) and not rt.has_photos(reply.text)

    def test_nothing_is_added_when_the_reply_already_offers_a_ticket(self, temp_db):
        said = "The reporting deadline closed on 6 November 2026, 00:00 UTC. I can raise a support ticket."
        reply, _ = reply_to("C101", "My earbuds from ORD1001 arrived damaged. Can I still return them?",
                            ask("ORD1001", "IT101"), LLMResult(text=said))
        assert reply.text == said

    def test_nothing_is_added_when_both_periods_have_closed_and_no_damage_was_reported(self, temp_db):
        said = "The ordinary return period closed on 4 November 2026, 00:00 UTC."
        reply, _ = reply_to("C102", "Can I still return the item from ORD1003?", ask("ORD1003", "IT103"),
                            LLMResult(text=said))
        assert reply.text == said

    def test_nothing_is_added_to_an_answer_that_did_not_use_the_check(self, temp_db):
        reply, _ = reply_to("C101", "Hello there, can you help me?", LLMResult(text="Of course."))
        assert "Of course." in reply.text and rt.limits_sentence("en") not in reply.text

    def test_a_blocked_check_adds_nothing(self, temp_db):
        said = "A return cannot be started because the order is still shipped."
        reply, _ = reply_to("C101", "Can I return the charger from ORD1002?", ask("ORD1002", "IT102"), LLMResult(text=said))
        assert reply.text == said

    def test_no_second_model_call_is_made_for_any_of_this(self, temp_db):
        reply, llm = reply_to("C101", "Can I still return my earbuds from ORD1001?", ask("ORD1001", "IT101"),
                              LLMResult(text="Until 11 November 2026, 00:00 UTC."))
        assert llm.chat_calls == 2 and not llm.complete_calls or all("interpret" not in c for c in llm.complete_calls)
