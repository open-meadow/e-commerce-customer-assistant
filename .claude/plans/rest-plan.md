You are helping me (Person B) on a university capstone: an AI E-Commerce Customer Support Assistant. The repo is on GitHub (open-meadow/e-commerce-customer-assistant), branch `master`. Deadline is about 2026-11-13, so we are working to a tight MVP. All data is synthetic. There is no real store, payment or shipping.
 
## What the product is
A Streamlit chat where a simulated logged-in customer asks about orders, delivery, payments, returns, refunds, products and store policies. The backend (RAG over FAQ/policy docs, tool calling over SQLite, an LLM orchestrator) is being built by my teammate, Person A. I own the frontend and the delivery materials.
 
## Split of ownership (strict, to avoid merge conflicts)
- **Person A (teammate) owns:** `app/db`, `app/backend`, `app/tools`, `app/llm`, `app/rag`, `app/orchestrator`, and their tests. Do NOT edit these.
- **Person B (me) owns:** `app/ui`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `data/eval`, `scripts/`, README, the technical report, and the deck.
- If something in A's area must change, tell me instead of editing it.
- Work on a branch `feat/delivery` cut from `master`. Rebase on `master` before each commit. Never edit `uv.lock` by hand: change `pyproject.toml` and run `uv lock`. Only add UI and evaluation dependencies. Person A adds LLM/RAG dependencies.
 
## Hard rules
- This is a **uv** project, Python 3.13. Use `uv run ...`. Do not use pip or create another venv.
- Never open, seed, reset or write `data/app.db`. Do not run `python -m app.db.seed_data` or `main.py`: both touch the real database. Do not modify `data/source/*`, especially `Products.json`.
- The UI must import only `app.backend` (the interface below). It must never import `app.db`, `sqlite3`, or read files under `data/`.
- Never print, log or display the session state object (it is a bearer credential).
- Do not put contact details in UI text. `support@example.com` in the source docs is a placeholder and must not be shown as a real channel.
- Never commit secrets. `.env.example` lists variable names only, with no values.
- Make small, reviewable commits. Do not push to `master` directly: open a pull request.
 
## Current state of the repo
- Database layer (`app/db/`) is done and tested. Customers are C101–C106.
- `app/ui/` already has a first Streamlit app: `streamlit_app.py`, `session.py`, `client.py`. It has a customer selector and a chat transcript with **stub replies**. `app/ui/client.py::handle_turn(authenticated_customer_id, message, history)` is a placeholder and does NOT match the real contract. Replace it.
- `streamlit` is already in `pyproject.toml`.
- Not built yet: the backend, RAG, LLM, evaluation set, Docker, README setup, report, deck.
 
## The contract you build against (frozen, from docs/03_backend_architecture.md Section 4)
Person A will publish `app/backend/types.py` and `app/backend/api.py`, plus a `FakeBackend`. Until that lands, build against a local stand-in in `app/ui/fake_backend.py` that follows exactly this shape, so the swap is trivial:
 
```text
list_login_options() -> list[LoginOption]
start_session(customer_id: str) -> StartSessionResult
handle_message(
    state: ConversationState,
    message: str | None = None,
    *,
    decision: ConfirmationDecision | None = None,
    choice: ChoiceSelection | None = None,
) -> ChatReply
 
LoginOption(customer_id, display_name)
StartSessionResult(ok: bool, state: ConversationState | None, greeting: str, error_code: str | None)
    error_code is "unknown_customer" or "unavailable"; on failure state is None
ConversationState: opaque. Store it unmodified (e.g. st.session_state). Never read, edit, print or persist it.
ConfirmationDecision(action_id: str, accepted: bool)      # confirm / cancel buttons
ChoiceSelection(prompt_id: str, option_id: str)           # category buttons
ChatReply(text, state, status, error_code, pending, choice, citations, request_id)
    status is one of: "ok", "needs_choice", "needs_confirmation", "error"
    error_code is set only when status == "error":
      message_too_long, rate_limited, invalid_session, unavailable, internal
PendingView(action_id, kind, summary, expires_at)         # shown next to confirm/cancel buttons
ChoiceView(prompt_id, question, options: tuple[ChoiceOption(option_id, label), ...])
Citation(source, section)
```
 
Rules for calling it:
- Exactly one of `message`, `decision`, `choice` per call.
- After every call, replace the stored state with `reply.state`.
- `reply.text` is untrusted: render as plain text or escaped, never as HTML or code.
- At most one of `reply.pending` and `reply.choice` is set. If `pending` is set, show its `summary` with Confirm and Cancel buttons that send a `ConfirmationDecision`. If `choice` is set, show its options as buttons that send a `ChoiceSelection`.
- On `error_code == "invalid_session"`, discard the state and call `start_session` again.
- Switching the selected customer ends the session: call `start_session` for the new customer and clear the transcript.
- Show clear loading (spinner) and error states. Never show raw exceptions or internal details. `request_id` may be shown for support.
- The customer ID is chosen only at login (selector fed by `list_login_options`). It is never sent along with chat messages.
 
## Your tasks, in order
**B1. Streamlit UI (`app/ui`)**
1. [DONE] Create `app/ui/fake_backend.py` matching the contract (canned replies, including one `needs_choice` flow with the three return-reason options "Damaged or defective item", "Wrong product received", "Other / standard return reason", one `needs_confirmation` flow, and one `error` reply).
2. **[DONE]** Rewrite `app/ui/client.py` as a thin adapter that imports the backend functions from one place, so switching from the fake to `app.backend` is a single import change.
3. **[DONE]** Update `streamlit_app.py` and `session.py`: login selector from `list_login_options`, chat transcript, confirm/cancel buttons, category buttons, spinner, error banner, new-session handling. Keep the code small and typed.
4. **[DONE]** Add a few unit tests for the pure session and state logic only (no Streamlit rendering, no database), under `tests/ui/`.
5. **[DONE]** Provide a one-line run command (e.g. `uv run streamlit run app/ui/streamlit_app.py`) and put it in the README.
 
**B2. Docker.** Complete these steps in order, and stop for approval between steps:
1. **[DONE] B2-1 — Confirm runtime configuration.** Person A has not finalized the runtime contract, so use these explicitly provisional, easy-to-replace samples for the first Docker pass: `DATABASE_PATH=data/app.db`, `VECTOR_STORE_DIR=data/vector_store`, `LLM_PROVIDER=openai`, and optional `OPENAI_API_KEY=` (empty unless a real provider integration is later enabled). The Compose service will forward these settings from the developer's local `.env`; app configuration will eventually use `DATABASE_PATH` when opening SQLite, `VECTOR_STORE_DIR` for persistent RAG index files, and `LLM_PROVIDER` plus `OPENAI_API_KEY` for the LLM client. Today only the SQLite default path exists in code; it is `data/app.db` and `get_connection()` does not read environment variables. The UI currently uses a fake backend, and no vector store or LLM-provider configuration exists yet, so the latter samples are not used by the app until those integrations land. Container startup is `uv run streamlit run app/ui/streamlit_app.py --server.address=0.0.0.0 --server.port=8501`; the Compose health check will probe Streamlit's `/_stcore/health` endpoint on port 8501. Revisit these sample names/values if Person A publishes the real contract before Compose wiring is completed. Do not put an API key in `.env.example`, source control, or chat.
2. **[DONE] B2-2 — Add the Docker image.** Create `Dockerfile` using Python 3.13 and uv; install the project dependencies, run as a non-root user, and set the project root as the working directory so the relative `data/app.db` path resolves as expected. Do not open, seed, reset, or write the real `data/app.db`.
3. **[DONE] B2-3 — Define the build context.** Add `.dockerignore` to exclude local environments, secrets, caches, Git data, and generated/runtime data that should not enter the image, while retaining files needed to install and run the app.
4. **B2-4 — Compose the service.** Add `docker-compose.yml` with the app service, configured environment forwarding, persistent volumes for `data/` and the confirmed vector-store directory, and a health check using the confirmed startup/health behavior. Provide `docker compose up --build` as the single run command.
5. **B2-5 — Document environment-variable names.** Add `.env.example` with names only and no values. A real API key is not needed for the current fake-backend demo. If the confirmed integration later requires an OpenAI key, keep it only in the developer's local, git-ignored `.env`; never put its value in `.env.example`, source control, or chat.
6. **B2-6 — Verify Docker behavior.** Build and run the Compose service, verify its health check and volume configuration using isolated test data/volumes, and confirm the documented command. Never use the real `data/app.db` for validation.
 
**B3. Demo data script (`scripts/`).** `scripts/init_demo_db.py` that creates the schema and seeds a demo database at a path I pass in, using `app.db.connection.get_connection(db_path)`, `app.db.schema.create_schema(connection)` and `app.db.seed_data.seed_database(connection, reference_time=<aware UTC datetime>)`. It must require an explicit reference time argument, refuse to run on a non-empty database, and never default to `data/app.db`. Note in its help text that the 48-hour "damaged" demo order (ORD1005) is only valid for 12 hours after the reference time, and the 7-day scenarios for about 4 to 5.5 days.
 
**B4. Evaluation (`data/eval`, `scripts/eval`).** Expand `data/source/customer_queries_sample.csv` (6 rows today; columns id, query, intent, category, example_expected_action, notes) into about 60–80 labeled queries in `data/eval/queries.csv`. Cover: order status, delivery, payments, returns (48h damaged vs 7-day standard), refunds, product specs and pre-purchase questions, policies, cancellation/address change/warranty (unsupported actions that must be answered honestly and escalated), complaints, escalation, cross-customer attempts (a customer asking about another customer's order), prompt injection, out-of-scope requests (coding, jokes, trivia), and ambiguous messages. Add columns for the customer ID to log in as and the expected behavior and tool. Write `scripts/eval/run_eval.py` that drives `handle_message` and reports accuracy, tool-call correctness, hallucination checks and safety checks. Add sample conversation logs.
 
**B5. Documents.** README (setup, run, Docker, limitations), refresh stale status text in `PROJECT_STATUS.md` and `AGENTS.md`, an architecture diagram, the technical report and the presentation deck. Facts about the backend come from `docs/03_backend_architecture.md`: read it and do not invent features.
 
## Facts to rely on
- Source of truth for policies: `data/source/FAQ.md` and `data/source/Policies.md`. Return windows: 7 days standard, 48 hours for damaged or wrong product.
- Money is stored as integer paise in the database. Timestamps are UTC ISO-8601.
- The assistant must never claim an action succeeded unless the backend confirms it. Writes (return requests, support tickets) always go through a confirm step.
 
## How to work
Start by reading `docs/03_backend_architecture.md` Sections 4 and 16, `app/ui/*.py`, `README.md` and `CLAUDE.md` (if present). Before writing code, summarize what you found and list the files you plan to touch, then wait for my OK. After each task, show the diff, how to run it, and what you could not verify. Do not start the next task until I say so.