"""Ledger: SDK transport coverage, UTC reconciliation, double counting and the historical baseline. Offline only."""

import base64
import json
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
import usage_ledger as ul  # noqa: E402

httpx = pytest.importorskip("httpx")
httpx2 = pytest.importorskip("httpx2")
openai = pytest.importorskip("openai")

T0 = "2026-11-17T07:09:00Z"
PRIOR_WINDOW = ("2026-11-17T07:00:00Z", "2026-11-17T11:30:00Z")


def make(tmp_path, ceiling=150, prior_usage="pending", name="u.jsonl"):
    led = ul.Ledger(tmp_path / name, run_id="t", lock_timeout=2)
    led.init(ceiling, "test", prior_usage=prior_usage)
    return led


def now_window(hours=1):
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=hours), now + timedelta(hours=hours)


def iso(moment, tz=timezone.utc):
    return moment.astimezone(tz).isoformat()


def one_request(led):
    led.mark_sent(rid := led.reserve("chat", "api.example.test", "/v1/responses", "POST", "fp"))
    led.finish(rid, "ok", status=200)
    return rid


# ---------------------------------------------------------------------------------------------------------------------
# 2. SDK transport behaviour
# ---------------------------------------------------------------------------------------------------------------------

def embedding_body(request):
    wants_float = b'"encoding_format":"float"' in request.content.replace(b" ", b"")
    vector = [0.25, 0.5]
    value = vector if wants_float else base64.b64encode(struct.pack("<2f", *vector)).decode()
    return {"object": "list", "model": "text-embedding-3-small", "usage": {"prompt_tokens": 1, "total_tokens": 1},
            "data": [{"object": "embedding", "index": 0, "embedding": value}]}


@pytest.fixture
def sdk_transport(monkeypatch):
    """Replace the SDK's httpx2 ``send`` with a fake that answers from a script: no socket is ever opened."""
    script, seen = [], []

    def fake(self, request, *args, **kwargs):
        seen.append(request)
        status = script.pop(0) if script else 200
        body = embedding_body(request) if status == 200 else {"error": {"message": "boom", "type": "server_error"}}
        return httpx2.Response(status, json=body, request=request)

    monkeypatch.setattr(httpx2.Client, "send", fake)
    monkeypatch.setattr("openai._base_client.time.sleep", lambda seconds: None)  # no real backoff wait
    return script, seen


def sdk_client(max_retries=2):
    return openai.OpenAI(api_key="sk-test-not-real", base_url="https://api.example.test/v1", max_retries=max_retries)


class TestSdkTransport:
    def test_the_sdk_does_not_use_httpx_so_a_hook_on_httpx_alone_would_miss_it(self):
        assert httpx2.Client is not httpx.Client and not issubclass(httpx2.Client, httpx.Client)
        assert sdk_client()._client.__class__.__mro__[2] is httpx2.Client  # the SDK client IS an httpx2.Client

    def test_the_guard_hooks_both_client_libraries(self, tmp_path):
        led = make(tmp_path, prior_usage="none")
        with ul.guards(led):
            assert {"httpx.Client.send", "httpx2.Client.send", "urllib.request.urlopen"} <= set(led.hooked)

    def test_a_real_sdk_embedding_request_is_counted(self, tmp_path, sdk_transport):
        led = make(tmp_path, prior_usage="none")
        with ul.guards(led):
            sdk_client().embeddings.create(model="text-embedding-3-small", input="hello", encoding_format="float")
        assert led.summary()["by_kind"] == {"embedding": 1} and led.summary()["requests"]["ok"] == 1

    def test_the_sdks_internal_retries_are_each_counted_independently(self, tmp_path, sdk_transport):
        script, seen = sdk_transport
        script.extend([500, 500, 200])
        led = make(tmp_path, prior_usage="none")
        with ul.guards(led):
            sdk_client(max_retries=2).embeddings.create(model="text-embedding-3-small", input="same",
                                                         encoding_format="float")
        assert len(seen) == 3  # the SDK really did send three requests for one call
        summary = led.summary()
        assert summary["requests"]["reserved_locally"] == 3 and summary["requests"]["error"] == 2
        assert summary["requests"]["ok"] == 1 and summary["retries_counted_independently"] == 2

    def test_chromas_openai_embedding_function_is_counted(self, tmp_path, sdk_transport):
        from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
        led = make(tmp_path, prior_usage="none")
        function = OpenAIEmbeddingFunction(api_key="sk-test-not-real", model_name="text-embedding-3-small",
                                           api_base="https://api.example.test/v1")
        with ul.guards(led):
            function(["a policy passage"])
        assert led.summary()["by_kind"] == {"embedding": 1}

    def test_other_installed_http_libraries_are_reported_as_not_counted(self, tmp_path):
        led = make(tmp_path, prior_usage="none")
        with ul.guards(led):
            pass
        assert isinstance(led.unhooked_present, list)
        assert "coverage" in led.summary() and "installed_but_not_counted" in led.summary()["coverage"]


# ---------------------------------------------------------------------------------------------------------------------
# 1. UTC datetime comparison
# ---------------------------------------------------------------------------------------------------------------------

class TestUtcComparison:
    def test_offsets_and_z_name_the_same_instant(self):
        assert ul._utc("2026-11-17T12:39:00+05:30") == ul._utc("2026-11-17T07:09:00Z")

    def test_a_timestamp_without_a_timezone_is_refused_not_assumed(self, tmp_path):
        led = make(tmp_path)
        with pytest.raises(ul.LedgerError):
            ul._utc("2026-11-17T07:09:00")
        with pytest.raises(ul.LedgerError):
            led.import_dashboard(kind="reconcile", requests=1, window_start="2026-11-17T07:00:00",
                                 window_end="2026-11-17T08:00:00Z", model="m", source="s")
        with pytest.raises(ul.LedgerError):
            led.record_unknown_prior("x", basis="estimated", low=1, high=2, window_start="2026-11-17 07:00",
                                     window_end="2026-11-17T08:00:00Z", note="n")

    def test_a_window_must_end_after_it_starts(self, tmp_path):
        with pytest.raises(ul.LedgerError):
            make(tmp_path).import_dashboard(kind="reconcile", requests=1, window_start="2026-11-17T08:00:00Z",
                                            window_end="2026-11-17T07:00:00Z", model="m", source="s")

    def test_reconciliation_counts_by_instant_across_offsets_and_sub_second_boundaries(self, tmp_path):
        led = make(tmp_path, prior_usage="none")
        rid = one_request(led)
        stamp = ul._utc(next(r["ts"] for r in led.state()["local"] if r["req_id"] == rid))
        ist = timezone(timedelta(hours=5, minutes=30))
        led.import_dashboard(kind="reconcile", requests=9, window_start=iso(stamp - timedelta(minutes=5), ist),
                             window_end=iso(stamp + timedelta(minutes=5), ist), model="m", source="s")
        # a window that starts one microsecond AFTER the request: a string comparison could not tell, datetimes can
        led.import_dashboard(kind="reconcile", requests=9, window_start=iso(stamp + timedelta(microseconds=1)),
                             window_end=iso(stamp + timedelta(hours=1)), model="m", source="s")
        rows = led.summary()["dashboard_reconciliation"]
        assert [r["ledger_requests_in_window"] for r in rows] == [1, 0]
        assert led.summary()["budget"]["used_total"] == 1  # a reconcile import is never added


# ---------------------------------------------------------------------------------------------------------------------
# 3. Double counting
# ---------------------------------------------------------------------------------------------------------------------

def prior(led, entry_id="p1", basis="estimated", low=10, high=20, window=PRIOR_WINDOW):
    led.record_unknown_prior(entry_id, basis=basis, low=low, high=high, window_start=window[0], window_end=window[1],
                             note="n")


def dash(led, requests=15, window=PRIOR_WINDOW, supersedes=(), kind="historical"):
    led.import_dashboard(kind=kind, requests=requests, window_start=window[0], window_end=window[1], model="m",
                         source="dashboard", supersedes=list(supersedes))


class TestDoubleCounting:
    def test_a_historical_import_covering_requests_the_ledger_recorded_is_refused(self, tmp_path):
        led = make(tmp_path, prior_usage="none")
        one_request(led)
        start, end = now_window()
        with pytest.raises(ul.LedgerError, match="reconcile"):
            dash(led, window=(iso(start), iso(end)))

    def test_a_prior_entry_whose_window_contains_recorded_requests_is_refused(self, tmp_path):
        led = make(tmp_path, prior_usage="none")
        one_request(led)
        start, end = now_window()
        with pytest.raises(ul.LedgerError, match="counted twice"):
            prior(led, window=(iso(start), iso(end)))

    def test_an_import_overlapping_an_unsuperseded_entry_must_supersede_it(self, tmp_path):
        led = make(tmp_path)
        prior(led)
        with pytest.raises(ul.LedgerError, match="overlaps entry"):
            dash(led)
        dash(led, supersedes=["p1"])
        assert led.state()["used"] == 15  # the estimate was replaced, not added to

    def test_a_partial_replacement_is_refused(self, tmp_path):
        led = make(tmp_path)
        prior(led)
        with pytest.raises(ul.LedgerError, match="does not cover"):
            dash(led, window=("2026-11-17T08:00:00Z", "2026-11-17T09:00:00Z"), supersedes=["p1"])

    def test_two_historical_imports_may_not_overlap(self, tmp_path):
        led = make(tmp_path, prior_usage="none")
        dash(led, window=("2026-11-17T00:00:00Z", "2026-11-17T06:00:00Z"))
        with pytest.raises(ul.LedgerError, match="earlier historical import"):
            dash(led, window=("2026-11-17T05:00:00Z", "2026-11-17T07:00:00Z"))
        dash(led, window=("2026-11-17T06:00:00Z", "2026-11-17T07:00:00Z"))  # touching is not overlapping

    def test_an_entry_can_be_superseded_only_once(self, tmp_path):
        led = make(tmp_path)
        prior(led)
        dash(led, supersedes=["p1"])
        with pytest.raises(ul.LedgerError):
            dash(led, window=("2026-11-18T00:00:00Z", "2026-11-18T01:00:00Z"), supersedes=["p1"])

    def test_prior_entries_may_not_overlap_each_other(self, tmp_path):
        led = make(tmp_path)
        prior(led, "a", window=("2026-11-17T07:00:00Z", "2026-11-17T09:00:00Z"))
        with pytest.raises(ul.LedgerError, match="overlaps entry"):
            prior(led, "b", window=("2026-11-17T08:00:00Z", "2026-11-17T10:00:00Z"))
        prior(led, "b", window=("2026-11-17T09:00:00Z", "2026-11-17T10:00:00Z"))

    def test_one_import_can_supersede_several_entries(self, tmp_path):
        led = make(tmp_path)
        prior(led, "a", basis="client_recorded", low=12, high=12, window=("2026-11-17T07:00:00Z", "2026-11-17T07:15:00Z"))
        prior(led, "b", low=62, high=144, window=("2026-11-17T07:15:00Z", "2026-11-17T11:30:00Z"))
        dash(led, requests=80, supersedes=["a", "b"])
        assert led.state()["used"] == 80 and led.summary()["baseline"]["superseded_entries"].keys() == {"a", "b"}


# ---------------------------------------------------------------------------------------------------------------------
# 4 and 5. The historical baseline
# ---------------------------------------------------------------------------------------------------------------------

class TestBaseline:
    def test_the_prior_usage_statement_is_required_and_validated(self, tmp_path):
        with pytest.raises(TypeError):
            ul.Ledger(tmp_path / "a.jsonl").init(10, "no statement")
        with pytest.raises(ul.LedgerError):
            ul.Ledger(tmp_path / "b.jsonl").init(10, "bad", prior_usage="zero")

    def test_a_pending_baseline_blocks_every_request_and_the_run_itself(self, tmp_path):
        led = make(tmp_path, prior_usage="pending")
        sent = []
        with pytest.raises(ul.LedgerError, match="baseline not established"):
            with ul.guards(led):
                sent.append(1)  # pragma: no cover
        with pytest.raises(ul.LedgerError):
            led.reserve("chat", "h", "/p", "POST", "fp")
        assert sent == [] and led.summary()["budget"]["blocked"] is True

    def test_a_blocked_baseline_reports_no_remaining_figure_rather_than_a_misleading_one(self, tmp_path):
        budget = make(tmp_path).summary()["budget"]
        assert budget["used_total"] is None and budget["remaining"] is None
        assert budget["remaining_if_estimates_were_at_their_lower_bound"] is None

    def test_a_ledger_with_a_ceiling_but_no_baseline_statement_is_blocked(self, tmp_path):
        path = tmp_path / "hand.jsonl"
        path.write_text(json.dumps({"event": "ceiling", "ceiling": 100}) + "\n")
        led = ul.Ledger(path)
        with pytest.raises(ul.LedgerError, match="no baseline statement"):
            led.reserve("chat", "h", "/p", "POST", "fp")

    def test_an_unresolved_entry_blocks_until_a_provider_figure_replaces_it(self, tmp_path):
        led = make(tmp_path, prior_usage="none")
        prior(led, "u", basis="unresolved", low=None, high=None)
        with pytest.raises(ul.LedgerError, match="unresolved prior usage"):
            led.reserve("chat", "h", "/p", "POST", "fp")
        dash(led, requests=40, supersedes=["u"])
        assert led.reserve("chat", "h", "/p", "POST", "fp")

    def test_declaring_none_is_refused_when_prior_usage_is_recorded_and_declared_when_nothing_is(self, tmp_path):
        led = make(tmp_path)
        with pytest.raises(ul.LedgerError, match="nothing is recorded"):
            led.declare_baseline("declared", "claims there is a record")
        prior(led)
        with pytest.raises(ul.LedgerError, match="prior usage is recorded"):
            led.declare_baseline("none", "claims nothing was spent")
        led.declare_baseline("declared", "attempts 1-3 recorded as estimates")
        assert led.summary()["budget"]["blocked"] is False

    def test_the_four_kinds_of_baseline_usage_are_reported_separately(self, tmp_path):
        led = make(tmp_path, ceiling=150)
        prior(led, "attempt1", basis="client_recorded", low=12, high=12, window=("2026-11-17T07:09:00Z", "2026-11-17T07:13:00Z"))
        prior(led, "attempts23", basis="estimated", low=62, high=144, window=("2026-11-17T07:17:00Z", "2026-11-17T11:24:00Z"))
        dash(led, requests=7, window=("2026-11-16T00:00:00Z", "2026-11-16T12:00:00Z"))  # an earlier, verified day
        led.declare_baseline("declared", "all earlier usage is listed")
        summary = led.summary()
        baseline, budget = summary["baseline"], summary["budget"]
        assert baseline["provider_verified"]["requests"] == 7
        assert baseline["client_recorded"] == {"requests": 12, "entries": ["attempt1"]}
        assert baseline["estimated"] == {"low": 62, "high": 144, "entries": ["attempts23"]}
        assert baseline["unresolved"]["entries"] == []
        assert budget["used_total"] == 7 + 12 + 144 and budget["used_is_an_upper_bound"] is True
        assert budget["remaining"] == 150 - 163  # over the ceiling: nothing may be sent
        assert budget["remaining_if_estimates_were_at_their_lower_bound"] == 150 - (7 + 12 + 62)
        with pytest.raises(ul.BudgetExhausted):
            led.reserve("chat", "h", "/p", "POST", "fp")

    def test_a_client_recorded_count_must_be_exact_and_an_estimate_must_be_a_range(self, tmp_path):
        led = make(tmp_path)
        with pytest.raises(ul.LedgerError):
            prior(led, basis="client_recorded", low=10, high=12)
        with pytest.raises(ul.LedgerError):
            prior(led, basis="estimated", low=20, high=10)
        with pytest.raises(ul.LedgerError):
            prior(led, basis="unresolved", low=1, high=2)
        with pytest.raises(ul.LedgerError):
            prior(led, basis="guess", low=1, high=2)

    def test_an_event_missing_required_fields_is_corruption_not_a_default(self, tmp_path):
        led = make(tmp_path, prior_usage="none")
        with open(led.path, "ab") as handle:
            handle.write(json.dumps({"event": "unknown_prior", "id": "x"}).encode() + b"\n")
            handle.write(json.dumps({"event": "ceiling", "ceiling": 5}).encode() + b"\n")
        with pytest.raises(ul.LedgerError, match="corrupt"):
            led.state()

    def test_nothing_ever_treats_a_missing_baseline_as_zero_in_the_runner(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))
        import real_eval as ev
        led = make(tmp_path, prior_usage="pending")
        monkeypatch.setattr(ev, "_factory", lambda mode, allow_live: (lambda: pytest.fail("no model may be built")))
        with pytest.raises(SystemExit):
            ev.main(["--llm", "env", "--allow-live", "--usage-ledger", str(led.path)])
