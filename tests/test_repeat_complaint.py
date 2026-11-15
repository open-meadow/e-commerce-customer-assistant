"""Phase 0 of context retention: a customer who says they already gave their details is not asked again.

Final live run, case C003 ("I already told you my order number, why do I have to repeat everything?"): the model
sometimes answered "could you please provide the order ID again?" instead of looking at the customer's orders. The
fix is small and stores nothing:

* a prompt sentence says never to ask again, look earlier, otherwise call ``list_my_orders``;
* if a real model still asks for the order ID without looking anything up, it is reminded once with fixed text
  that contains no customer data, and the orders still come from the tool.

The offline stand-in never gets the reminder. Scripted model output only; no network.
"""

from __future__ import annotations

import json

import pytest

from app.backend import Backend, Settings
from app.llm.base import LLMResult, ToolCall
from app.orchestrator import guard
from app.orchestrator.engine import SYSTEM_PROMPT
from backend_helpers import Client, ScriptedLLM

COMPLAINT = "I already told you my order number, why do I have to repeat everything?"
ASK = LLMResult(text="I'm sorry about that. Could you please provide the order ID again?")
LOOKUP = LLMResult(tool_calls=(ToolCall("list_my_orders", {}, "c1"),))
ANSWER = LLMResult(text="Sorry about the repeat. I can see your orders: ORD1001 is delivered.")


class Sequence(ScriptedLLM):
    """One scripted answer per chat call; records every conversation it was shown."""

    def __init__(self, *answers, real_model=True):
        super().__init__(chat=self._next)
        if real_model:
            self.interprets_language = True  # declared like a real model
        self.answers = list(answers)
        self.seen: list[list[dict]] = []

    def _next(self, system, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.answers.pop(0)


def start(temp_db, llm, customer="C101"):
    backend = Backend(llm=llm, settings=Settings())
    return backend, Client(backend, customer)


class TestDetection:
    @pytest.mark.parametrize("text", [
        COMPLAINT, "I already gave you the order number", "why do I have to repeat",
        "I'm tired of repeating myself", "you keep asking, I told you my order already",
        "maine pehle hi bata diya tha", "I have said this over and over",
    ])
    def test_repeat_complaints_are_recognised(self, text):
        assert guard.is_repeat_complaint(text)

    @pytest.mark.parametrize("text", [
        "where is my order", "I want to return the earbuds", "my order ORD1001 has not arrived", "", None, 7,
        "can you repeat the return policy",
    ])
    def test_ordinary_messages_are_not(self, text):
        assert not guard.is_repeat_complaint(text)

    @pytest.mark.parametrize("reply", [
        "Could you please provide the order ID again?", "Please share your order number.",
        "What is your order ID?", "Can you tell me the order number so I can check?",
    ])
    def test_a_request_for_the_order_id_is_recognised(self, reply):
        assert guard.asks_for_order_id(reply)

    @pytest.mark.parametrize("reply", [
        "Your order ORD1001 is on its way.", "I can see two orders on your account.", "", None,
        "The order status is shipped.",
    ])
    def test_other_replies_are_not(self, reply):
        assert not guard.asks_for_order_id(reply)


class TestReminderOnce:
    def test_a_real_model_that_asks_again_is_reminded_and_then_looks_up_the_orders(self, temp_db):
        llm = Sequence(ASK, LOOKUP, ANSWER)
        backend, c = start(temp_db, llm)
        reply = c.say(COMPLAINT)
        assert reply.status == "ok" and "ORD1001" in reply.text and "provide the order ID" not in reply.text
        assert llm.chat_calls == 3
        second = llm.seen[1]
        assert second[-1] == {"role": "user", "content": guard.REPEAT_REMINDER}
        assert second[-2] == {"role": "assistant", "content": ASK.text}
        tools = [json.loads(m["content"]) for m in llm.seen[2] if m["role"] == "tool"]
        assert tools and tools[0]["status"] == "ok" and tools[0]["code"] == "orders"  # from the tool, not memory

    def test_the_reminder_is_fixed_text_with_no_customer_data(self):
        assert "ORD" not in guard.REPEAT_REMINDER and "C101" not in guard.REPEAT_REMINDER

    def test_a_second_ask_after_the_reminder_is_not_challenged_again(self, temp_db):
        llm = Sequence(ASK, ASK)
        backend, c = start(temp_db, llm)
        reply = c.say(COMPLAINT)
        assert llm.chat_calls == 2 and "order ID" in reply.text  # reminded once, then the model's answer stands

    def test_the_reminder_is_not_kept_in_the_conversation_history(self, temp_db):
        llm = Sequence(ASK, LOOKUP, ANSWER, LLMResult(text="Anything else?"))
        backend, c = start(temp_db, llm)
        c.say(COMPLAINT)
        c.say("which of them is the earbuds one")
        last = llm.seen[-1]
        assert all(guard.REPEAT_REMINDER not in str(m.get("content")) for m in last)
        assert ASK.text not in str(last)  # the abandoned question is not replayed either

    def test_nothing_is_written(self, temp_db, raw):
        llm = Sequence(ASK, LOOKUP, ANSWER)
        backend, c = start(temp_db, llm)
        before = [raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("return_requests", "support_tickets")]
        c.say(COMPLAINT)
        after = [raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("return_requests", "support_tickets")]
        assert before == after


class TestWhenItDoesNotApply:
    def test_an_ordinary_question_for_the_id_is_left_alone(self, temp_db):
        llm = Sequence(ASK)
        backend, c = start(temp_db, llm)
        reply = c.say("my package is damaged")
        assert llm.chat_calls == 1 and "order ID" in reply.text

    def test_a_model_that_already_looked_something_up_is_left_alone(self, temp_db):
        llm = Sequence(LOOKUP, ASK)
        backend, c = start(temp_db, llm)
        reply = c.say(COMPLAINT)
        assert llm.chat_calls == 2 and "order ID" in reply.text

    def test_a_complaint_answered_without_asking_is_left_alone(self, temp_db):
        llm = Sequence(LLMResult(text="Sorry about that. Which order is this about?"))
        backend, c = start(temp_db, llm)
        c.say(COMPLAINT)
        assert llm.chat_calls == 1

    def test_the_offline_stand_in_never_gets_the_reminder(self, temp_db):
        llm = Sequence(ASK, real_model=False)
        backend, c = start(temp_db, llm)
        reply = c.say(COMPLAINT)
        assert llm.chat_calls == 1 and "order ID" in reply.text


class TestPrompt:
    def test_the_system_prompt_forbids_asking_again(self):
        assert "never ask for something the customer already gave" in SYSTEM_PROMPT.lower() and "list_my_orders" in SYSTEM_PROMPT
