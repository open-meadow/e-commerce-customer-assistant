"""The persistent usage ledger: offline only. No test sends a request; transports are fakes or httpx.MockTransport."""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import real_eval as ev  # noqa: E402
import run_valid  # noqa: E402
import usage_ledger as ul  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
CHAT_URL = "https://api.example.test/v1/responses"
EMBED_URL = "https://api.example.test/v1/embeddings"


@pytest.fixture
def ledger(tmp_path):
    led = ul.Ledger(tmp_path / "led" / "usage.jsonl", run_id="r1", lock_timeout=2)
    led.init(10, "test ceiling", prior_usage="none")
    return led


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def chat_request(body=b'{"input":"hello"}'):
    return urllib.request.Request(CHAT_URL, data=body, method="POST", headers={"Authorization": "Bearer sk-SECRET-KEY"})


@pytest.fixture
def fake_urlopen(monkeypatch):
    calls = []

    def fake(request, *args, **kwargs):
        calls.append(request)
        return FakeResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def events(led):
    return [json.loads(line) for line in led.path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestStates:
    def test_a_successful_request_is_reserved_sent_and_ok(self, ledger, fake_urlopen):
        with ul.guards(ledger):
            urllib.request.urlopen(chat_request())
        assert [e["event"] for e in events(ledger)] == ["ceiling", "baseline", "reserve", "sent", "outcome"]
        counts = ledger.summary()["requests"]
        assert counts == {"reserved_locally": 1, "sent_to_transport": 1, "ok": 1, "error": 0, "unknown_outcome": 0,
                          "reserved_unsent": 0}

    def test_a_failed_request_is_an_error_and_still_counts(self, ledger, monkeypatch):
        def failing(request, *a, **k):
            raise urllib.error.HTTPError(CHAT_URL, 429, "rate limited", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", failing)
        with ul.guards(ledger):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(chat_request())
        outcome = [e for e in events(ledger) if e["event"] == "outcome"][0]
        assert outcome["result"] == "error" and outcome["status"] == 429
        assert ledger.state()["used"] == 1

    def test_a_crash_after_sending_is_an_unknown_outcome_and_counts_as_spent(self, ledger, fake_urlopen, monkeypatch):
        monkeypatch.setattr(ul.Ledger, "finish", lambda self, *a, **k: (_ for _ in ()).throw(OSError("killed")))
        with ul.guards(ledger):
            with pytest.raises(OSError):
                urllib.request.urlopen(chat_request())
        counts = ledger.summary()["requests"]
        assert counts["unknown_outcome"] == 1 and counts["sent_to_transport"] == 1 and ledger.state()["used"] == 1

    def test_a_crash_between_reservation_and_transmission_is_kept_not_lost(self, ledger, fake_urlopen, monkeypatch):
        monkeypatch.setattr(ul.Ledger, "mark_sent", lambda self, rid: (_ for _ in ()).throw(SystemExit("killed")))
        with ul.guards(ledger):
            with pytest.raises(SystemExit):
                urllib.request.urlopen(chat_request())
        assert fake_urlopen == []  # the transport was never called
        summary = ledger.summary()
        assert summary["requests"]["reserved_unsent"] == 1 and summary["budget"]["used_total"] == 1  # still counted

    def test_only_an_unsent_reservation_can_be_voided(self, ledger, fake_urlopen, monkeypatch):
        monkeypatch.setattr(ul.Ledger, "mark_sent", lambda self, rid: (_ for _ in ()).throw(SystemExit("killed")))
        with ul.guards(ledger):
            with pytest.raises(SystemExit):
                urllib.request.urlopen(chat_request())
        monkeypatch.undo()
        stuck = [r for r in ledger.state()["local"]][0]["req_id"]
        ledger.void(stuck, "process died before transmission; verified in the log")
        assert ledger.state()["used"] == 0
        sent_id = ledger.reserve("chat", "api.example.test", "/v1/responses", "POST", "abc")
        ledger.mark_sent(sent_id)
        with pytest.raises(ul.LedgerError):
            ledger.void(sent_id, "might have been billed")

    def test_loopback_traffic_is_not_provider_usage(self, ledger, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())
        with ul.guards(ledger):
            urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000/x"))
        assert ledger.state()["used"] == 0


class TestTransports:
    def test_embeddings_through_httpx_are_counted_and_classified(self, ledger):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})))
        with ul.guards(ledger):
            client.post(EMBED_URL, json={"input": ["a", "b"]}, headers={"Authorization": "Bearer sk-SECRET-KEY"})
        assert ledger.summary()["by_kind"] == {"embedding": 1}

    def test_a_server_error_is_recorded_and_a_repeat_is_an_independently_counted_retry(self, ledger):
        answers = iter([httpx.Response(500), httpx.Response(200, json={})])
        client = httpx.Client(transport=httpx.MockTransport(lambda r: next(answers)))
        with ul.guards(ledger):
            client.post(EMBED_URL, json={"input": ["same"]})  # the SDK's internal retry sends the same body again
            client.post(EMBED_URL, json={"input": ["same"]})
        summary = ledger.summary()
        assert summary["requests"]["reserved_locally"] == 2 and summary["requests"]["error"] == 1
        assert summary["requests"]["ok"] == 1 and summary["retries_counted_independently"] == 1
        assert ledger.state()["used"] == 2

    def test_the_hooks_are_removed_afterwards(self, ledger, fake_urlopen):
        before_send, before_open = httpx.Client.send, urllib.request.urlopen
        with ul.guards(ledger):
            assert httpx.Client.send is not before_send and urllib.request.urlopen is not before_open
        assert httpx.Client.send is before_send and urllib.request.urlopen is before_open

    def test_nothing_sensitive_is_written(self, ledger, fake_urlopen):
        with ul.guards(ledger):
            urllib.request.urlopen(chat_request(b'{"input":"PRIVATE CUSTOMER TEXT"}'))
        text = ledger.path.read_text(encoding="utf-8")
        assert "sk-SECRET-KEY" not in text and "PRIVATE CUSTOMER TEXT" not in text and "Authorization" not in text


class TestBudget:
    def test_the_request_over_the_ceiling_is_refused_before_anything_is_sent(self, tmp_path, fake_urlopen):
        led = ul.Ledger(tmp_path / "u.jsonl", run_id="r")
        led.init(2, "tiny", prior_usage="none")
        with ul.guards(led):
            urllib.request.urlopen(chat_request())
            urllib.request.urlopen(chat_request())
            with pytest.raises(ul.BudgetExhausted):
                urllib.request.urlopen(chat_request())
        assert len(fake_urlopen) == 2
        assert led.summary()["budget"]["remaining"] == 0

    def test_the_budget_is_cumulative_across_runs_and_restarts(self, tmp_path, fake_urlopen):
        path = tmp_path / "u.jsonl"
        ul.Ledger(path, run_id="a").init(3, "x", prior_usage="none")
        with ul.guards(ul.Ledger(path, run_id="a")):  # a new Ledger object per run: no in-process state is shared
            urllib.request.urlopen(chat_request())
            urllib.request.urlopen(chat_request())
        with ul.guards(ul.Ledger(path, run_id="b")):
            urllib.request.urlopen(chat_request())  # the third and last request the ceiling allows
            with pytest.raises(ul.BudgetExhausted):
                urllib.request.urlopen(chat_request())
        assert ul.Ledger(path).state()["used"] == 3 and len(fake_urlopen) == 3

    def test_the_per_run_summary_excludes_other_runs(self, tmp_path, fake_urlopen):
        path = tmp_path / "u.jsonl"
        ul.Ledger(path).init(10, "x", prior_usage="none")
        for run_id, n in (("a", 2), ("b", 3)):
            with ul.guards(ul.Ledger(path, run_id=run_id)):
                for i in range(n):
                    urllib.request.urlopen(chat_request(str(i).encode()))
        assert ul.Ledger(path, run_id="b").summary(run_only=True)["requests"]["reserved_locally"] == 3
        assert ul.Ledger(path).summary()["budget"]["used_total"] == 5


class TestFailClosed:
    def test_a_missing_ledger_refuses_before_any_request(self, tmp_path, fake_urlopen):
        with pytest.raises(ul.LedgerError):
            with ul.guards(ul.Ledger(tmp_path / "missing.jsonl")):
                urllib.request.urlopen(chat_request())  # pragma: no cover
        assert fake_urlopen == []

    def test_a_corrupt_line_refuses(self, ledger, fake_urlopen):
        with open(ledger.path, "ab") as handle:
            handle.write(b"{not json}\n" + json.dumps({"event": "ceiling", "ceiling": 5}).encode() + b"\n")
        with pytest.raises(ul.LedgerError):
            with ul.guards(ledger):
                urllib.request.urlopen(chat_request())  # pragma: no cover
        assert fake_urlopen == []

    def test_a_torn_final_line_is_ignored_reported_and_repaired(self, ledger, fake_urlopen):
        good = ledger.path.read_bytes()
        with open(ledger.path, "ab") as handle:
            handle.write(b'{"event": "reserve", "req_id": "dead')  # crash mid-write: never sent
        with ul.guards(ledger):
            urllib.request.urlopen(chat_request())
        assert ledger.path.read_bytes().startswith(good)
        assert ledger.summary()["requests"]["reserved_locally"] == 1  # the torn entry is not counted, the rest intact
        assert b"dead" in ledger.path.with_suffix(".jsonl.torn").read_bytes()  # kept for audit, not silently dropped

    def test_no_ceiling_refuses(self, tmp_path, fake_urlopen):
        path = tmp_path / "u.jsonl"
        path.write_text(json.dumps({"event": "unknown_prior", "id": "x", "low": 0, "high": 1, "note": ""}) + "\n")
        with pytest.raises(ul.LedgerError):
            with ul.guards(ul.Ledger(path)):
                urllib.request.urlopen(chat_request())  # pragma: no cover

    def test_a_held_lock_refuses_instead_of_waiting_forever(self, ledger, fake_urlopen):
        with ul._locked(ledger.path, 2):
            other = ul.Ledger(ledger.path, lock_timeout=0.2)
            with pytest.raises(ul.LedgerError):
                other.state()

    def test_init_will_not_overwrite_an_existing_ledger(self, ledger):
        with pytest.raises(ul.LedgerError):
            ledger.init(1000, "reset the budget", prior_usage="none")


WORKER = """
import sys, urllib.request
sys.path.insert(0, {eval_dir!r})
import usage_ledger as ul
led = ul.Ledger({path!r}, run_id=sys.argv[1])
done = refused = 0
for i in range(10):
    try:
        rid = led.reserve("chat", "api.example.test", "/v1/responses", "POST", "fp%s" % i)
        led.mark_sent(rid); led.finish(rid, "ok", status=200); done += 1
    except ul.BudgetExhausted:
        refused += 1
print(done, refused)
"""


class TestConcurrency:
    def test_four_processes_share_one_ceiling_exactly(self, tmp_path):
        path = tmp_path / "shared.jsonl"
        ul.Ledger(path).init(25, "concurrency test", prior_usage="none")
        code = WORKER.format(eval_dir=str(EVAL_DIR), path=str(path))
        procs = [subprocess.Popen([sys.executable, "-c", code, f"w{i}"], stdout=subprocess.PIPE, text=True)
                 for i in range(4)]
        done = sum(int(p.communicate()[0].split()[0]) for p in procs)
        assert done == 25  # 40 attempted, exactly the ceiling granted, none lost, none double counted
        state = ul.Ledger(path).state()
        assert state["used"] == 25 and len({r["req_id"] for r in state["local"]}) == 25
        assert ul.Ledger(path).summary()["requests"]["ok"] == 25


class TestRunnerWiring:
    def test_a_live_run_without_a_ledger_is_refused(self, monkeypatch, capsys):
        monkeypatch.delenv(ul.ENV_PATH, raising=False)
        with pytest.raises(SystemExit):
            ev.main(["--llm", "env", "--allow-live"])
        assert "usage ledger" in capsys.readouterr().err

    def test_a_live_run_with_a_missing_ledger_makes_no_request(self, monkeypatch, tmp_path, fake_urlopen, capsys):
        with pytest.raises(SystemExit):
            ev.main(["--llm", "env", "--allow-live", "--usage-ledger", str(tmp_path / "nope.jsonl")])
        assert fake_urlopen == []

    def test_reaching_the_ceiling_stops_the_run_with_code_4_and_a_stub_report(self, monkeypatch, tmp_path, fake_urlopen):
        led = ul.Ledger(tmp_path / "u.jsonl")
        led.init(1, "one request only", prior_usage="none")

        class Probe:
            name = "probe"
            interprets_language = False

            def chat(self, system, messages, tools, *, max_output_tokens):
                urllib.request.urlopen(chat_request())
                raise ev.LLMError("probe failure", category="server_error")

            complete = chat

        monkeypatch.setattr(ev, "_factory", lambda mode, allow_live: (lambda: Probe()))
        out = tmp_path / "report.json"
        code = ev.main(["--llm", "env", "--allow-live", "--usage-ledger", str(led.path), "--run-id", "t",
                        "--output", str(out)])
        assert code == 4 and len(fake_urlopen) == 1
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["integrity"]["run_valid_for_scoring"] is False
        assert report["usage_ledger"]["cumulative"]["budget"]["remaining"] == 0

    def test_the_attempt_runner_never_retries_into_a_spent_budget(self, tmp_path):
        calls = []

        def run(cmd):
            calls.append(cmd)
            Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps({"integrity": {}}), encoding="utf-8")
            return 4

        summary = run_valid.run_until_valid("x", tmp_path / "o", [], runner=["real_eval"], max_attempts=3,
                                            check=lambda: (True, "ok"), sleep=lambda s: None, run=run)
        assert summary["status"] == "budget" and len(calls) == 1
