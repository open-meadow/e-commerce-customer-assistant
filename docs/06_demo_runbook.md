# Demo runbook and scripted verification

**Date:** 2026-11-12 (A5). **Audience:** whoever runs the demo (Person A or Person B). Commands are PowerShell, run from the repository root, and use only commands and files that exist in this repository. Nothing here needs an API key, a live provider or the network.

## 1. Read this first

- **`app/db/data/app.db` must never be opened or accessed.**
- **`app/db/data/app_d2.db` is protected.** Do not open, read, initialize, seed or modify it, and do not start Streamlit while `APP_DB_PATH` resolves to it, unless the technical lead explicitly authorizes that specific access. `.env.example` (and so a `.env` copied from it) sets `APP_DB_PATH=app/db/data/app_d2.db`. Every command below uses a demo database (`demo/demo.db`, git-ignored) or a temporary one.
- The standard demo needs **no live provider**. Any real-provider demonstration (`LLM_PROVIDER=openai` or `anthropic` with a key) needs separate, explicit authorization and is not described here.
- **Docker's current demo uses `UI_BACKEND=fake`** (set in `docker-compose.yml`). The fake UI backend returns canned text and writes nothing; it is **not evidence of assistant or language-model quality**.
- The real backend's default assistant is `RuleBasedLLM`: a deterministic keyword stand-in, **not a language model**. Its checks are deterministic and offline.

## 2. Which demo to run

| Mode | What you show | Needs | Use when |
|---|---|---|---|
| **A. Real backend, `RuleBasedLLM`, demo database** (recommended) | Customer-scoped lookups, return and ticket flows with explicit confirmation, rows really written to the demo database, policy answers with citations | `uv`, a seeded `demo/demo.db`; no key, no network | Normal demo |
| **B. Fake UI backend** (safest fallback) | The UI itself: customer selection, chat list, a canned return-choice then confirmation flow | Nothing (no database, no provider, no network) | Anything in A fails, or you only need to show the interface |
| **C. Docker** | Mode B in a container on port 8501 | Docker Desktop | You need to show the containerized build |

Mode B is the safest fallback because it depends on no database file, no environment configuration and no provider: it cannot fail on setup, and it cannot touch a protected file. Be honest on screen about what it is (a demo of the interface) and do not present its replies as assistant behaviour.

## 3. Prerequisites and startup checks

1. Python 3.13 and `uv` installed; from the repository root run `uv sync`.
2. Check Git: `git status -sb` (the working tree should be what you expect).
3. Check where `APP_DB_PATH` resolves **before starting Streamlit**:

```powershell
$env:APP_DB_PATH
```

A real environment variable overrides `.env`. If it prints nothing, the UI falls back to `.env` (normally `app/db/data/app_d2.db`, which is protected). Set it to the demo file as shown in Section 4. If you cannot be sure, use Mode B.
4. Run the scripted verification once (Section 6). It builds its own temporary database, so it is safe at any time.

## 4. Mode A: start the app locally (real backend, demo database)

Seed a fresh demo database. The return windows are measured from the reference time, so use the current UTC time on the day of the demo (never a future time). The seed command refuses an existing file, so to reseed delete `demo\demo.db` first.

```powershell
$now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
New-Item -ItemType Directory -Force demo | Out-Null
uv run python -m app.db.seed_data --db-path demo/demo.db --reference-time $now
```

Point the app at it and start Streamlit. The real environment variables below override anything in `.env`:

```powershell
$env:APP_DB_PATH = "demo\demo.db"
$env:UI_BACKEND = "real"
$env:LLM_PROVIDER = "rule_based"
uv run python -m streamlit run app/ui/streamlit_app.py
```

`UI_BACKEND=real` matters because a `.env` that sets `UI_BACKEND=fake` would otherwise silently give you the canned demo. Open the local URL Streamlit prints (normally <http://localhost:8501>; add `--server.port 8502` to the command if the port is busy). If the database is missing or a provider is requested but not configured, the page shows a message instead of the login.

## 5. Mode C: Docker

From the repository root, with Docker Desktop running:

```powershell
docker compose up --build
```

Open <http://localhost:8501>. The service `app` runs the Streamlit UI with `UI_BACKEND=fake`; the image contains no database and does not use the provider settings Compose forwards (`LLM_PROVIDER`, `OPENAI_API_KEY`, which you should leave unset). The Compose health check polls `/_stcore/health`. Docker was smoke-tested earlier in this project; it was not re-run for this runbook. Note that `docker-compose.yml` defines no volumes (the README still says it does).

## 6. The demo conversation

Choose a customer in the sidebar. Use **New chat** for another conversation and select a listed chat to return to it (the chat list is temporary session state; reloading loses it).

**Mode A (real backend).** The six seeded customers are C101 Alice Walker, C102 Bob Smith, C103 Charlie Patel, C104 Diana Roy, C105 Evan Sharma and C106 Fiona Das. Suggested flow as **C101**:

1. `Where is my order ORD1001?` shows the order's status and items.
2. `status of ORD1003` (an order that belongs to C102) answers that it cannot be found on the account, revealing nothing.
3. `I want to return item IT101 from order ORD1001 because I changed my mind about it`, then choose **Another reason**. The assistant shows what will be saved, exactly as typed. Typing "yes" does nothing; click **Confirm**. The reply shows the new `RET_...` ID.
4. `show my returns` lists it. Repeating step 3 is refused (an active return already exists).
5. **New chat**, `I want to speak to a human agent`, then describe the issue (for example `My earbuds from ORD1001 stopped charging after two days and I need help.`). Confirm; the reply shows the `TKT_...` ID. `show my tickets` lists it.
6. Switch to **C102** and ask `show my returns` and `show my tickets`: both are empty.
7. Policy and scope: `How long do I have to return a regular item?` (answer with a citation) and `Tell me a joke` (a redirect).

**Mode B and C (fake backend).** Two simulated customers (C101 and C102). Any message containing "return" starts a canned reason choice and then a Confirm/Cancel step; `demo confirmation` opens the confirmation step directly; `demo error` shows the error banner; other messages get a fixed "this is a demo reply". Confirming writes nothing.

## 7. Scripted verification

```powershell
uv run python scripts/verify_real_backend.py
```

It builds a fresh temporary database for each run (fixed reference time `2026-11-07T00:00:00Z`, both clocks fixed at that instant, so it behaves the same on any day), runs the real `Backend` with `RuleBasedLLM` only, drives the same public calls the UI uses, and deletes everything afterwards, even on failure. It never reads `.env`, never uses the default path, refuses to open `app.db` or `app_d2.db`, and counts network attempts (it requires zero). It checks business state (database rows, pending actions, the repository's own eligibility answer as an independent oracle), not wording, except where text is the behaviour (a status, an ID shown, a list).

| Section | Checks |
|---|---|
| Startup | temporary database only; `RuleBasedLLM`; database ready |
| Customer selection and seed | six customers; unknown customer refused; no returns or tickets at the start |
| Order lookup | own order shows its stored status; another customer's order reveals nothing |
| Return rejection | another customer's item, expired damaged window and not-delivered order are refused with no confirmation and no write |
| Return proposal | category choice and proposal write nothing; the exact reason is shown; typing "yes" does not confirm; Confirm writes exactly one row owned by the customer; the stored reason equals the typed text; the ID is shown; replaying the confirmation writes nothing more |
| Duplicates | a second return for the same item is refused |
| Return retrieval | the owner sees it; another customer sees none |
| Tickets | proposal writes nothing; Confirm writes one open ticket with the customer's text; Decline writes nothing; retrieval is customer-scoped |
| Final state and safety | only C101 has a return and a ticket; zero network attempts; only the temporary database was opened |

**Expected result** (actual run, 2026-11-12; the script prints one line per check, then the summary):

```text
== Startup
[PASS] startup: temporary database, not the default or protected path
...
[PASS] safety: zero network attempts and only the temporary database was opened

RESULT: PASS (28/28 checks passed)
```

**Exit codes:** 0 all checks passed; 1 at least one check failed (the failing line shows `expected:` and `actual:`); 2 the run could not complete (the exception class is printed, never its message). **Interpreting a failure:** a failure in "Return rejection", "Duplicates" or the confirmation checks means a write happened (or was allowed) that should not have, or that a rule changed; treat it as a regression and do not demo the real backend. A failure in "Startup" means the environment, not the backend, is wrong. A failure of the safety check means something tried to use the network or another database: stop and investigate. If you cannot resolve it, use Mode B.

The script's own behaviour (passing, failing on a wrong backend, cleanup, tripwires) is tested by `tests/test_verify_real_backend.py`.

### Evidence and what was not reproduced

- **Historical, not re-run:** on 2026-11-12 a temporary scratch script (not in the repository) reported 20 of 20 checks on the real backend, plus 7 of 7 browser checks on the running Streamlit app, against a temporary database. Those numbers are historical.
- **This script is new:** it covers the same behaviours (and a few more: unknown customer, typing "yes", replayed confirmation, Decline) in 28 checks. It is **not** a re-run of the scratch script, and its 28/28 result is its own.
- **Not reproduced by this script:** the 7 browser checks and anything about the Streamlit page; the UI client adapter and startup gate (`app/ui/client.py`, `backend_status()`, `UI_BACKEND` selection; covered by `tests/ui`); the 10-minute expiry of confirmations and choices (the clock is fixed); and any real language model. The script also does not check reply wording beyond the cases above, so it says nothing about answer quality.
- For broader offline coverage see `scripts/eval/real_eval.py` (70 cases and five scenarios, mechanical checks; `docs/04_frontend_handover.md` Section 10).

## 8. Stopping and cleaning up

```powershell
# Streamlit: press Ctrl+C in its terminal.
# Docker:
docker compose down
# Demo database and session variables (the file is git-ignored; it is yours to delete):
Remove-Item demo\demo.db -ErrorAction SilentlyContinue
Remove-Item Env:APP_DB_PATH, Env:LLM_PROVIDER, Env:UI_BACKEND -ErrorAction SilentlyContinue
```

`scripts/verify_real_backend.py` and `scripts/eval/real_eval.py` clean up their own temporary databases. Never delete or reseed `app/db/data/app_d2.db` without the technical lead's explicit authorization.

## 9. If something fails

| Symptom | Do this |
|---|---|
| Docker will not build or start, or the port is busy | Use Mode B locally: `$env:UI_BACKEND = "fake"; uv run python -m streamlit run app/ui/streamlit_app.py` (add `--server.port 8502` if needed) |
| The page says the database is not initialized, or the login list is empty | Seed `demo/demo.db` as in Section 4, or use Mode B |
| The page says the assistant is not configured | A real provider is requested without its key. Set `$env:LLM_PROVIDER = "rule_based"` and restart, or use Mode B. Do not add a key without authorization |
| `$env:APP_DB_PATH` is empty or names `app_d2.db` | Do not start the app. Set it to `demo\demo.db`, or use Mode B |
| Orders look out of their return windows | The demo database was seeded on an earlier day. Delete `demo\demo.db` and reseed with the current UTC time |
| `verify_real_backend.py` fails | See Section 7; if unresolved, demo Mode B only and say so |

## 10. What is simulated and what is implemented

| Area | Status |
|---|---|
| Customers and login | **Simulated**: you pick a customer; there is no authentication |
| Data | **Synthetic** (six customers, 15 orders, a product catalog and FAQ/policy text) |
| Orders, returns, tickets | **Implemented** in the real backend on SQLite: customer-scoped reads, confirmed writes, eligibility windows (48 h damaged or wrong item, 168 h otherwise), duplicate protection |
| Confirmation | **Implemented**: nothing is written until the Confirm button; pending actions expire after 10 minutes |
| Policy answers | **Implemented**: lexical BM25 over `FAQ.md` and `Policies.md`, with citations |
| Assistant | **`RuleBasedLLM`**, a keyword stand-in (not a language model). OpenAI Responses and Anthropic adapters exist but are mock-tested only; one guarded probe is not an application evaluation |
| Refunds, couriers, payments, real support contact | **Not implemented / simulated**: no real refunds or shipments; `support@example.com` in the source text is a placeholder |
| Fake UI backend (Docker) | **Canned demo**: not connected to data or an assistant, writes nothing |
| Chat history | Temporary Streamlit session state; sessions and pending actions are in memory (one process) |
