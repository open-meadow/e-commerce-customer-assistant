# Technical Report

**Project:** AI E-Commerce Customer Support Assistant
**Report date:** 2026-11-12
**Project type:** Educational capstone prototype
**Data:** Synthetic only

## Abstract

This capstone prototype provides a Streamlit customer-support UI connected by
default to a local backend. The backend uses a simulated customer login,
customer-scoped SQLite operations, Chroma vector retrieval over FAQ and policy
documents, read-only product lookup, and confirmation-gated return and ticket
writes. Its default assistant is an offline, deterministic rule-based stand-in
for a general language model.

The UI also has an explicit canned fake-backend mode. Docker Compose uses the
real backend with a database seeded into a persistent volume on first start;
the evaluation runner uses the fake mode. The 70-case runner records fake
replies and expected labels, but marks answer quality, tool correctness,
hallucination, and safety metrics `not_assessed`. It is a demo smoke harness,
not an evaluation of the real assistant.

The project is synthetic and local. It is not connected to a real retailer,
customer account, payment provider, or courier.

## 1. Problem and scope

E-commerce support commonly involves questions about orders, delivery,
payments, returns, refunds, products, and store policies. The target capstone
system demonstrates a conversational interface that answers from approved
synthetic sources, accesses only the signed-in demo customer's data through
controlled tools, and can propose support actions for explicit confirmation.

The simulated login is not production authentication. Checkout, production
identity, real payments, refunds, couriers, and store integrations are out of
scope. The project uses five database tables: customers, orders, order items,
return requests, and support tickets. It has no payment or shipment tables.
The supplied FAQ and `Policies.md` describe sample policies, not a real
retailer.

## 2. Implemented system

### 2.1 Streamlit UI and backend

`app/ui/client.py` selects `app.backend` by default; `UI_BACKEND=fake` selects
the older canned backend. The real UI presents six synthetic login customers,
keeps chat history in Streamlit session state, and checks backend readiness
before showing login. Backend sessions are in-memory and single-process.
Simulated login demonstrates application-scoped identity but is not
production-grade authentication.

The backend orchestrates customer-scoped order, return, and ticket operations,
embedding-based FAQ/policy retrieval, product lookup, and LLM responses.
Return and support ticket writes require an explicit confirmation; the UI
reports success only when the backend confirms the operation.

### 2.2 Knowledge and language-model behavior

Policy and FAQ retrieval uses a persistent Chroma vector index with OpenAI
`text-embedding-3-small` embeddings over `FAQ.md` and `Policies.md`. It requires
`OPENAI_API_KEY` for indexing and searches, including when the LLM is using the
offline rule-based default. Product lookup reads `Products.json` separately
and is not part of RAG.

The default `RuleBasedLLM` is an offline deterministic stand-in that handles a
limited set of phrasings; it is not a general language model. OpenAI Responses
API and Anthropic adapters are available and have mocked transport tests, but
neither has been verified against a live provider. A misconfigured selected
provider fails closed rather than silently falling back to rule-based mode.

### 2.3 SQLite data layer

The database contains five tables: `customers`, `orders`, `order_items`,
`return_requests`, and `support_tickets`. Repository functions accept the
authenticated customer identity and scope reads and writes in SQL. Writes use
transactions and recheck eligibility. The default database path is
`app/db/data/app_d2.db`; normal application startup does not create or seed it.
The seed command creates a new database and refuses existing files and the
protected legacy `app/db/data/app.db`.

The canonical database implementation plan and the files currently present
have a documented checkpoint-status discrepancy for write-function
checkpoints 7–9. This report does not change or resolve that authorization
record; see `PROJECT_STATUS.md` and the canonical implementation plan.

## 3. Architecture and operational modes

The local real-backend path is:

```text
Customer
  -> Streamlit UI
  -> UI adapter
  -> app.backend
      -> customer-scoped tools -> SQLite
      -> Chroma vector retrieval -> FAQ.md / Policies.md
      -> product lookup -> Products.json
      -> LLM interface -> RuleBasedLLM (default)
```

`UI_BACKEND=fake` instead routes the UI to canned in-process responses and does
not access the database. Docker Compose sets `UI_BACKEND=real`; its entrypoint
seeds the persistent database volume once when the database file is absent.
The evaluation runner calls the fake backend directly. See
[`architecture.md`](./architecture.md) for the current and alternate paths.

## 4. Data protection and reliability boundaries

Customer identity is established by the application session, not by user
messages or LLM-generated tool arguments. Repository operations are
customer-scoped. The LLM does not have unrestricted SQL access, and state
changes require a confirmation action bound to the conversation session.
These are prototype controls, not a substitute for production authentication
or security review.

The rule-based assistant is limited to fixed phrasings. Backend sessions and
chat history are not durable and do not support multiple application
processes. Provider adapters have not been tested against live services. No
real payment, refund, shipping, courier, or retailer integration is present.

No real personal, order, or payment information should be entered.

## 5. Evaluation artifacts

`app/db/data/eval/queries.csv` contains 70 synthetic labeled cases across 17
categories, including order status, delivery, payment, returns, product and
policy questions, cross-customer attempts, prompt-injection attempts,
out-of-scope requests, ambiguity, and language robustness.

`scripts/eval/run_eval.py` runs each case in a fresh fake-backend session and
emits a JSON report. It records replies and expected labels but observes no
real tool-call trace. Answer accuracy, tool-call correctness, hallucination
checks, and safety checks are `not_assessed`. The case labels are expectations,
not evidence that the assistant performed the expected behavior. No
real-backend evaluation mode is present in this checkout.

## 6. Verification

| Check | Result |
|---|---|
| `uv run python -m pytest tests -q` | **1119 passed** on 2026-11-12 |
| Current real-backend readiness check | Ready against the configured existing `app/db/data/app_d2.db`; six synthetic login IDs available |
| Local UI smoke observation | C101's owned `ORD1001` query returned Delivered and Wireless Earbuds |
| Person A's 2026-11-12 handoff report | 20 of 20 scripted checks and seven browser checks against a temporary seeded DB; not independently repeated for this report |
| Demo evaluation | 70 cases; fake backend; metrics `not_assessed` |
| Provider adapters | Mocked transport tests only; no live provider call verified |

The local UI observation above is limited to the owned-order query. A
cross-customer request remained pending during this documentation task, and
return/ticket confirmation flows were not exercised here. No screenshot is
included as evidence of those flows. The broader scripted and browser results
are attributed to Person A's handoff, not presented as independently
reproduced.

## 7. Limitations and future work

The real backend is implemented, but the default assistant is rule-based and
the provider integrations and embedding calls are not live-verified. Retrieval
uses OpenAI embeddings and requires network access and an API key. Sessions are
in-memory and single-process, the login is simulated, and the evaluation
runner does not measure real assistant quality or safety. Docker uses
persistent database and vector-index storage; live provider adapters remain
unverified.

Future work could add live-provider verification, a real-backend evaluation
mode with measurable tool traces, durable session management, production
authentication, and production-grade database provisioning and backups. These
are future activities, not claims about the current prototype.

## 8. Reproduction

From the repository root with Python 3.13+ and `uv` installed:

```bash
uv sync
uv run python -m pytest tests -q
uv run python -m streamlit run app/ui/streamlit_app.py
```

The local UI uses `APP_DB_PATH` (default `app/db/data/app_d2.db`). FAQ and
policy retrieval also requires `OPENAI_API_KEY`; its persistent Chroma index
uses `VECTOR_STORE_DIR` (default
`app/db/data/generated/vector_store`), separate from the business database.
Use an already initialized database if one exists. To create a separate new demo
database, select a new path and current UTC reference time; do not run the seed
command against an existing file:

```bash
mkdir -p demo
uv run python -m app.db.seed_data --db-path demo/demo.db --reference-time 2026-11-12T00:00:00Z
APP_DB_PATH=demo/demo.db uv run python -m streamlit run app/ui/streamlit_app.py
```

The protected legacy `app/db/data/app.db` must never be opened, seeded,
reset, or modified. The demo-only evaluator can be run with:

```bash
uv run python scripts/eval/run_eval.py
```

Docker starts the real-backend UI and seeds its persistent database volume on
first start with:

```bash
docker compose up --build
```
