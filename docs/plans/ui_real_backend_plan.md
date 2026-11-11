<!-- Tracked copy of the approved plan. Each step is one commit; "[DONE]" marks a finished step. -->

# Plan: wire the Streamlit UI to the real backend

## Context
`master` (`abafcac`) has a tested backend (`app.backend`) and a finished UI, but the UI runs on `app/ui/fake_backend.py` through `app/ui/client.py`. Nothing in the demo reaches the database, the tools, the retrieval or the confirmation rules. Everything else (real evaluation, Docker, report, demo) depends on this link. The good news from reading both sides: the fake was built to the real contract, so the field names, `handle_message(state, message=None, *, decision=None, choice=None)`, the `needs_choice` / `needs_confirmation` handling and the per-chat `ConversationState` already line up. The work is an adapter switch, a startup gate, a few behavior fixes, and tests.

## Design decisions (keep it small)
- **One switch point:** `app/ui/client.py` keeps exporting the same names; an environment variable `UI_BACKEND` (`real` by default, `fake` for offline UI work and for the eval runner and existing tests) chooses the source. No other UI module imports a backend directly.
- **Real backend only through `app.backend`** (`list_login_options`, `start_session`, `handle_message`, `check_backend`, `get_backend`, `LLMConfigError`, the types). Contract: `docs/04_frontend_handover.md`.
- **Keep `fake_backend.py`** and its tests; `scripts/eval/run_eval.py` imports it directly and stays unchanged in this task.

## Steps, in order

**[DONE] Step 0. Baseline and a safe database (no code)**
- Confirm the baseline: `uv run python -m pytest tests -q` gives 985 passed, 5 failed (all `tests/test_init_demo_db.py`, unrelated).
- Create a demo database for running the UI, **not** `data/app_d2.db` (CLAUDE.md: only if the Technical Lead asks) and never `data/app.db`: `mkdir demo`, then `uv run python -m app.db.seed_data --db-path demo/demo.db --reference-time <now, ISO with Z>`. `demo/*.db` is already ignored (`*.db`). Remember the 48-hour scenario (ORD1005) stays valid only about 12 hours after the reference time.

*Step 0 result: working on branch `wire-ui-backend` (from `master` at `abafcac`). Baseline `uv run python -m pytest tests -q`: 985 passed, 5 failed (all `tests/test_init_demo_db.py`, as expected). Demo database seeded at `demo/demo.db` (ignored by `.gitignore`) with `--reference-time 2026-11-11T18:01:02Z`, so the 48-hour scenario (ORD1005) is valid until about 2026-11-12T06:00Z; reseed after that. `data/app.db` and `data/app_d2.db` were not touched.*

**[DONE] Step 1. `app/ui/client.py`: the adapter**
- Add the backend switch described above. In `real` mode re-export from `app.backend`: `ChatReply`, `ChoiceOption`, `ChoiceSelection`, `ChoiceView`, `Citation`, `ConfirmationDecision`, `ConversationState`, `LoginOption`, `PendingView`, `StartSessionResult`, `handle_message`, `list_login_options`, `start_session`.
- Make `app` importable in both launch modes: `streamlit_app.py` falls back to `from client import ...` when run as a script, where only `app/ui` is on `sys.path`. Reuse the pattern already used in `scripts/init_demo_db.py` and `scripts/eval/run_eval.py` (insert the project root derived from `__file__` into `sys.path`) so both `python -m streamlit run` and bare `streamlit run` (the Dockerfile) work.
- Add `backend_status()` returning a small result the UI can show: `ready`, or a customer-safe message for `not_initialized` (with the seed command), `schema_incompatible`, `unavailable`, and `LLMConfigError` (message names variables only). In `fake` mode it returns `ready`. Do this by calling `get_backend()` first (catching `LLMConfigError`), then `check_backend()`, exactly as `docs/04` shows.
- Delete the legacy `handle_turn` and its re-export in `app/ui/__init__.py`. Nothing uses it (checked), and it would open a new backend session per call.

*Step 1 result: `client.py` now selects the backend with `UI_BACKEND` (`real` default, `fake`; any other value raises `ValueError`), puts the project root on `sys.path`, re-exports the contract types and the three functions from `app.backend` (or the fake), and adds `BackendStatus` / `backend_status()` (`ready`, `not_configured`, `not_initialized`, `schema_incompatible`, `unavailable`, with customer-safe messages). `handle_turn` and its `app/ui/__init__.py` export are removed. Deviation from the plan: the one-line import change in `tests/ui/test_session.py` (types from `app.ui.fake_backend`) is made here instead of in Step 2, because the real default would otherwise break those tests. Checked: both modes import under package and script launch, `backend_status()` against `demo/demo.db`, a missing file, a bad provider and the fake, a real chat through the adapter (six customers, ORD1001 for C101, ORD1003 refused), and an `AppTest` load of the unchanged app with no exceptions. Suite: 985 passed, 5 failed (the same `test_init_demo_db` failures).*

**[DONE] Step 2. Decouple `app/ui/session.py` from the backend types**
- `session.py` imports `ChoiceView`, `ConversationState`, `PendingView` from `.client` only for annotations. Switch to `typing.TYPE_CHECKING` or `Any`, so session helpers work with either backend.
- `tests/ui/test_session.py` builds `ConversationState(customer_id="C101")` from `app.ui.client`, which is the fake's class. Point those imports at `app.ui.fake_backend` (the real `ConversationState` is opaque and takes only a handle). No assertion changes.

*Step 2 result: `session.py` no longer imports `client` at runtime; the backend types are imported only under `TYPE_CHECKING` (with `from __future__ import annotations`), so importing the session helpers loads no backend. The `tests/ui/test_session.py` import change was already made in Step 1. Added `tests/ui/test_session_decoupled.py` (a fresh-interpreter check that `app.backend`, `app.ui.client` and `app.ui.fake_backend` are not imported, and plain objects work as chat state). Checked script-mode import and an `AppTest` load of the app in both `real` and `fake` modes with no exceptions. Suite: 987 passed, 5 failed (the same `test_init_demo_db` failures).*

**[DONE] Step 3. `app/ui/streamlit_app.py`: behavior**
- **Startup gate:** at the top of `main()`, call `backend_status()`; on a problem show the message and `st.stop()` before the login selector. Today an uninitialized database would just show "No simulated customer login options are available".
- **Login list:** nothing to change beyond the gate; it already reads `list_login_options()` and uses `display_name`, so the six real customers (C101 to C106, with names) appear. A failed or empty list calls the status check instead of an empty selector.
- **Error replies:** real error replies carry a customer-safe `reply.text` (for example the 2,000-character limit). Show that as the banner for `status == "error"`, keep the fixed map only as a fallback, add `invalid_request`, and do not also append the same text to the transcript (today both happen).
- **Keep as is:** `invalid_session` restart (real sessions expire after 60 idle minutes and are lost on a backend restart), Confirm/Cancel buttons, choice buttons, plain-text rendering with `st.text`, one backend session per chat.
- **Small gaps to fix while here:** show `pending.expires_at` with the summary; keep the text input enabled while the assistant waits for a typed answer (a return reason or a ticket description gives an `ok` reply with no buttons); show `reply.citations` (optional, a caption under the assistant message).

*Step 3 result: `main()` now calls `_require_backend()` (uses `backend_status()`; shows the message, a "Try again" button for `unavailable` or a reload hint otherwise, then `st.stop()`) before the login selector. An empty login list re-checks the status and shows its message. Error replies (`status == "error"`) show the backend's own text as the banner (the fixed map stays as a fallback, with `invalid_request` added), are no longer appended to the transcript, and no longer clear an open confirmation or choice. The pending summary now shows `Expires at ... (UTC)`. Already satisfied, no change: the text box is always enabled, the real login list uses `display_name`, `invalid_session` restart, plain-text rendering. Deliberately skipped: the citation caption, because the rule-based replies already cite their source inline (revisit with a real LLM). Checked with `AppTest` on a scratch database: the gate for a missing database and for a bad provider (no login selector, no exception); six customers; C101 order status; a 2,001-character message shows the length banner while the three category buttons stay; typed text re-shows the choice; the pending summary and the expiry caption; Confirm creates one `RET_` row for C101/IT101; the fake backend's "demo error" shows a banner and no transcript entry. Suite: 987 passed, 5 failed (the same `test_init_demo_db` failures). AppTest tests are added in Step 4.*

**Step 4. Tests**
- New `tests/ui/test_client_real.py`: with the `temp_db` fixture (which sets `APP_DB_PATH`) and a fixture that calls `app.backend.set_backend(None)` before and after, check `list_login_options()` returns six customers, a session starts, order status for C101 works, a cross-customer order is "couldn't find", and `backend_status()` for a missing database (`not_initialized`), a bad provider (`LLM_PROVIDER=anthropic` without keys) and a healthy one.
- New `tests/ui/test_streamlit_app_real.py` using Streamlit's `AppTest` (available in the installed 1.67.0): load `app/ui/streamlit_app.py`, pick a customer, send a message, assert the reply is shown; run the return flow (reason, category button, Confirm) and assert a row exists in the temp database; assert the startup gate shows the seed-command message when the database is missing.
- The conftest guard still applies: tests never touch `data/app.db` or `data/app_d2.db`.
- Existing UI and fake-backend tests keep passing; the 5 `test_init_demo_db` failures are a separate task.

**Step 5. Run it and verify (manual)**
- `APP_DB_PATH=demo/demo.db uv run python -m streamlit run app/ui/streamlit_app.py` (PowerShell: set `$env:APP_DB_PATH` first). Also try bare `streamlit run` once to prove the path bootstrap.
- Checklist in the browser: C101 asks "Where is my order ORD1001?"; return flow with a typed reason, category buttons, Confirm shows a `RET_` ID; "show my returns"; a second return for the same item is refused; C101 asks for `ORD1003` (C102's) and gets "couldn't find"; typed `cancel` during a choice; a 2,001-character message shows the length banner; "I want to speak to a human agent" then a description gives a ticket proposal; a missing `APP_DB_PATH` file shows the startup message; `LLM_PROVIDER=anthropic` without a key shows the configuration message; restart the app mid-chat and confirm the "session expired, new session started" notice.
- Spot-check the database read-only afterward (return and ticket rows belong to C101).

**Step 6. Docs and config touched by the switch**
- `README.md` (Running the Project and "Replies are canned" paragraphs): add the database initialization step, `UI_BACKEND`, and that replies now come from the backend.
- `docs/04_frontend_handover.md`: remove the "obsolete stubs, replace them" section; record that the wiring is done and how.
- `CLAUDE.md` UI section and the "Where the code stands" paragraph; Person B's `docs/architecture.md` and `docs/technical_report.md` mention the fake.
- `docker-compose.yml`: set `UI_BACKEND=fake` for now so the container keeps working unchanged until the separate Docker task (it lacks `data/source`, a database and the right variable names).

**Step 7. Commit**
- A new branch from `master` for Person B, small logical commits (client/session, app behavior, tests, docs), `git diff --check` clean, no database or `.env` files, no push without review.

## Files
Modified: `app/ui/client.py`, `app/ui/__init__.py`, `app/ui/session.py`, `app/ui/streamlit_app.py`, `tests/ui/test_session.py`, `docker-compose.yml`, `README.md`, `docs/04_frontend_handover.md`, `CLAUDE.md`. New: `tests/ui/test_client_real.py`, `tests/ui/test_streamlit_app_real.py`. Reused: `app.backend` public API, `tests/conftest.py` fixtures (`temp_db`), `scripts/init_demo_db.py`'s `sys.path` pattern, `python -m app.db.seed_data`.

## Risks and gotchas
- **Import path:** `streamlit run` puts only `app/ui` on `sys.path`; the bootstrap in Step 1 (or `python -m streamlit`) is required, or `import app.backend` fails.
- **Misconfigured provider:** `get_backend()` raises `LLMConfigError` on first use and keeps raising until the environment is fixed; the gate must catch it once, not on every rerun. Default (no `LLM_PROVIDER`) is the offline rule-based assistant, so no key is needed.
- **Singleton and reruns:** the backend is one process-wide object; Streamlit reruns share it and each chat is isolated by its state. Tests must reset it (`set_backend(None)`).
- **Demo data ages:** reseed with a current `--reference-time` on demo day.
- **Not in this task:** the real evaluation, Docker, `init_demo_db.py`, LLM provider live verification, report and deck.
- **Assistant coverage is narrow:** the offline rule-based assistant understands a fixed set of phrasings (order IDs, "return ... IT101", "my tickets"); a quick phrase list for the demo script helps.

## Acceptance
All new UI tests pass with the existing suite (still 5 unrelated failures until the init-script task); the checklist in Step 5 passes by hand; `git diff --check` is clean; no change under `app/backend`, `app/db` or the other backend packages.

## Suggested split
Person B (Rohan): Steps 1 to 3, 5 and 6. Person A: review of the adapter against `docs/04`, the `AppTest` fixtures and backend-reset helper in Step 4, and any backend bug the manual run exposes.
