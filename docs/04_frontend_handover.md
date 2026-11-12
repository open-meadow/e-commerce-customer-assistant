# Frontend and delivery handover (Person A to Person B)

**Date:** 2026-11-11. **Status:** the backend described here is implemented in the working tree of `rukum/backend` (uncommitted; the database commit `7dfd2f0` is on `rukum/db`). `uv run python -m pytest tests -q` gave **939 passed** on 2026-11-11. Anything marked **unverified** has not been run against the real thing.

You build `app/ui`, Docker, the evaluation set, the report and the deck. You do not edit `app/db`, `app/backend`, `app/orchestrator`, `app/tools`, `app/rag`, `app/llm` or `app/catalog`; ask Person A.

## 1. Rules

- Import only `app.backend` (and its types). Never import `app.db`, `sqlite3` or the orchestrator. Never read a database file directly.
- Never open, seed or modify the legacy `app/db/data/app.db`. It is protected in code (no connection factory opens it).
- Treat `ChatReply.text` as untrusted: render plain text, never HTML or markdown with raw HTML.
- Never log, display, store or put in a URL the `ConversationState` value. It is a bearer credential. Keep it in `st.session_state` only.
- Do not build your own confirmation by parsing text. Confirmation is a button that sends `ConfirmationDecision`.

## 2. Install, initialize the database and run

Dependencies (`streamlit` is declared in `pyproject.toml`; the backend itself uses only the standard library):

```bash
uv sync
```

### The database variable

The one canonical variable is **`APP_DB_PATH`** (not `DATABASE_PATH`, which no code reads). If it is not set, the application and the seed command both use `app/db/data/app_d2.db` under the project root. A relative value resolves from the project root, not from the working directory.

Normal startup **never creates** a database. Until you initialize one, `check_backend().status` is `not_initialized` and the login list is empty.

### Initialize (explicit, run once)

The parent directory must already exist; the file must not. The command creates the schema and the demo data in a new file, and refuses the legacy `app/db/data/app.db` and any existing database.

For a separate demo file:

```bash
mkdir demo
```

```bash
uv run python -m app.db.seed_data --db-path demo/demo.db --reference-time 2026-11-12T00:00:00Z
```

For the default location (`app/db/data/app_d2.db`, the same path the application uses), omit `--db-path`:

```bash
uv run python -m app.db.seed_data --reference-time 2026-11-12T00:00:00Z
```

`--db-path` and `APP_DB_PATH` resolve through the same function, so a seed command run with `APP_DB_PATH` set and no `--db-path` creates exactly the file the application will open. The command prints the path it used.

**Reference time.** Seeded orders are offsets from the reference time (default `2026-11-07T00:00:00Z`). Return windows are 48 hours (damaged, wrong product) and 168 hours (other reasons) from delivery, so pass a `--reference-time` close to the demo day (an ISO-8601 time with an explicit offset), or the demo orders will already be outside their windows. The seed is not idempotent: to re-seed, delete your own demo file and run the command again.

Your old `scripts/init_demo_db.py` calls `get_connection(db_path)`, which no longer creates files. Replace it with the command above, or with `get_initialization_connection(path)`, then `create_schema(connection)`, then `seed_database(connection, reference_time)`.

### Status: the UI is wired to the backend

Done on branch `wire-ui-backend` (plan: `docs/plans/ui_real_backend_plan.md`). `app/ui/client.py` re-exports the `app.backend` API by default and adds `backend_status()` (startup gate: not configured, not initialized, incompatible, unavailable). `UI_BACKEND=fake` selects the old canned `app/ui/fake_backend.py` for offline work. `session.py` is backend-independent, `streamlit_app.py` shows the gate, error banners from `reply.text`, the pending expiry, and choice and confirmation buttons. `handle_turn` is removed. Tests: `tests/ui/test_client_real.py`, `tests/ui/test_streamlit_app_real.py`. The text below is the original handover and is kept for the contract:

### Original note: the early UI files (now replaced)

`app/ui/client.py`, `app/ui/session.py` and `app/ui/streamlit_app.py` on `master` were an early stub that did not use `app.backend`. They were rewritten against the API in Section 4:

- `client.py` has `handle_turn(authenticated_customer_id, message, history)`, which returns canned text. That signature does not exist in the backend and must not be kept. Replace it with a thin wrapper over `handle_message(state, ...)`.
- `session.py` keeps `authenticated_customer_id` and a message list in `st.session_state` and passes the customer ID with every message. The backend never accepts a customer ID with a message: identity is fixed once by `start_session(customer_id)`. Keep only the opaque `ConversationState` (and your own display transcript) in `st.session_state`.
- `streamlit_app.py` hard-codes two customers (`SIMULATED_CUSTOMERS`). Replace that with `list_login_options()`. It also has no handling for `needs_choice`, `needs_confirmation` or error replies; add those (Section 5).

Because the stub imports with a package-or-script fallback (`from .client ...` / `from client ...`), keep your imports package-style (`from app.ui...`, `from app.backend import ...`) and launch as below.

### Run the UI

Launch from the repository root with `python -m streamlit`, which puts the repository root on `sys.path` so that `import app.backend` works. (`app` is not an installed package, and the bare `streamlit run` command puts only the script's own folder on the path, so it can fail with `ModuleNotFoundError: No module named 'app'`.)

```bash
uv run python -m streamlit run app/ui/streamlit_app.py
```

(`APP_DB_PATH` and the other settings are read from the git-ignored `.env` at the project root; see `.env.example`.)

PowerShell:

```powershell
$env:APP_DB_PATH = "demo\demo.db"; uv run python -m streamlit run app/ui/streamlit_app.py
```

Without `uv`, `python -m streamlit run app/ui/streamlit_app.py` from the repository root in the project's environment is equivalent. (This launch command could not be executed in the review that wrote it; confirm it once on your machine.)

### Startup, configuration and initialization failures

Do these checks once per app start, before showing the login screen. Import everything from `app.backend` (`LLMConfigError` is re-exported there).

```python
from app.backend import LLMConfigError, check_backend, get_backend, list_login_options

try:
    get_backend()                      # builds the LLM client; raises on a bad provider setting
except LLMConfigError as exc:
    st.error(f"The assistant is not configured: {exc}")   # names variables only, never values
    st.stop()                          # fix the environment and restart; do not retry in a loop

health = check_backend()
if health.status != "ready":
    st.error(MESSAGES[health.status])  # see the table below
    st.stop()

options = list_login_options()
```

| Failure | Where it shows | What the frontend does |
|---|---|---|
| `LLMConfigError` | raised by the first call that needs the backend (`get_backend`, `list_login_options`, `start_session`, `check_backend`); it keeps being raised until the environment is fixed and the process restarted | catch it once at startup, show the message (it names only the missing variables, for example `ANTHROPIC_API_KEY`), stop. Never show a traceback. It does **not** fall back to the rule-based stand-in. |
| `check_backend().status == "not_initialized"` | after startup | show "The database has not been initialized" and the seed command from Section 2. Not retryable until an operator initializes it. |
| `schema_incompatible` (`reason` `outdated` or `future`) | after startup | show "The database was created by a different version"; an operator must create a fresh database. |
| `unavailable` | after startup, or `StartSessionResult.error_code == "unavailable"` | show "The service is temporarily unavailable" and offer a retry button. This is also the result if `APP_DB_PATH` names the protected legacy file. |
| `list_login_options()` returns `[]` | login screen | treat it as a failure, call `check_backend()` and show its status; do not show an empty selector. |
| `start_session` returns `ok=False` | login | `unknown_customer`: refresh the list; `unavailable`: as above. |
| any other exception from the backend | anywhere | should not happen (replies carry errors instead). Log it with the `request_id` if you have one, show a generic message, keep the session. |

`check_backend()` does not call the language-model provider, so `ready` means the database is usable and the provider is configured, not that the provider is reachable. A provider outage appears later as a reply with `status == "error"` and `error_code == "unavailable"`.

## 3. Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `APP_DB_PATH` | SQLite file for the application and the seed command | `<project>/data/app_d2.db` |
| `LLM_PROVIDER` | `rule_based`, `openai` or `anthropic` | `rule_based` |
| `LLM_MODEL` | model name, required for real providers | none |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | for `openai` (any OpenAI-compatible endpoint) | none |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` | for `anthropic` | none |
| `LLM_TIMEOUT_SECONDS` | per-call timeout | adapter default |

There is no `.env.example` in the repository; the table above is the configuration reference. Do not commit real keys.

For Docker: set `APP_DB_PATH` to a file on a mounted volume (not `DATABASE_PATH`). The image must also contain `app/db/data/source/` (`FAQ.md`, `Policies.md`, `Products.json`); retrieval and the catalog read them at runtime. Run the seed command as a separate one-off step, never on container start. A health check can call `check_backend()` and expect `ready`.

`rule_based` needs no key and no network. It is a deterministic stand-in for a language model: it understands a fixed set of phrasings (order IDs like `ORD1001`, "return ... IT101", "my tickets", "refund", product words). The OpenAI-compatible and Anthropic adapters have only been tested against a mocked transport (`tests/test_providers.py`); **no live provider call has ever been made (unverified)**.

### Configuring one live provider (Anthropic example)

The backend reads these from the process environment only. It does not read `.env` files, so set them in the shell, the Docker `environment:` block or your secret store. Never commit a key; `.env` and `.env.*` are gitignored.

| Variable | Required | Value |
|---|---|---|
| `LLM_PROVIDER` | yes | `anthropic` |
| `ANTHROPIC_API_KEY` | yes | your key (secret) |
| `LLM_MODEL` | yes | a model ID your account can use (for example `claude-sonnet-5-5`; confirm it in your account) |
| `ANTHROPIC_BASE_URL` | no | default `https://api.anthropic.com` |
| `LLM_TIMEOUT_SECONDS` | no | default 20 |

For an OpenAI-compatible endpoint use `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, `LLM_MODEL` and optionally `OPENAI_BASE_URL` (default `https://api.openai.com/v1`). The adapter sends `max_tokens`; some newer OpenAI models require `max_completion_tokens` instead, which is unverified here.

A requested provider with a missing variable fails closed: the backend raises `LLMConfigError` when it is first used and does **not** fall back to the rule-based stand-in. Check the configuration without a network call or printing any value:

```bash
uv run python -c "import os; from app.llm.providers import readiness; print(readiness(os.environ))"
```

It prints the provider name, whether it is configured, and the names (never the values) of any missing variables. The first real request is the first paid call; make it only with explicit authorization.

## 4. Public API (`from app.backend import ...`)

```python
list_login_options() -> list[LoginOption]          # LoginOption(customer_id, display_name)
start_session(customer_id: str) -> StartSessionResult
handle_message(state, message=None, *, decision=None, choice=None) -> ChatReply
end_session(state) -> None
get_backend() -> Backend                           # optional: builds the backend now; raises LLMConfigError if the provider setting is bad
check_backend() -> BackendHealth
```

`StartSessionResult(ok, state, greeting, error_code)`; `error_code` is `unknown_customer` or `unavailable`.

`check_backend()` returns `BackendHealth(status, reason, llm_mode, notes)`:

- `status`: `ready`, `not_initialized`, `schema_incompatible` or `unavailable` (the database state; it never calls the language-model provider).
- `reason`: `outdated` or `future` when `status == "schema_incompatible"`, otherwise `None`.
- `llm_mode`: the active model client, `rule_based` (offline stand-in), `openai` or `anthropic`.
- `notes`: a tuple of short, customer-safe strings about the current limits (for example that FAQ/policy retrieval needs OpenAI embeddings and that the offline assistant is not a language model). Safe to show in an About or status panel.

`handle_message` takes **exactly one** of `message` (typed text), `decision` (`ConfirmationDecision(action_id, accepted)`) or `choice` (`ChoiceSelection(prompt_id, option_id)`). It never raises for user or service problems; it returns a `ChatReply`. It raises `TypeError` only if `state` is not a `ConversationState`.

```python
ChatReply(text, state, status, error_code, pending, choice, citations, request_id)
```

- `status`: `ok`, `needs_choice`, `needs_confirmation` or `error`.
- `error_code` (only when `status == "error"`): `message_too_long`, `rate_limited`, `invalid_session`, `unavailable`, `invalid_request`, `internal`.
- `pending: PendingView(action_id, kind, summary, expires_at)` when `needs_confirmation`. `kind` is `return_request` or `support_ticket`. `expires_at` is UTC `YYYY-MM-DDTHH:MM:SSZ`, ten minutes after creation.
- `choice: ChoiceView(prompt_id, question, options)` when `needs_choice`; options are `ChoiceOption(option_id, label)`.
- `citations`: `Citation(source, section)` tuples for policy answers.
- At most one of `pending` and `choice` is set. `request_id` is for support or logs.

## 5. Expected UI behavior

1. **Login.** Call `list_login_options()`; show a selector; on submit call `start_session`. If the list is empty or `ok` is false with `unavailable`, call `check_backend()` and show its `status` (`not_initialized` or `schema_incompatible` means the database must be initialized, not a transient failure). Show `greeting` as the first assistant message. A new login replaces the previous session (call `end_session` on the old state).
2. **Chat.** Send typed text with `message=`. Append `reply.text` as the assistant message. Show citations under it. Replace your stored state with `reply.state` (it is the same handle). Keep the text input enabled at all times: some replies (for example a request for the return reason) expect a typed answer and show no buttons. Some replies are plain `ok` replies that nevertheless wait for a typed answer: the request for a return reason, and the question "What is the issue you'd like the ticket to be about?" (see Section 6). Do not treat a reply without buttons as the end of a workflow.
3. **`needs_choice`.** Render one button per option with its `label`. A click sends `ChoiceSelection(choice.prompt_id, option.option_id)`. Every choice includes a cancel-style option. A choice expires **ten minutes after it was shown**; the expiry is not extended by re-displaying it or by other messages. After expiry a click is answered "expired, nothing was changed". Typed text while a choice is open re-shows it; typed `cancel` (any case) cancels it.
4. **`needs_confirmation`.** Show `pending.summary`, a Confirm button and a Cancel button. Confirm sends `ConfirmationDecision(pending.action_id, True)`, Cancel sends `False`. Disable both after a click (replays are answered "already handled", but do not rely on it). Typing "yes", "confirm" or "ok" does nothing; only the buttons confirm. Typed `cancel` also cancels it. Show the expiry; after it passes the backend answers that the confirmation expired and nothing was saved.
5. **Errors.** `invalid_session`: clear stored state and return to login. `rate_limited`: tell the user to start a new session. `message_too_long`: keep the input and show the limit (2,000 characters). `unavailable` or `internal`: show `reply.text`, allow retry. Never show raw exceptions.
6. **Success messages** ("has been submitted", "has been created") come only from the backend after the database confirmed the write. Do not synthesize them.

## 6. Behavior to know (and test against)

- Identity comes only from the session. Asking for another customer's order returns "couldn't find".
- **Returns.** The customer names the order and item. The reason stored with the return is **the customer's own message, exactly as typed** (never a model rewrite). If the message is only a bare request ("I want to return item IT101 from order ORD1001"), the assistant asks for a short explanation, telling the customer that the next message is saved exactly as written and that they can type `cancel`. The next message becomes the reason unless it is plainly something else (a question, a greeting, another order); such a message is not stored and the assistant asks again. A reason over 1,000 characters is not stored or shortened: the assistant asks the customer to send a shorter one. Then the customer picks a reason category (damaged, wrong product, other; chosen only by the customer), the backend checks eligibility, shows a confirmation whose summary includes the exact reason text that will be stored (display `pending.summary` as plain text and preserve its line breaks), and only a confirmed action writes. Windows: 48 h (damaged, wrong product) and 168 h (other), inclusive; the order must be `Delivered`. **While the assistant waits for the return reason:** typed `cancel` (any case, optional punctuation) cancels the request and nothing is stored. A message that is plainly a question or a command (it ends in `?`, starts with a word such as what, where, when, why, how, which, show or list, names a different order or item, or is a greeting or an unsupported request) is **not** stored as the reason: the assistant answers that it is still waiting, keeps the original request and its original ten-minute expiry, and asks again. The deliberate trade-off is that a genuine reason that begins with a question word (for example "Why? Because it broke") is also asked again. Anything else is stored exactly as typed.
- **Ticket requests ask for the issue first.** If the customer asks for a ticket ("I want to speak to a human agent") and no issue is being tracked, the assistant replies with a plain `ok` message asking what the issue is, and the **next message becomes the issue** that the ticket describes (unless it is a greeting, thanks or an out-of-scope message). Typed `cancel` at that point cancels the request; the word is never stored as the issue or the ticket description. With an issue already tracked, the ticket proposal appears immediately.
- Tickets: ask for a ticket, describe the issue, confirm. Up to two issues are tracked at once (one active, one on hold); a third raises a choice about which to drop. Cancelling a ticket proposal leaves the issue as it was. Limits per session: 5 returns, 3 tickets, 60 messages; idle sessions end after 60 minutes.
- **Questions about an unsupported action are answered from policy.** "Can I cancel my order?" and "How do I change my address?" are answered from the FAQ text with its citation. An imperative request ("please cancel my order ORD1002") gets the honest "I can't do that" reply with a ticket offer.
- **A return can be refused at confirmation.** Eligibility is checked again when the customer confirms. If the window closed or another return was created in the meantime, the reply is a normal `ok` message that names the closed window (with its date) or the existing return, and nothing is saved.
- **Missing knowledge sources degrade, they do not break the chat.** If the product catalog cannot be loaded, product questions say that product information is temporarily unavailable (order items are shown without product names). If the FAQ or policy source cannot be loaded, policy questions say the assistant cannot confirm the answer and offer a ticket, with no citation. Greetings, order questions, returns and tickets keep working, and the features recover when the files return. These are ordinary `ok` replies, not errors.
- Unsupported actions (cancel an order, change an address, replacement or exchange) get an honest "I can't do that" with an offer of a ticket. Out-of-scope questions get a redirect.
- There is no real support email or phone. `support@example.com` in the source documents is a placeholder; do not show it.
- State is in memory: restarting the backend ends every session and drops every pending action. Run a single process.

## 7. Integration steps

Integration notes from the backend audit:

- The backend itself has no database-free fake; the UI's `UI_BACKEND=fake` is a separate canned demo, not the contract. To develop without a model or a real database against the real backend, use the real backend with `LLM_PROVIDER=rule_based` and a seeded temporary database (Section 2), or construct `Backend(llm=...)` as `tests/backend_helpers.py` does.
- The backend is one process-wide singleton (`get_backend()`); every Streamlit session and rerun shares it, and each browser session is isolated by its `ConversationState`. Streamlit reloading the script does not reset sessions, but restarting the process does.
- Idle expiry refreshes on every call that carries a valid state, including calls that return an error such as `message_too_long`.
- Only typed messages count toward the 60-message limit; confirmations and choices do not.
- `reply.text` for a successful return or ticket includes the new ID (`RET_...` or `TKT_...`). Show it; it is the only identifier the customer needs.

1. Write the UI against `from app.backend import ...`; run the real backend with `LLM_PROVIDER=rule_based` (no key needed). `Backend(llm=...)` in `tests/backend_helpers.py` shows how to substitute a scripted model.
2. Initialize a demo database (Section 2) and set `APP_DB_PATH`.
3. Walk the C101 flow: "Where is my order ORD1001?", "I want to return item IT101 from order ORD1001 because I changed my mind", pick "Another reason", Confirm. Then ask "show my returns".
4. Try cross-customer access (`ORD1003` as C101), an out-of-scope message, a typed "cancel" during a choice, and an expired confirmation (wait ten minutes).
5. Evaluation runner: call `handle_message` directly; no UI needed. Each case should use a fresh `start_session`. Seeded customers are C101 to C106; see `app/db/seed_data.py` for orders.
6. Docker: Compose runs `UI_BACKEND=real` with `APP_DB_PATH=/app/data/app_d2.db` on the persistent `app-data` volume. The entrypoint seeds that database only when the file is absent, then starts Streamlit; the UI imports the backend in-process. Optional `.env` values configure the LLM provider and API key.

## 8. Error states summary

| Situation | What you see |
|---|---|
| Provider requested but not configured | `LLMConfigError` from the first backend call (see Section 2, startup failures); the message names missing variables only |
| Product catalog or FAQ source missing or invalid | ordinary `ok` replies saying the information is temporarily unavailable; no citation; the rest of the chat is unaffected |
| Database file missing or not initialized | `check_backend().status == "not_initialized"`; empty login list |
| Old or newer schema | `schema_incompatible`, reason `outdated` or `future` |
| `APP_DB_PATH` points at the protected legacy file | `unavailable` (the file is never opened) |
| Database unavailable | `start_session` `unavailable`; chat replies say the order system is unavailable |
| LLM provider down | `status == "error"`, `error_code == "unavailable"` |
| Session ended or expired | `error_code == "invalid_session"` |
| Confirmation or choice expired, replayed, cancelled or foreign | normal reply (`status ok`) saying nothing was changed or already handled |

## 9. Known limitations

- The default assistant is rule-based; natural-language coverage is narrow. A real provider must be configured and tested by Person A before a demo that claims language-model behavior. **Unverified live.**
- Policy retrieval uses persistent Chroma vector search with OpenAI embeddings; `OPENAI_API_KEY` is required even when `LLM_PROVIDER=rule_based`. FAQ/policy source text is embedded, and each query is sent to OpenAI. The local index defaults to `app/db/data/generated/vector_store`; Docker Compose uses `/app/data/vector_store` on its persistent `app-data` volume. The index is separate from the SQLite business database. A cosine-distance cutoff can return no hits for unrelated questions.
- Login is simulated (pick a customer). There is no authentication.
- No durable pending actions or idempotency; a crash between a write and its reply can leave an ambiguous result, which the backend reports honestly and does not retry.
- Single process; no multi-worker deployment.
- Policy and catalog text is read from `app/db/data/source/` at first use and cached for the life of the process.
