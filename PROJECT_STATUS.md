# Project Status

**Project:** AI E-Commerce Customer Support Assistant
**Deadline:** Approximately 2026-11-13
**Status snapshot:** 2026-11-11
**Current workstream:** B5 documentation and presentation complete, on `rohan/documents`

## Product scope and current behavior

This capstone uses synthetic data only. The runnable Streamlit chat uses
simulated customer selection (C101/C102) and `app/ui/fake_backend.py`, which
returns canned demo replies and supports demo-only return-choice and
confirmation flows. It is not connected to the SQLite repository, a real LLM,
RAG, product lookup, or customer data. A demo confirmation does not submit a
real action.

The SQLite schema, seed, customer-scoped repository, and tests are implemented
separately. Docker files and a safe demo database initialization script are
present. The evaluation set contains 70 synthetic labeled cases; its runner
only exercises the fake backend. Answer quality, tool-call correctness,
hallucination, and safety results are explicitly not assessed.

## Delivery plan

| Workstream | Status |
|---|---|
| B1 — Streamlit UI integration with local fake backend | Complete |
| B2 — Docker setup | Complete |
| B3 — Safe demo database initialization script and tests | Complete |
| B4 — Evaluation cases and demo-only runner | Complete; all evaluation metrics remain unassessed |
| B5-1 — README | Complete |
| B5-2 — Reconcile status and agent guidance | Complete; database checkpoint discrepancy is documented for Technical Lead resolution |
| B5-3 — Architecture diagram | Complete |
| B5-4 — Technical report | Complete |
| B5-5 — Presentation deck | Complete; editable Markdown source and PowerPoint deliverable |
| B5-6 — Documentation consistency review | Complete |

## Database implementation status and discrepancy

`docs/02_database_implementation_plan.md` is canonical for database
requirements and sequencing. Its status says checkpoints 1–6 are complete,
7A (write-function documentation contracts) is approved, and checkpoint 7
(write implementation) plus checkpoints 8–9 are not started and not
authorized.

The current checkout nevertheless contains `create_return_request` and
`create_support_ticket` in `app/db/repository.py`, along with
`tests/test_returns.py` and `tests/test_tickets.py`. This conflicts with the
checkpoint status in `docs/02`. The code and tests are present, but this status
file does not infer that the work was authorized or silently change the
canonical checkpoint record. Resolve the discrepancy with the Technical Lead
before updating `docs/02` or treating those checkpoints as formally approved.

## Verified implementation and checks

- Database foundation: `app/db/connection.py`, `schema.py`, `seed_data.py`,
  and customer-scoped repository functions exist; see `docs/01` and `docs/02`
  for canonical design and sequencing.
- Streamlit UI: `app/ui/`; it imports the local fake backend through
  `app/ui/client.py`.
- Docker: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, and
  `.env.example` are present. Docker Compose passes provisional environment
  variables that the current chat does not use.
- Demo database initializer: `scripts/init_demo_db.py`; focused tests are in
  `tests/test_init_demo_db.py`.
- Evaluation: `data/eval/queries.csv` (70 cases),
  `scripts/eval/run_eval.py`, sample logs under `data/eval/logs/`, and focused
  tests in `tests/test_eval_runner.py`.
- On 2026-11-11, `uv run python -m pytest -q` passed: **545 passed**.
  `uv run python scripts/eval/run_eval.py` processed **70 cases**; quality,
  safety, hallucination, and tool-call metrics remain `not_assessed`.
  `uv sync --check` and `docker compose config --quiet` passed.

## Remaining work

- Ask the Technical Lead to resolve the database checkpoint discrepancy in
  the canonical plan before changing its recorded authorization status.
- Real backend, LLM/RAG integration, tool execution, and production
  authentication are not present in the current UI and are not represented as
  completed work here.

## Sources of truth

- `docs/01_database_design.md` — approved database design.
- `docs/02_database_implementation_plan.md` — database checkpoint sequence
  and current recorded status (including the discrepancy above).
- `.claude/plans/rest-plan.md` — delivery workstreams B1–B5.
- Source code and test results — evidence of what currently exists and what
  has been verified.
