"""Phase 1, step 1a: the account-level new-ticket limit (design NT-01 to NT-09, NT-86 to NT-89, NT-105).

The limit is counted from persisted ticket rows inside the inserting transaction, so a new chat, a restart, another channel
or another process cannot reset it. These tests turn it on explicitly (``ticket_rate_policy="rolling"``): the shipped default
is ``off`` (gated, open decision B-7), and the per-session counter is unchanged and still tested by the existing suite.
The numbers here are test values; the owner-supplied starting configuration (5 per 86400 s) is only a configuration default
for ``ticket_rate_max`` and ``ticket_rate_window_seconds``, not a policy constant in code.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backend import Backend, Settings
from app.backend.config import ConfigError
from app.db import repository
from backend_helpers import Client, ScriptedLLM

REPO_ROOT = Path(__file__).resolve().parents[1]
W = 86400
BASE = datetime(2026, 11, 10, 0, 0, 0, tzinfo=timezone.utc)


def make(customer="C101", limit=(5, W), text="a broken item"):
    return repository.create_support_ticket(customer, "general", text, "summary", rate_limit=limit)


def count(raw, customer="C101"):
    return raw.execute("SELECT COUNT(*) FROM support_tickets WHERE customer_id = ?", (customer,)).fetchone()[0]


# ---------------------------------------------------------------- boundary and window (NT-03, NT-04)

class TestWindowBoundary:
    def test_fourth_and_fifth_succeed_sixth_is_limited_with_retry_time(self, temp_db, set_now, raw):
        set_now(BASE)
        outcomes = [make()["outcome"] for _ in range(5)]
        assert outcomes == ["created"] * 5
        sixth = make()
        assert sixth["outcome"] == "rate_limited"
        assert sixth["retry_at"] == (BASE + timedelta(seconds=W)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert count(raw) == 5  # nothing was written for the refused attempt

    def test_one_second_before_and_exactly_at_the_window_edge(self, temp_db, set_now, raw):
        set_now(BASE)
        for _ in range(5):
            make()
        set_now(BASE + timedelta(seconds=W - 1))
        assert make()["outcome"] == "rate_limited"
        set_now(BASE + timedelta(seconds=W))  # a row exactly one window old no longer counts
        assert make()["outcome"] == "created"
        assert count(raw) == 6

    def test_just_inside_exactly_at_and_just_outside_the_boundary(self, temp_db, set_now, raw):
        """Five tickets at T0. Just inside (T0 + 24h - 1s): all five count. Exactly 24 h later: none counts (strictly later than
        now - 24h). Just outside (T0 + 24h + 1s): none counts either."""
        set_now(BASE)
        for _ in range(5):
            make()
        for offset, outcome in ((W - 1, "rate_limited"), (W, "created"), (W + 1, "created")):
            set_now(BASE + timedelta(seconds=offset))
            result = make(limit=(5, W))
            assert result["outcome"] == outcome, (offset, result)
            if outcome == "created":  # reset the scenario for the next boundary
                raw.execute("DELETE FROM support_tickets WHERE ticket_id = ?", (result["ticket_id"],))
                raw.commit()

    def test_a_ticket_one_second_younger_than_the_window_still_counts(self, temp_db, set_now, raw):
        set_now(BASE)
        make(limit=None)  # the oldest, created at T0
        set_now(BASE + timedelta(seconds=1))
        for _ in range(4):
            make(limit=None)
        set_now(BASE + timedelta(seconds=W))  # T0's ticket is exactly one window old and drops out: 4 remain
        assert make()["outcome"] == "created"
        assert make()["outcome"] == "rate_limited"  # now 5 (four from T0+1s, one new): the T0+1s tickets are still inside

    def test_retry_time_is_when_the_count_first_drops_below_the_maximum(self, temp_db, set_now):
        for minutes in (0, 10, 20, 30, 40):
            set_now(BASE + timedelta(minutes=minutes))
            make(limit=None)
        set_now(BASE + timedelta(minutes=50))
        first = make(limit=(5, W))
        assert first["outcome"] == "rate_limited"
        assert first["retry_at"] == (BASE + timedelta(seconds=W)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # the same data under a smaller maximum: the count must fall below 2, so the 4th-oldest row (minute 30) must age out
        second = make(limit=(2, W))
        assert second["retry_at"] == (BASE + timedelta(minutes=30, seconds=W)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_data_already_over_the_limit_still_gives_a_correct_retry_time(self, temp_db, set_now):
        for minutes in range(7):  # seven rows created before the limit existed
            set_now(BASE + timedelta(minutes=minutes))
            make(limit=None)
        set_now(BASE + timedelta(minutes=30))
        out = make(limit=(5, W))
        # count 7, maximum 5: the count drops below 5 only when the 3rd-oldest row (minute 2) leaves the window
        assert out["outcome"] == "rate_limited"
        assert out["retry_at"] == (BASE + timedelta(minutes=2, seconds=W)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_utc_text_comparison_across_midnight_and_a_process_timezone(self, temp_db, set_now, monkeypatch):
        monkeypatch.setenv("TZ", "Pacific/Kiritimati")  # a +14:00 local zone must change nothing
        set_now(datetime(2026, 11, 10, 23, 59, 59, tzinfo=timezone.utc))
        for _ in range(5):
            make()
        set_now(datetime(2026, 11, 11, 0, 0, 1, tzinfo=timezone.utc))  # just past midnight UTC: still the same window
        assert make()["outcome"] == "rate_limited"
        set_now(datetime(2026, 11, 11, 23, 59, 59, tzinfo=timezone.utc))
        assert make()["outcome"] == "created"


# ---------------------------------------------------------------- what counts (NT-06, NT-87)

class TestWhatCounts:
    def test_closed_tickets_still_count(self, temp_db, set_now, raw):
        set_now(BASE)
        ids = [make(limit=(5, W))["ticket_id"] for _ in range(5)]
        raw.executemany("UPDATE support_tickets SET status = 'Closed' WHERE ticket_id = ?", [(i,) for i in ids])
        raw.commit()
        assert make()["outcome"] == "rate_limited"  # create-and-close cannot reset the allowance

    def test_another_customer_is_unaffected(self, temp_db, set_now):
        set_now(BASE)
        for _ in range(5):
            make("C101")
        assert make("C101")["outcome"] == "rate_limited"
        assert make("C102")["outcome"] == "created"

    def test_a_refused_attempt_writes_nothing_and_does_not_count(self, temp_db, set_now, raw):
        set_now(BASE)
        for _ in range(5):
            make()
        for _ in range(3):
            assert make()["outcome"] == "rate_limited"
        assert count(raw) == 5

    def test_no_limit_means_no_check(self, temp_db, set_now, raw):
        set_now(BASE)
        for _ in range(8):
            assert make(limit=None)["outcome"] == "created"
        assert count(raw) == 8

    @pytest.mark.parametrize("bad", [(0, W), (5, 0), (-1, W), (True, W), (5, "x"), (5,), "55", 5, (5.0, W)])
    def test_invalid_limit_arguments_are_refused(self, temp_db, bad):
        with pytest.raises(ValueError):
            repository.create_support_ticket("C101", "general", "d", "s", rate_limit=bad)

    def test_read_only_state_matches_the_write_check(self, temp_db, set_now, raw):
        set_now(BASE)
        for _ in range(4):
            make()
        state = repository.get_ticket_rate_state("C101", (5, W))
        assert state["allowed"] is True and state["count"] == 4 and state["retry_at"] is None
        make()
        state = repository.get_ticket_rate_state("C101", (5, W))
        assert state["allowed"] is False and state["count"] == 5 and state["retry_at"] is not None
        assert count(raw) == 5  # the read wrote nothing


# ---------------------------------------------------------------- atomicity (NT-01, NT-86)

class TestAtomicity:
    def test_twelve_threads_never_exceed_the_maximum(self, temp_db, set_now, raw):
        set_now(BASE)
        results, lock = [], threading.Lock()

        def worker():
            out = make(limit=(3, W))["outcome"]
            with lock:
                results.append(out)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results.count("created") == 3 and results.count("rate_limited") == 9
        assert count(raw) == 3

    def test_six_real_processes_never_exceed_the_maximum(self, temp_db, set_now, raw, tmp_path):
        """Each process has its own memory, so only the database can hold the count (the old per-process counters could not)."""
        worker = tmp_path / "worker.py"
        worker.write_text(textwrap.dedent("""
            import sys
            from datetime import datetime, timezone
            from app.db import clock, repository
            fixed = datetime(2026, 11, 10, 0, 0, 0, tzinfo=timezone.utc)
            clock.utc_now = lambda: fixed
            created = 0
            for _ in range(6):
                if repository.create_support_ticket("C101", "general", "d", "s", rate_limit=(3, 86400))["outcome"] == "created":
                    created += 1
            print(created)
        """), encoding="utf-8")
        import os
        env = {**os.environ, "APP_DB_PATH": temp_db, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
        procs = [subprocess.Popen([sys.executable, str(worker)], env=env, cwd=str(tmp_path), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(6)]
        outputs = [p.communicate(timeout=120) for p in procs]
        assert all(p.returncode == 0 for p in procs), [o[1][-300:] for o in outputs]
        assert sum(int(o[0].strip()) for o in outputs) == 3
        assert count(raw) == 3


# ---------------------------------------------------------------- through the backend: new chat, restart, channel (NT-86, NT-88)

def limited_settings(maximum=2, **extra):
    return Settings(ticket_rate_policy="rolling", ticket_rate_max=maximum, ticket_rate_window_seconds=W, **extra)


def ticket_card(c, tag):
    c.say("I want to speak to a human agent")
    return c.say(f"My earbuds from ORD1001 stopped working after two days ({tag})")


class TestAccountLevelThroughTheBackend:
    def test_a_new_chat_a_restart_and_a_second_channel_do_not_reset_the_limit(self, temp_db, set_now, raw):
        set_now(BASE)
        a = Client(Backend(llm=ScriptedLLM(), settings=limited_settings()), "C101")
        for tag in (1, 2):
            assert "has been created" in a.confirm(ticket_card(a, tag)).text
        refused = a.say("I want to speak to a human agent")
        assert "limit for creating new support tickets" in refused.text and refused.status == "ok" and refused.pending is None

        backend_b = Backend(llm=ScriptedLLM(), settings=limited_settings())  # a restart: a fresh Backend, same database
        b = Client(backend_b, "C101")
        assert "limit for creating new support tickets" in b.say("I want to speak to a human agent").text
        voice_like = Client(backend_b, "C101")  # another session for the same customer (text, voice, tab, device: all alike)
        assert "limit for creating new support tickets" in voice_like.say("I want to speak to a human agent").text
        assert count(raw) == 2

    def test_a_different_customer_is_unaffected(self, temp_db, set_now):
        set_now(BASE)
        a = Client(Backend(llm=ScriptedLLM(), settings=limited_settings(1)), "C101")
        a.confirm(ticket_card(a, 1))
        other = Client(Backend(llm=ScriptedLLM(), settings=limited_settings(1)), "C102")
        assert "has been created" in other.confirm(ticket_card(other, 1)).text

    def test_a_card_made_before_the_limit_was_reached_is_refused_at_confirmation(self, temp_db, set_now, raw):
        set_now(BASE)
        backend = Backend(llm=ScriptedLLM(), settings=limited_settings(1))
        a, b = Client(backend, "C101"), Client(backend, "C101")
        card_a = ticket_card(a, "a")  # both chats are shown a card while the allowance is still free
        card_b = ticket_card(b, "b")
        assert "has been created" in b.confirm(card_b).text
        late = a.confirm(card_a)  # the authority is the check inside the write transaction, not the earlier courtesy read
        assert "limit for creating new support tickets" in late.text and "TKT_" not in late.text
        assert count(raw) == 1
        sess = backend.registry.get(a.state._handle)
        assert sess.known_ticket_ids == set() and sess.committed["support_ticket"] == 0  # nothing was created, nothing counted

    def test_the_limit_message_names_a_time_and_promises_no_human(self, temp_db, set_now):
        set_now(BASE)
        a = Client(Backend(llm=ScriptedLLM(), settings=limited_settings(1)), "C101")
        a.confirm(ticket_card(a, 1))
        text = a.say("I want to speak to a human agent").text
        assert (BASE + timedelta(seconds=W)).strftime("%Y-%m-%dT%H:%M:%SZ") in text
        assert "can't currently connect you to a human agent" in text
        assert "follow up" not in text.lower() and "will contact" not in text.lower()

    def test_status_lookup_still_works_at_the_limit(self, temp_db, set_now):
        set_now(BASE)
        a = Client(Backend(llm=ScriptedLLM(), settings=limited_settings(1)), "C101")
        a.confirm(ticket_card(a, 1))
        assert "TKT_" in a.say("show my tickets").text

    def test_cancelled_previews_and_lookups_do_not_count(self, temp_db, set_now, raw):
        set_now(BASE)
        a = Client(Backend(llm=ScriptedLLM(), settings=limited_settings(1)), "C101")
        card = ticket_card(a, 1)
        assert a.confirm(card, accepted=False).status == "ok"
        a.say("show my tickets")
        assert count(raw) == 0
        assert "has been created" in a.confirm(ticket_card(a, 2)).text

    def test_a_retried_confirmation_does_not_create_or_count_twice(self, temp_db, set_now, raw):
        set_now(BASE)
        a = Client(Backend(llm=ScriptedLLM(), settings=limited_settings(2)), "C101")
        card = ticket_card(a, 1)
        a.confirm(card)
        again = a.confirm(card)
        assert "TKT_" not in again.text and count(raw) == 1

    def test_the_defaults_are_the_approved_rolling_five_per_24_hours(self):
        s = Settings()
        assert (s.ticket_rate_policy, s.ticket_rate_max, s.ticket_rate_window_seconds) == ("rolling", 5, 86400)

    def test_the_per_session_counter_is_retired_and_the_account_limit_is_what_stops_the_sixth(self, temp_db, set_now, raw):
        assert not hasattr(Settings(), "max_tickets_per_session") and Settings().cap_for("support_ticket") is None
        set_now(BASE)
        a = Client(Backend(llm=ScriptedLLM()), "C101")  # all defaults: rolling, 5 per 86400 s
        for tag in range(1, 6):  # five in ONE session: the old cap of three no longer applies
            assert "has been created" in a.confirm(ticket_card(a, tag)).text
        out = a.say("I want to speak to a human agent")
        assert "limit for creating new support tickets" in out.text and count(raw) == 5

    def test_the_defaults_alone_enforce_the_limit_across_sessions_and_a_restart(self, temp_db, set_now, raw):
        set_now(BASE)
        first = Client(Backend(llm=ScriptedLLM()), "C101")
        for tag in range(1, 6):
            first.confirm(ticket_card(first, tag))
        again = Client(Backend(llm=ScriptedLLM()), "C101")  # a new Backend on the same database
        assert "limit for creating new support tickets" in again.say("I want to speak to a human agent").text
        assert count(raw) == 5

    def test_explicit_overrides_replace_the_defaults(self, temp_db, set_now, raw):
        set_now(BASE)
        c = Client(Backend(llm=ScriptedLLM(), settings=Settings(ticket_rate_max=2)), "C101")
        c.confirm(ticket_card(c, 1))
        c.confirm(ticket_card(c, 2))
        assert "limit for creating new support tickets" in c.say("I want to speak to a human agent").text
        off = Client(Backend(llm=ScriptedLLM(), settings=Settings(ticket_rate_policy="off")), "C102")
        for tag in range(1, 7):  # explicit off: no account limit
            assert "has been created" in off.confirm(ticket_card(off, tag)).text


# ---------------------------------------------------------------- configuration (NT-08)

class TestConfiguration:
    def test_defaults_are_rolling_five_per_86400_seconds(self):
        s = Settings.from_env({})
        assert s == Settings() and (s.ticket_rate_policy, s.ticket_rate_max, s.ticket_rate_window_seconds) == ("rolling", 5, 86400)

    def test_off_is_an_explicit_choice(self):
        assert Settings.from_env({"SUPPORT_TICKET_RATE_POLICY": "off"}).ticket_rate_policy == "off"
        assert Settings.from_env({"SUPPORT_TICKET_RATE_POLICY": "OFF "}).ticket_rate_policy == "off"

    def test_environment_values(self):
        s = Settings.from_env({"SUPPORT_TICKET_RATE_POLICY": " Rolling ", "SUPPORT_TICKET_RATE_MAX": "7",
                               "SUPPORT_TICKET_RATE_WINDOW_SECONDS": "3600"})
        assert (s.ticket_rate_policy, s.ticket_rate_max, s.ticket_rate_window_seconds) == ("rolling", 7, 3600)

    @pytest.mark.parametrize("variable,value", [
        ("SUPPORT_TICKET_RATE_POLICY", "on"), ("SUPPORT_TICKET_RATE_POLICY", "ROLLING2"),
        ("SUPPORT_TICKET_RATE_MAX", "0"), ("SUPPORT_TICKET_RATE_MAX", "-3"), ("SUPPORT_TICKET_RATE_MAX", "x"),
        ("SUPPORT_TICKET_RATE_MAX", "1.5"), ("SUPPORT_TICKET_RATE_WINDOW_SECONDS", "0"),
        ("SUPPORT_TICKET_RATE_WINDOW_SECONDS", "abc"), ("SUPPORT_TICKET_RATE_WINDOW_SECONDS", "59")])
    def test_invalid_values_stop_startup(self, variable, value):
        with pytest.raises(ConfigError) as caught:
            Settings.from_env({variable: value})
        assert variable in str(caught.value)

    def test_the_h3_settings_are_untouched(self):
        s = Settings.from_env({})
        assert (s.max_requests_per_session, s.max_active_sessions, s.session_requests_per_minute, s.max_body_bytes,
                s.provider_calls_per_session, s.max_returns_per_session) == (300, 200, 30, 16384, 300, 5)
