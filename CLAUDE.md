# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository. Rules and workflow are in [AGENTS.md](AGENTS.md) (read it first);
product overview in [README.md](README.md); design in [docs/architecture.md](docs/architecture.md); current state in
[docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md). This file covers commands, layout and conventions only.

## Commands

A **uv** project on Python 3.13. Do not use pip or create a separate venv. Run everything from the repository root and use
`python -m`, so the root is on `sys.path` and relative `APP_DB_PATH` values resolve.

```bash
uv sync
```

```bash
uv run python -m pytest tests -q
```

```bash
uv run python -m pytest tests/test_returns.py::TestClassName::test_name -q
```

```bash
uv run python -m streamlit run app/ui/streamlit_app.py
```

```bash
uv run uvicorn app.backend.http_api:app --host 127.0.0.1 --port 8000
```

```bash
docker compose up --build
```

Create a demo database (the command refuses existing files and `app/db/data/app.db`; the parent directory must exist). New databases are
schema version 2; a version-1 file is refused by every runtime connection until it is migrated (scratch copies only, see Conventions) or re-seeded:

```bash
uv run python -m app.db.seed_data --db-path demo/demo.db --reference-time 2026-11-07T00:00:00Z
```

Settings come from a git-ignored `.env` (template: `.env.example`); real environment variables override it. There is no linter,
formatter or coverage tool. The latest recorded full run is 4,314 passed, 0 failed, 0 skipped (recorded runs took 6 to 14 minutes) and needs no
network.

## Layout

```
app/ui            Streamlit chat; client.py picks the backend (UI_BACKEND=real default, fake for a canned demo)
app/backend       public API a front end imports (Backend, sessions, HTTP API, limits.py: rate limits and provider budget)
app/orchestrator  engine.py (tool loop, every state change), guard.py, issues.py, summarizer.py, ticket_flow.py (fresh-description
                  rules), ticket_writer.py (writer interface and outcomes)
app/llm           OpenAI Responses, Anthropic, offline RuleBasedLLM stand-in
app/tools         registry.py (10 tools), flow_texts.py (150 sentences x 3 languages), return_texts.py
app/rag           Chroma vector index over FAQ.md and Policies.md
app/db            SQLite layer, repository (authorization boundary), seed, source data and eval data; migrations.py and migrate.py
                  (operator-run, scratch-only schema migration), ticket_writer.py (the real ticket writer)
app/voice         LiveKit worker (never run live)
scripts/eval      real-backend evaluation harness, usage ledger, transport retry, attempt composition
tests/            offline suite; tests/ui for the UI helpers
docs/             see docs/README.md
```

`src/ecommerce_support_assistant/` is an unused uv packaging stub that `pyproject.toml` builds; leave it (removing it needs a
build-config change).

## Conventions

- Four connection factories in `app/db/connection.py`; never call `sqlite3.connect()` elsewhere. `get_connection` never creates a
  file; only `get_initialization_connection` does, and it refuses `app/db/data/app.db`. `get_migration_connection` exists only for
  `app/db/migrations.py` (it also refuses `app_d2.db`); `get_readonly_connection` serves health checks.
- `SCHEMA_VERSION` is 2 and every runtime connection requires exactly that (`PRAGMA user_version`); a version-1 file is `outdated`
  and refused, with the fresh-ticket gate ON or OFF. Nothing migrates at startup. The migration (`python -m app.db.migrate`, needs
  `--scratch`) has been run on scratch databases only; never run it on `app/db/data/app.db` or `app_d2.db`.
- The fresh-description ticket flow is behind `SUPPORT_TICKET_FRESH_FLOW_ENABLED` (default `false`). With it ON, `Backend` accepts only
  `RepositoryTicketWriter` and fails startup closed otherwise; tests inject the fake writer in `tests/ticket_writer_fakes.py` directly
  into an `Engine`. Creation is idempotent per request key, duplicate detection is a separate outcome, the rate limit is counted in the
  creation transaction, and an uncertain outcome (`outcome_unknown`) keeps the request and blocks another creation until reconciled.
- Every repository function takes `authenticated_customer_id` and filters on it in SQL. Writes use `BEGIN IMMEDIATE` and recheck
  eligibility; failures raise typed `DatabaseError` subclasses.
- Money is integer paise; timestamps are TEXT `YYYY-MM-DDTHH:MM:SSZ` (UTC); `orders.status` values come from `FAQ.md` Q1.4.
- Text is validated exactly and never modified: return reasons up to 1,000 characters, ticket text up to 2,000.
- Fixed customer-facing sentences live only in `app/tools/flow_texts.py`, in all three languages. After changing one run
  `uv run python scripts/localization_review.py`; a test fails if the committed review sheets are stale. English is pinned by
  tests; Hinglish and Hindi stay `draft` until a fluent reviewer changes `REVIEW_STATUS`. The English `approved` status does not approve
  every sentence: `ticket_duplicate_active`, `ticket_idempotency_conflict`, `ticket_recovery_unknown` and `ticket_recovered_created`
  are proposals pending owner approval.
- `SYSTEM_PROMPT` (in `engine.py`) is pinned by tests (exact phrases, under 7,000 characters). A prompt change needs a small live
  check, because offline tests cannot show whether the model follows it.
- Adding a tool: register it in `registry.py` with a schema that contains no customer ID, scope its repository call to the
  session's customer and add an authorization test.
- Shell scripts (`*.sh`) must stay LF (`.gitattributes`); CRLF breaks the `api` container.
- Retrieval needs `OPENAI_API_KEY` even with `LLM_PROVIDER` unset; there is no silent BM25 fallback. `support@example.com` in the
  source documents is a placeholder and must not be presented as a real channel.

## Tests

Shared fixtures are in `tests/conftest.py`: an autouse guard fails any test that opens `app/db/data/app.db` or `app_d2.db`;
`temp_db` builds a seeded temporary database; `set_now` and `bnow` control the clocks. Backend tests use
`tests/backend_helpers.py` (`Client`, `ScriptedLLM`). Adapter tests mock `urllib.request.urlopen` and are not live-provider
verification. Follow these patterns for new tests.

## Evaluation

`scripts/eval/real_eval.py` runs the real backend against a model on a temporary database and scores replies with mechanical
checks; manual reading is the authority. **Live runs need explicit authorization** and must go through the usage ledger
(`scripts/eval/usage_ledger.py`). `scripts/eval/run_eval.py` uses the fake backend and measures nothing. The procedure is in
[docs/PROJECT_HANDOVER.md](docs/PROJECT_HANDOVER.md) section 8; results are in
[docs/evaluation/EVALUATION_REPORT.md](docs/evaluation/EVALUATION_REPORT.md).

The previous, longer version of this file is kept in [docs/history/](docs/history/CLAUDE_before_corrective_v2.md).
