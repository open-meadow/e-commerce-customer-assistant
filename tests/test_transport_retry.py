"""Harness-only transport retry: only provably pre-transmission failures are re-sent. Offline: urlopen is always a fake."""

import errno
import http.client
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import real_eval as ev  # noqa: E402
import transport_retry as tr  # noqa: E402
import usage_ledger as ul  # noqa: E402
from app.llm import providers  # noqa: E402

URL = "https://api.example.test/v1/responses"
DNS = urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
REFUSED = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
UNREACHABLE = urllib.error.URLError(OSError(errno.ENETUNREACH, "network unreachable"))


class Response:
    status = 200

    def __init__(self, body=b'{"ok": true}'):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def request(payload=b'{"input": "hello"}'):
    return urllib.request.Request(URL, data=payload, method="POST", headers={"Authorization": "Bearer sk-not-real"})


class Script:
    """A fake urlopen that plays a list of outcomes (exceptions are raised, anything else returned) and records calls."""

    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, req, *args, **kwargs):
        self.calls.append((req, getattr(req, "data", None)))
        outcome = self.outcomes.pop(0) if self.outcomes else Response()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def fake(monkeypatch):
    def install(*outcomes):
        script = Script(outcomes)
        monkeypatch.setattr(urllib.request, "urlopen", script)
        return script
    return install


@pytest.fixture
def waits():
    return {"slept": [], "probes": []}


def retrying(waits, max_retries=2, probe_result=False):
    return tr.retrying(max_retries, sleep=waits["slept"].append,
                       probe=lambda host: waits["probes"].append(host) or probe_result)


@pytest.fixture
def ledger(tmp_path):
    led = ul.Ledger(tmp_path / "u.jsonl", run_id="r", lock_timeout=2)
    led.init(100, "t", prior_usage="none")
    return led


def reserves(led):
    return [json.loads(l) for l in led.path.read_text(encoding="utf-8").splitlines() if '"event": "reserve"' in l]


class TestWhatIsRetried:
    @pytest.mark.parametrize("failure,reason", [(DNS, "dns"), (REFUSED, "connection_refused"),
                                                (UNREACHABLE, "network_unreachable")])
    def test_a_failure_before_transmission_is_retried_and_can_recover(self, fake, waits, failure, reason):
        script = fake(failure, Response())
        with retrying(waits) as collector:
            result = urllib.request.urlopen(request())
        assert result.status == 200 and len(script.calls) == 2
        assert collector.recovered == [{"logical_id": collector.recovered[0]["logical_id"], "attempts": 2,
                                        "reasons": [reason]}]

    @pytest.mark.parametrize("failure", [
        urllib.error.URLError(TimeoutError("timed out")),            # a connect or send timeout: ambiguous
        TimeoutError("read timed out"),                               # raised while reading the response
        urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, "reset")),
        ConnectionResetError(errno.ECONNRESET, "reset"),
        http.client.RemoteDisconnected("closed without a response"),
        urllib.error.HTTPError(URL, 500, "server error", {}, None),
        urllib.error.HTTPError(URL, 429, "rate limited", {}, None),
        urllib.error.URLError(OSError(errno.EPIPE, "broken pipe")),   # during send: it may have been transmitted
    ])
    def test_an_ambiguous_or_answered_failure_is_never_resent(self, fake, waits, failure):
        script = fake(failure, Response())
        with retrying(waits) as collector:
            with pytest.raises(type(failure)):
                urllib.request.urlopen(request())
        assert len(script.calls) == 1 and waits["slept"] == []
        assert sum(collector.not_retried.values()) == 1 and collector.recovered == [] and collector.exhausted == []

    def test_a_success_is_untouched(self, fake, waits):
        script = fake(Response())
        with retrying(waits) as collector:
            urllib.request.urlopen(request())
        assert len(script.calls) == 1 and collector.summary()["retry_attempts"] == 0


class TestBounds:
    def test_at_most_two_retries_then_the_failure_is_raised_unchanged(self, fake, waits):
        script = fake(DNS, DNS, DNS, Response())
        with retrying(waits) as collector:
            with pytest.raises(urllib.error.URLError) as caught:
                urllib.request.urlopen(request())
        assert caught.value is DNS and len(script.calls) == 3  # one attempt and two retries, never a fourth
        assert collector.exhausted[0]["attempts"] == 3 and collector.exhausted[0]["reasons"] == ["dns"] * 3

    def test_waits_are_bounded_and_end_early_when_the_host_resolves(self, fake, waits):
        fake(DNS, DNS, Response())
        with retrying(waits, probe_result=False):
            urllib.request.urlopen(request())
        assert len(waits["slept"]) == 5 + 20 and waits["probes"][0] == "api.example.test"  # never beyond 5 s then 20 s
        waits2 = {"slept": [], "probes": []}
        fake(DNS, DNS, Response())
        with retrying(waits2, probe_result=True):
            urllib.request.urlopen(request())
        assert waits2["slept"] == [1.0, 1.0]  # the host resolved straight away: no further waiting

    def test_the_retry_limit_cannot_be_raised_above_two(self):
        with pytest.raises(ValueError):
            with tr.retrying(3):
                pass  # pragma: no cover
        with pytest.raises(ValueError):
            with tr.retrying(-1):
                pass  # pragma: no cover

    def test_the_hook_is_removed_afterwards(self, fake, waits):
        fake()
        before = urllib.request.urlopen
        with retrying(waits):
            assert urllib.request.urlopen is not before
        assert urllib.request.urlopen is before


class TestSamePayload:
    def test_every_attempt_sends_the_same_request_object_and_bytes(self, fake, waits):
        script = fake(DNS, REFUSED, Response())
        original = request(b'{"input": "ORD1005 wala return"}')
        with retrying(waits):
            urllib.request.urlopen(original)
        assert [c[0] for c in script.calls] == [original] * 3
        assert {c[1] for c in script.calls} == {b'{"input": "ORD1005 wala return"}'}


class TestLedgerAndReports:
    def test_each_attempt_is_its_own_ledger_entry_linked_by_one_logical_id(self, ledger, fake, waits):
        script = fake(DNS, DNS, Response())
        with ul.guards(ledger):
            with retrying(waits):  # installed after the guards: each attempt passes through the ledger
                urllib.request.urlopen(request())
        entries = reserves(ledger)
        assert len(entries) == 3 and len(script.calls) == 3
        assert [e["attempt"] for e in entries] == [1, 2, 3]
        assert len({e["logical_id"] for e in entries}) == 1 and len({e["fingerprint"] for e in entries}) == 1
        assert [e["retry_reason"] for e in entries] == [None, "dns", "dns"]
        summary = ledger.summary()
        assert summary["requests"]["reserved_locally"] == 3 and summary["requests"]["error"] == 2
        assert summary["requests"]["ok"] == 1 and summary["harness_retry_attempts"] == 2
        assert ledger.state()["used"] == 3  # a retry costs budget like any other request

    def test_a_ledger_ceiling_stops_a_retry_before_it_is_sent(self, tmp_path, fake, waits):
        led = ul.Ledger(tmp_path / "small.jsonl", run_id="r")
        led.init(2, "t", prior_usage="none")
        script = fake(DNS, DNS, Response())
        with ul.guards(led):
            with retrying(waits):
                with pytest.raises(ul.BudgetExhausted):
                    urllib.request.urlopen(request())
        assert len(script.calls) == 2

    def test_an_exhausted_request_makes_the_report_invalid_and_is_reported_apart_from_chatbot_results(self, fake, waits):
        fake(DNS, DNS, DNS)
        with retrying(waits) as collector:
            with pytest.raises(urllib.error.URLError):
                urllib.request.urlopen(request())
        report = {"integrity": {"run_valid_for_scoring": True}, "results": ["chatbot"]}
        tr.annotate(report, collector)
        assert report["integrity"]["run_valid_for_scoring"] is False
        assert report["integrity"]["transport_retries_exhausted"] == 1
        assert report["transport_retries"]["exhausted"] and report["results"] == ["chatbot"]

    def test_a_recovered_retry_leaves_the_run_valid_and_is_still_recorded(self, fake, waits):
        fake(DNS, Response())
        with retrying(waits) as collector:
            urllib.request.urlopen(request())
        report = {"integrity": {"run_valid_for_scoring": True}}
        tr.annotate(report, collector)
        assert report["integrity"]["run_valid_for_scoring"] is True
        assert len(report["transport_retries"]["recovered"]) == 1 and report["transport_retries"]["retry_attempts"] == 1


class TestProductCodeIsUntouched:
    def test_the_product_client_sees_a_normal_reply_after_a_recovered_retry(self, fake, waits):
        fake(DNS, Response(b'{"output": []}'))
        with retrying(waits):
            data = providers._post_json(URL, {}, {"x": 1}, 5.0, provider="openai", model="m")
        assert data == {"output": []}

    def test_the_product_client_still_reports_the_failure_when_retries_are_exhausted(self, fake, waits):
        fake(DNS, DNS, DNS)
        with retrying(waits):
            with pytest.raises(ev.LLMError) as caught:
                providers._post_json(URL, {}, {"x": 1}, 5.0, provider="openai", model="m")
        assert caught.value.category == "dns"

    def test_without_the_flag_nothing_is_patched(self, tmp_path, fake, monkeypatch):
        script = fake(*[DNS] * 10)
        led = ul.Ledger(tmp_path / "u.jsonl")
        led.init(10, "t", prior_usage="none")

        class Probe:
            name, interprets_language = "probe", False

            def chat(self, *a, **k):
                providers._post_json(URL, {}, {"x": 1}, 5.0, provider="openai", model="m")

            complete = chat

        monkeypatch.setattr(ev, "_factory", lambda mode, allow_live: (lambda: Probe()))
        out = tmp_path / "r.json"
        ev.main(["--llm", "env", "--allow-live", "--usage-ledger", str(led.path), "--stop-after-provider-failures", "3",
                 "--output", str(out)])
        assert len(script.calls) == 3  # three logical requests, one attempt each: no retry without --transport-retries

    def test_the_command_line_retries_and_marks_the_segment_invalid_when_exhausted(self, tmp_path, fake, monkeypatch):
        script = fake(*[DNS] * 20)
        monkeypatch.setattr(tr.time, "sleep", lambda s: None)
        monkeypatch.setattr(tr, "_resolves", lambda host: False)
        led = ul.Ledger(tmp_path / "u.jsonl")
        led.init(100, "t", prior_usage="none")

        class Probe:
            name, interprets_language = "probe", False

            def chat(self, *a, **k):
                providers._post_json(URL, {}, {"x": 1}, 5.0, provider="openai", model="m")

            complete = chat

        monkeypatch.setattr(ev, "_factory", lambda mode, allow_live: (lambda: Probe()))
        monkeypatch.setattr(tr, "DEFAULT_DELAYS", (1.0, 1.0))
        out = tmp_path / "r.json"
        code = ev.main(["--llm", "env", "--allow-live", "--usage-ledger", str(led.path), "--transport-retries", "2",
                        "--stop-after-provider-failures", "3", "--run-id", "seg", "--output", str(out)])
        stub = json.loads(out.read_text(encoding="utf-8"))
        assert code == 3 and stub["integrity"]["run_valid_for_scoring"] is False
        assert len(script.calls) == 9  # three logical requests, each tried three times
        assert len(stub["transport_retries"]["exhausted"]) == 3
        assert led.summary()["requests"]["reserved_locally"] == 9 and led.summary()["harness_retry_attempts"] == 6
