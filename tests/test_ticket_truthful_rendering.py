"""Phase 1: the ticket-reference guard in ``Engine._finish_text`` and the honest ticket-created confirmation
(design NT-60, NT-61, NT-102, NT-105; owner decision OD-05).

A ticket reference may be stated in a reply only when it is VERIFIED: it appears in this turn's tool results for the
signed-in customer, or a successful creation in this session returned it. What a model wrote or a customer typed verifies
nothing. The cleaned text, not the model's, is what the history keeps.
"""

from __future__ import annotations

import json
import re

import pytest

from app.backend import Backend, Settings
from app.db import repository
from app.db.errors import DatabaseOperationalError
from app.llm.base import LLMResult, ToolCall
from app.orchestrator import guard
from app.orchestrator.engine import _Ctx
from app.tools import flow_texts
from backend_helpers import Client, ScriptedLLM

FAKE_LONG = "TKT_" + "0" * 32
TRIGGER = "ZZGUARD show my tickets please"


def scripted(script):
    """A model that follows ``script(messages)`` for messages that start with ZZGUARD and is the offline stand-in otherwise
    (so the ordinary ticket flow still works for creating real tickets)."""
    llm = ScriptedLLM()

    def chat(system, messages, tools):
        last = next((m for m in reversed(messages) if m.get("role") == "user"), {}).get("content", "")
        if "ZZGUARD" in last:
            return script(messages)
        return llm.base.chat(system, messages, tools, max_output_tokens=512)

    llm.chat_fn = chat
    return llm


def say_text(text):
    return lambda messages: LLMResult(text=text)


def make_ticket(c, tag="x"):
    c.say("I want to speak to a human agent")
    card = c.say(f"My earbuds from ORD1001 stopped working after two days ({tag})")
    done = c.confirm(card)
    ticket_id = re.search(r"TKT_[0-9a-f]{32}", done.text).group(0)
    return done, ticket_id


def tool_message_ids(messages):
    """The ticket IDs in the tool results the model was shown this turn (what an honest model would quote)."""
    found = []
    for m in messages:
        if m.get("role") == "tool":
            found += re.findall(r"TKT_[0-9a-f]{32}", m.get("content", ""))
    return found


def session_of(backend, client):
    return backend.registry.get(client.state._handle)


# ---------------------------------------------------------------- the guard function itself

class TestGuardFunctions:
    def test_refs_are_found_in_every_form_case_insensitively(self):
        assert guard.ticket_refs(f"see TKT_1 and {FAKE_LONG} and tkt_ABC9 and TKT_") == {"tkt_1", FAKE_LONG.lower(), "tkt_abc9"}
        assert guard.ticket_refs("") == set() and guard.ticket_refs("no reference ATKT_1x") == set()

    def test_only_unverified_sentences_are_dropped(self):
        ok = "TKT_" + "a" * 32
        text = f"Your ticket {ok} is open. Ticket {FAKE_LONG} is closed. Anything else?"
        assert guard.strip_unverified_ticket_ids(text, {ok}) == f"Your ticket {ok} is open. Anything else?"

    def test_unchanged_when_everything_is_verified_and_empty_when_nothing_is(self):
        ok = "TKT_" + "b" * 32
        assert guard.strip_unverified_ticket_ids(f"Ticket {ok} is open.", {ok}) == f"Ticket {ok} is open."
        assert guard.strip_unverified_ticket_ids("It is TKT_1.", set()) == ""
        assert guard.strip_unverified_ticket_ids("No reference here.", set()) == "No reference here."

    def test_devanagari_sentence_end_is_respected(self):
        assert guard.strip_unverified_ticket_ids("आपका टिकट TKT_9 खुला है। कुछ और?", set()) == "कुछ और?"


# ---------------------------------------------------------------- through the engine (the model is hostile or wrong)

class TestUnverifiedReferencesNeverReachTheCustomer:
    def test_an_invented_normal_form_id_is_removed_and_the_rest_kept(self, temp_db):
        c = Client(Backend(llm=scripted(say_text(f"Your ticket {FAKE_LONG} is being handled. Anything else I can do?"))), "C101")
        out = c.say(TRIGGER)
        assert "TKT_" not in out.text and "Anything else I can do?" in out.text

    def test_a_short_form_is_removed_and_the_fixed_fallback_is_used_when_nothing_remains(self, temp_db):
        c = Client(Backend(llm=scripted(say_text("TKT_1 is open and being worked on."))), "C101")
        out = c.say(TRIGGER)
        assert out.text == flow_texts.text("ticket_id_unverified", "en")

    def test_the_fallback_exists_in_all_three_languages_and_carries_no_reference(self):
        for language in ("en", "hinglish", "hi"):
            text = flow_texts.text("ticket_id_unverified", language)
            assert text and "TKT" not in text and "{" not in text

    def test_the_fallback_follows_the_session_language(self, temp_db):
        backend = Backend(llm=scripted(say_text("x")))
        c = Client(backend, "C101")
        s = session_of(backend, c)
        for language in ("en", "hinglish", "hi"):
            s.language = language
            turn = backend.engine._finish_text(s, "Status: TKT_7.", _Ctx(), False)
            assert turn.text == flow_texts.text("ticket_id_unverified", language)

    def test_the_cleaned_text_is_what_the_history_records(self, temp_db):
        backend = Backend(llm=scripted(say_text(f"Ticket {FAKE_LONG} is being handled. Anything else?")))
        c = Client(backend, "C101")
        c.say(TRIGGER)
        history = session_of(backend, c).history
        assert history[-1]["role"] == "assistant"
        assert "TKT_" not in history[-1]["content"] and "Anything else?" in history[-1]["content"]
        assert all("TKT_" not in m["content"] for m in history)

    def test_an_id_the_customer_typed_is_not_trusted(self, temp_db):
        typed = "TKT_" + "c" * 32
        backend = Backend(llm=scripted(lambda messages: LLMResult(text=f"Ticket {typed} is open. Anything else?")))
        c = Client(backend, "C101")
        out = c.say(f"ZZGUARD what is the status of {typed}")
        assert typed not in out.text
        assert session_of(backend, c).known_ticket_ids == set()  # typing an ID never makes it known

    def test_a_reference_after_the_length_limit_cannot_slip_through_truncation(self, temp_db):
        long_body = ("This sentence is harmless. " * 170) + f"Now ticket {FAKE_LONG} is closed. Done."
        assert len(long_body) > 4000
        c = Client(Backend(llm=scripted(say_text(long_body))), "C101")
        out = c.say(TRIGGER)
        assert "TKT" not in out.text and len(out.text) <= 4001

    def test_an_id_straddling_the_truncation_point_is_removed_whole(self, temp_db):
        sentence = ("x" * 3980) + f" {FAKE_LONG}."
        c = Client(Backend(llm=scripted(say_text(sentence + " Done."))), "C101")
        out = c.say(TRIGGER)
        assert "TKT" not in out.text and "Done." in out.text


class TestLegitimateReferencesSurvive:
    def test_an_id_from_get_my_tickets_this_turn_is_preserved(self, temp_db):
        def script(messages):
            ids = tool_message_ids(messages)
            if not ids:
                return LLMResult(tool_calls=(ToolCall("get_my_tickets", {}, "c1"),))
            return LLMResult(text=f"You have a ticket: {ids[0]}. Anything else?")

        backend = Backend(llm=scripted(script))
        c = Client(backend, "C101")
        _, created = make_ticket(c)
        # a fresh session for the same customer: only the tool result can verify the ID
        c2 = Client(backend, "C101")
        out = c2.say(TRIGGER)
        # a turn that only listed the tickets is rendered by code from the rows, so the model's prose is not used at all
        assert created in out.text and "Anything else?" not in out.text and "support ticket(s)" in out.text
        assert session_of(backend, c2).known_ticket_ids == set()

    def test_an_id_created_earlier_in_the_same_session_is_preserved_without_a_tool_call(self, temp_db):
        holder = {}
        backend = Backend(llm=scripted(lambda messages: LLMResult(text=f"Your reference is {holder['id']}. Anything else?")))
        c = Client(backend, "C101")
        _, holder["id"] = make_ticket(c)
        out = c.say(TRIGGER)
        assert holder["id"] in out.text

    def test_a_session_learns_an_id_only_from_a_successful_creation(self, temp_db, monkeypatch):
        backend = Backend(llm=scripted(say_text("x")))
        c = Client(backend, "C101")
        c.say("I want to speak to a human agent")
        card = c.say("My earbuds from ORD1001 stopped working after two days")

        def boom(*a, **k):
            raise DatabaseOperationalError("database write failed")

        monkeypatch.setattr(repository, "create_support_ticket", boom)
        out = c.confirm(card)
        assert "TKT_" not in out.text
        assert session_of(backend, c).known_ticket_ids == set()

    def test_another_customers_id_cannot_pass(self, temp_db):
        backend = Backend(llm=scripted(lambda messages: LLMResult(text="")))  # replaced below once the ID exists
        other = Client(backend, "C102")
        _, foreign = make_ticket(other, "foreign")
        mine = Client(Backend(llm=scripted(say_text(f"Your ticket {foreign} is open. Anything else?"))), "C101")
        out = mine.say(TRIGGER)
        assert foreign not in out.text and "Anything else?" in out.text

    def test_the_tool_results_of_another_customer_never_verify_anything(self, temp_db):
        """get_my_tickets is scoped by the session's customer in SQL, so customer A's listing holds only A's IDs."""
        backend = Backend(llm=scripted(say_text("x")))
        other = Client(backend, "C102")
        _, foreign = make_ticket(other, "foreign")
        mine = Client(backend, "C101")
        _, own = make_ticket(mine, "own")
        listing = json.dumps(repository.get_customer_tickets("C101"))
        assert own in listing and foreign not in listing


class TestTheFixedCreatedReplyAndTheHonestConfirmation:
    def test_the_created_reply_is_fixed_text_with_the_real_id_and_the_guard_does_not_touch_it(self, temp_db, raw):
        backend = Backend(llm=ScriptedLLM())
        c = Client(backend, "C101")
        done, ticket_id = make_ticket(c)
        row = raw.execute("SELECT ticket_id FROM support_tickets").fetchone()
        assert ticket_id == row["ticket_id"]
        assert done.text == flow_texts.text("created", "en", ticket_id=ticket_id)
        assert ticket_id.lower() in session_of(backend, c).known_ticket_ids

    @pytest.mark.parametrize("language,forbidden", [
        ("en", ["will follow up", "will contact", "will respond", "will be assigned", "agent will"]),
        ("hinglish", ["karega", "karegi", "follow up karega"]),
        ("hi", ["संपर्क करेगा", "संपर्क करेगी", "फॉलो-अप करेगा"]),
    ])
    def test_the_confirmation_confirms_but_promises_no_human_follow_up(self, language, forbidden):
        text = flow_texts.text("created", language, ticket_id="TKT_" + "d" * 32)
        assert "TKT_" + "d" * 32 in text
        for phrase in forbidden:
            assert phrase not in text, phrase

    def test_the_english_confirmation_is_honest_and_says_how_to_check_status(self):
        text = flow_texts.text("created", "en", ticket_id="T1")
        assert text.startswith("Your support ticket T1 has been created.")
        assert "can't currently connect you to a human agent" in text
        assert "can't promise a reply, an assignment or a follow-up" in text
        assert "ask me to show your tickets" in text

    def test_hinglish_and_hindi_say_the_same_three_things(self):
        hing = flow_texts.text("created", "hinglish", ticket_id="T1")
        assert "ban gaya hai" in hing and "human agent se connect nahi kar sakta" in hing and "tickets dikhane" in hing
        hindi = flow_texts.text("created", "hi", ticket_id="T1")
        assert "बन गया है" in hindi and "मानव एजेंट से नहीं जोड़ सकती" in hindi and "टिकट दिखाने" in hindi

    def test_the_status_check_the_text_points_to_really_works(self, temp_db):
        backend = Backend(llm=ScriptedLLM())
        c = Client(backend, "C101")
        _, ticket_id = make_ticket(c)
        assert ticket_id in c.say("show my tickets").text
