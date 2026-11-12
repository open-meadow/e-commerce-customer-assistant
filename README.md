# E-Commerce Customer Support Assistant

This repository is an educational capstone prototype for a text-based
e-commerce customer-support assistant. It uses synthetic data only and is not
connected to a real store, customer account, payment provider, or courier.

## Current prototype

The Streamlit UI talks to the **real backend** (`app.backend`): order, return,
policy, product and ticket answers come from the SQLite database and the source
documents, and writes happen only after an explicit Confirm. The default
assistant is an offline rule-based stand-in for a language model, so it
understands a fixed set of phrasings. The simulated login lists the six seeded
customers (C101 to C106). The database must be initialized first (see Running the
Project). For offline UI work, `UI_BACKEND=fake` selects the old canned demo
backend (C101 and C102 only, no database).

| Area | Current state |
|---|---|
| Streamlit UI | Conversational customer support interface |
| Customer context | Simulated authenticated customer identity |
| SQLite database | Customers, orders, order items, returns, and support tickets |
| Policy retrieval | Lexical BM25 over FAQ and policy documents; no embeddings or vector store |
| Product lookup | Read-only lookup from `Products.json`, separate from policy retrieval |
| Backend tools | Customer-scoped order, return, and ticket operations |
| LLM orchestration | Provider-agnostic orchestration; offline rule-based behavior is the default |
| Ticket escalation | Create support ticket records |
| Testing | Authorization, business logic, retrieval, and orchestration tests |

**Authentication note:** The MVP uses a simulated customer selector in Streamlit session state. This is for demonstrating authorization boundaries and is not production-grade authentication.

### Explicitly Out of Scope

- Full e-commerce marketplace frontend.
- Product catalogue browsing and shopping interface.
- Customer registration and onboarding.
- Shopping cart and checkout.
- Payment gateway integration.
- Real courier or shipping integrations.
- Real refund processing.
- Production identity and authentication infrastructure.

Voice-based support may be considered as a future enhancement after the MVP is completed and tested.

## 4. Technology Stack

| Area | Technology |
|---|---|
| Programming language | Python |
| User interface | Streamlit |
| Database | SQLite |
| Structured data | JSON |
| Knowledge base | Markdown documents |
| AI orchestration | Provider-agnostic LLM interface; deterministic rule-based default |
| Knowledge retrieval | Lexical BM25 over Markdown FAQ and policy documents |
| Testing | Pytest |
| Version control | Git and GitHub |

OpenAI Responses API and Anthropic adapters are available but have only mock-tested transports; neither has been verified against a live provider. The default is `rule_based`, which needs no network or API key and matches a limited set of phrasings. Retrieval uses lexical BM25, not embeddings or a vector database.

## 5. High-Level Architecture

```text
Customer -> Streamlit UI -> UI client -> app.backend (real, default)
                                      -> fake backend (when UI_BACKEND=fake)

app.backend -> orchestrator -> customer-scoped tools -> SQLite
                            -> lexical BM25 -> FAQ.md / Policies.md
                            -> product lookup -> Products.json
                            -> LLM interface -> RuleBasedLLM (default)
```

`UI_BACKEND=real` is the local default. `UI_BACKEND=fake` selects the canned,
database-free demo. The Docker Compose service currently selects fake mode.

## 6. Security and Reliability Principles

Security is a core requirement, not an optional enhancement.

- Customer identity must originate from trusted application context.
- Customer IDs must never be trusted from user messages or LLM-generated arguments.
- Customer-specific database queries must enforce ownership filters.
- Customers must never access another customer's orders, returns, or tickets.
- The LLM must not receive unrestricted database or SQL access.
- State-changing operations must go through controlled backend functions.
- The assistant must never claim an operation succeeded without backend confirmation.
- Sensitive customer information must not be unnecessarily exposed in logs.
- Retrieved documents and user messages must be treated as untrusted input.

## 7. Project Structure

```text
ecommerce-support-assistant/
│
├── app/
│   ├── backend/        # public backend API and orchestration
│   ├── db/             # SQLite schema, seed, repository, source data
│   ├── llm/            # rule-based default and provider adapters
│   ├── rag/            # lexical policy/FAQ retrieval
│   ├── catalog/        # read-only product lookup
│   └── ui/             # Streamlit UI and optional fake backend
├── docs/
├── scripts/eval/       # demo-only fake-backend evaluation runner
└── tests/
```

## 8. Dataset

The project uses synthetic source data and an evaluation corpus:

- `app/db/data/source/FAQ.md` — common customer questions and answers.
- `app/db/data/source/Policies.md` — sample policies and the policy source of truth.
- `app/db/data/source/Products.json` — structured product information.
- `app/db/data/eval/queries.csv` — 70 synthetic labeled evaluation/demo cases.

Synthetic transactional records will be generated for customers, orders, order items, returns, and support tickets.

The supplied FAQ and policy documents are the source of truth for the MVP's support rules.

## 9. Development Methodology

The project was developed through approved checkpoints covering requirements,
the SQLite contract, customer-scoped backend tools, retrieval, LLM
orchestration, and the Streamlit UI. The real backend is connected locally.
Docker and the evaluation runner remain fake-backend paths; the runner does
not measure real assistant quality or safety.

## 10. Running the Project

Settings live in a git-ignored `.env` at the repository root; copy the template
once (the app and the seed command load it with python-dotenv, and real
environment variables override it):

```bash
cp .env.example .env
```

The local UI uses `APP_DB_PATH` (default `app/db/data/app_d2.db`). If that
database already exists and is initialized, point the app to it and do not seed
it again. Normal startup never creates a database. To create a separate new
demo database, first choose a new path whose parent directory exists:

```bash
mkdir -p demo
uv run python -m app.db.seed_data --db-path demo/demo.db --reference-time <current-UTC-time>
```

The seed command refuses an existing file and the protected legacy
`app/db/data/app.db`; it is not a reset or migration command. Never delete or
seed the legacy file. Set the UI database explicitly when using a non-default
file:

```bash
APP_DB_PATH=demo/demo.db uv run python -m streamlit run app/ui/streamlit_app.py
```

To use the existing default database, simply run:

```bash
uv run python -m streamlit run app/ui/streamlit_app.py
```

The local UI uses the real backend by default. If the database is missing or
incompatible, or a selected provider is misconfigured, the startup gate shows a
safe message instead of the login. Set `LLM_PROVIDER=rule_based` for the
offline deterministic assistant. Set `UI_BACKEND=fake` to use the canned
database-free fallback.

Open the local URL printed by Streamlit, then choose a customer in the
sidebar. Use **New chat** to start another conversation or select a listed chat
to return to it. The chat list is temporary Streamlit session state: switching
simulated customers clears it, and reloading the page loses it. It is not
persistent account history.

Replies in real mode use the configured local database, source files, and
backend tools. A return or ticket is written only after explicit confirmation
and backend success. The fake mode returns canned replies and writes nothing.

## Run with Docker

From the repository root:

```bash
docker compose up --build
```

Open <http://localhost:8501>. The current Compose configuration selects
`UI_BACKEND=fake` and the image contains no initialized database, so this is a
canned demo, not the real-backend local setup. The forwarded LLM variables do
not make the fake backend call a provider.

Stop the service with `Ctrl+C`; use `docker compose down` to stop and remove the
container and network.

## Tests and evaluation smoke run

Run the test suite:

```bash
uv run python -m pytest tests -q
```

Run the demo-only evaluation harness:

```bash
uv run python scripts/eval/run_eval.py
```

It reads 70 synthetic cases, starts a fresh fake-backend session per case, and
prints JSON replies and expected labels. The runner reports all quality and
safety metrics as `not_assessed`: it has no real tool-call trace and is not an
evaluation of the real assistant. Sample outputs in
`app/db/data/eval/logs/sample_conversations.jsonl` are labeled
`demo_only` / `not_assessed`. A real-backend evaluation mode is not included.

## Security and scope limitations

- The customer selector is simulated and is not production authentication.
- Backend sessions and UI chat history are in-memory and single-process.
- The default assistant is deterministic rule-based behavior, not a general
  language model. Provider adapters are not live-tested.
- If an external LLM provider is explicitly configured, prompts may be sent to
  that provider; the offline rule-based default makes no provider call.
- Retrieval is lexical BM25; there are no embeddings or vector store.
- There are no payment or shipment tables and no real payment, refund,
  courier, cancellation, or address-change integrations.
- Evaluation cases and sample logs are synthetic. The demo runner verifies
  only that the fake backend can be called for each case; it does not establish
  real assistant correctness or safety.

Do not enter real personal, order, or payment information.

