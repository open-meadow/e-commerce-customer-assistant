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
customers (C101 to C106). The database must be created first (see Running the
Project). For offline UI work, `UI_BACKEND=fake` selects the old canned demo
backend (C101 and C102 only, no database).

| Area | Current state |
|---|---|
| Streamlit UI | Conversational customer support interface |
| Customer context | Simulated authenticated customer identity |
| SQLite database | Customers, orders, order items, returns, and support tickets |
| RAG pipeline | Retrieve information from FAQ and policy documents |
| Product lookup | Retrieve product details from `Products.json` |
| Backend tools | Controlled customer-data retrieval and support operations |
| LLM orchestration | Understand queries and coordinate retrieval and tools |
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
| AI capabilities | LLM |
| Knowledge retrieval | Retrieval-Augmented Generation (RAG) |
| Testing | Pytest |
| Version control | Git and GitHub |

Specific LLM providers, embedding models, and vector storage technologies were to be finalized during implementation. **Current state (2026-11-11):** policy retrieval is lexical BM25 (no embeddings, no vector store; those are deferred), and the LLM provider is chosen by environment variables with an offline rule-based default (see `docs/04_frontend_handover.md`).

## 5. High-Level Architecture

```text
                 CUSTOMER
                    |
                    v
             STREAMLIT CHAT UI
                    |
                    v
          CUSTOMER CONTEXT / SESSION
                    |
                    v
             AI ORCHESTRATOR
                    |
          +---------+----------+
          |         |          |
          v         v          v
       RAG       PRODUCT     BACKEND
      ENGINE     LOOKUP       TOOLS
          |         |          |
          v         v          v
      FAQ /      Products    SQLite
      Policies   JSON        Database
          |         |          |
          +---------+----------+
                    |
                    v
              LLM RESPONSE
                    |
                    v
               CUSTOMER
```

The architecture will evolve as individual components are implemented and tested.

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
│   ├── db/
│   ├── tools/
│   ├── rag/
│   ├── orchestrator/
│   └── ui/
│
├── data/
│   ├── source/
│   └── generated/
│
├── docs/
│
├── tests/
│
├── .gitignore
├── PROJECT_STATUS.md
├── README.md
└── requirements.txt
```

## 8. Dataset

The project uses a provided synthetic dataset containing:

- `FAQ.md` — common customer questions and answers.
- `Policies.md` — shipping, returns, refunds, cancellation, and warranty policies.
- `Products.json` — structured product information.
- `customer_queries_sample.csv` — sample customer support queries.
- `README_DATASET.md` — dataset documentation.

Synthetic transactional records will be generated for customers, orders, order items, returns, and support tickets.

The supplied FAQ and policy documents are the source of truth for the MVP's support rules.

## 9. Development Methodology

The project will be developed incrementally through approved checkpoints.

1. Project discovery and requirements.
2. Database design.
3. Database implementation and authorization testing.
4. Backend service and tool development.
5. RAG pipeline implementation.
6. LLM orchestration.
7. Streamlit interface.
8. Integration and end-to-end testing.
9. Evaluation and documentation.
10. Final demonstration and deployment preparation.

Each checkpoint must be reviewed and tested before moving to the next.

## 10. Running the Project

From the repository root, create a demo database once (the parent folder must
exist; the command refuses to overwrite an existing file). Pass a reference time
close to the demo day, because return windows are measured from it:

```bash
mkdir demo
uv run python -m app.db.seed_data --db-path demo/demo.db --reference-time 2026-11-12T00:00:00Z
```

Then start the UI, pointing it at that file:

```bash
APP_DB_PATH=demo/demo.db uv run python -m streamlit run app/ui/streamlit_app.py
```

On Windows PowerShell set `$env:APP_DB_PATH = "demo/demo.db"` first. If the
database is missing, or a language-model provider is requested but not
configured, the page shows a message instead of the login. To run without a
database, set `UI_BACKEND=fake`.

Open the local URL printed by Streamlit, then choose a customer in the
sidebar. Use **New chat** to start another conversation or select a listed chat
to return to it. The chat list is temporary Streamlit session state: switching
simulated customers clears it, and reloading the page loses it. It is not
persistent account history.

Replies come from the backend over synthetic data. Confirming a return or a
ticket writes a real row to the chosen database; nothing leaves the machine.
With `UI_BACKEND=fake` replies are canned and confirming writes nothing.

## Run with Docker

From the repository root:

```bash
docker compose up --build
```

Open <http://localhost:8501>. Compose defines persistent Docker volumes and
forwards sample environment settings for a future database/vector-store/LLM
integration. The current chat does not use those settings and does not require
an API key.

Stop the service with `Ctrl+C`; use `docker compose down` to stop and remove the
container while keeping named volumes.

## Tests and evaluation smoke run

Run the test suite:

```bash
uv run python -m pytest
```

Run the demo-only evaluation harness:

```bash
uv run python scripts/eval/run_eval.py
```

It reads the synthetic case CSV, starts a fresh fake session per case, and
prints JSON replies and expected labels. It does not score answer accuracy,
safety, hallucinations, or tool-call correctness: the fake backend has no
real tools or observable tool-call trace. Sample outputs are in
`data/eval/logs/sample_conversations.jsonl` and are explicitly marked
`demo_only` / `not_assessed`.

## Security and scope limitations

- The customer selector is simulated and is not production authentication.
- The UI is not connected to the SQLite repository or real customer records.
- No real payments, refunds, shipping, cancellations, address changes, returns,
  or support tickets are performed.
- The current backend does not implement an LLM, RAG, product lookup, scope
  guard, or tool-call telemetry.
- Evaluation cases and sample logs are synthetic. The demo runner verifies
  only that the fake backend can be called for each case; it does not establish
  assistant correctness or safety.

Do not enter real personal, order, or payment information.

