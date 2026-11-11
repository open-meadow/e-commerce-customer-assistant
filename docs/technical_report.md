# Technical Report

**Project:** AI E-Commerce Customer Support Assistant  
**Report date:** 2026-11-11  
**Project type:** Educational capstone prototype  
**Data:** Synthetic only

## Abstract

This project is a prototype for a text-based e-commerce customer-support
assistant. The repository contains a runnable Streamlit demonstration with
simulated customer selection, a separately implemented SQLite data layer, a
Docker configuration, a safe demo-database initializer, and a 70-case labeled
evaluation dataset.

The current chat uses a local fake backend. It does not retrieve policies or
products, query the SQLite repository, call an LLM, execute real customer
tools, or create real returns or support tickets. Accordingly, the evaluation
runner is a smoke harness: it records fake-backend replies and expected labels,
but does not assess correctness, safety, hallucinations, or tool calls. The
implemented artifacts demonstrate project structure and testable components;
they do not constitute a completed AI support assistant.

## 1. Problem and scope

E-commerce support commonly involves questions about orders, delivery,
payments, returns, refunds, products, and store policies. The target capstone
concept is a conversational interface that can answer policy questions,
access only the signed-in customer's data through controlled tools, and
escalate unresolved issues.

The prototype uses synthetic data and a simulated login selector. It does not
connect to a real store, payment processor, courier, or customer account.
Production authentication, checkout, real refunds, and voice support are
outside the current scope.

The source dataset contains sample FAQ and policy documents, a five-product
JSON catalog, and six starter query examples. The project expanded the query
examples to 70 labeled synthetic cases. The FAQ and `Policies.md` describe
sample policies; they do not represent a real retailer.

## 2. Implemented system

### 2.1 Streamlit demonstration

The Streamlit app is in `app/ui/`. It offers simulated customers C101 and C102
and maintains chat state in the Streamlit session. Its adapter routes to
`app/ui/fake_backend.py`, which returns canned responses and supports demo
return-choice and confirmation flows. A confirmation explicitly does not
submit a real request. The UI is not wired to the SQLite repository or to the
FAQ, policy, or product files.

### 2.2 SQLite data layer

The independent database implementation defines five tables:
`customers`, `orders`, `order_items`, `return_requests`, and
`support_tickets`. The connection factory enables and verifies SQLite foreign
key enforcement and sets row results to `sqlite3.Row`. The seed data is
synthetic, uses a fixed reference baseline unless an explicit time is
provided, and validates product IDs against the read-only product catalog.
Repository functions are customer-scoped and parameterized.

The database work is separate from the runnable chat. Also, the canonical
implementation plan (`docs/02_database_implementation_plan.md`) records write
checkpoints 7–9 as not started and not authorized, while the current checkout
contains `create_return_request`, `create_support_ticket`, and corresponding
tests. This discrepancy is documented in `PROJECT_STATUS.md`; formal
checkpoint authorization remains unresolved and should not be inferred from
the files' presence.

### 2.3 Docker and database initialization

The repository includes a Docker image and Compose service for the Streamlit
app. Compose forwards provisional database, vector-store, and LLM environment
variables, but the current chat does not use them. The current fake-backend
demo does not require an API key.

`scripts/init_demo_db.py` accepts an explicit database path and timezone-aware
reference time, protects the project's canonical `data/app.db` from use, and
refuses to initialize a database containing existing tables or views. Its
validation uses temporary databases.

### 2.4 Evaluation artifacts

`data/eval/queries.csv` has 70 synthetic labeled cases across 17 categories,
including order status, delivery, payment, returns, product questions,
policies, cross-customer attempts, prompt-injection attempts, out-of-scope
requests, ambiguous messages, and language-robustness examples.

`scripts/eval/run_eval.py` runs each case in a fresh fake-backend session and
emits a JSON report. It reports expected-tool coverage and per-category case
counts, but it does not observe tool calls. Answer accuracy, tool-call
correctness, hallucination checks, and safety checks are explicitly reported
as `not_assessed`. Four illustrative, case-linked demo logs are provided in
`data/eval/logs/sample_conversations.jsonl`; they do not claim that expected
safety behavior was actually demonstrated.

## 3. Architecture

The current chat path is:

```text
Customer
  -> Streamlit UI
  -> UI adapter
  -> fake backend
  -> canned demo reply
```

Separately, database setup and repository tests exercise the SQLite layer.
The evaluation runner also calls the fake backend directly. There is no
implemented connection from the chat to the database, policies, or product
catalog. The proposed backend API, LLM orchestrator, RAG retriever, and
customer-scoped runtime tool layer remain future design. See
[`architecture.md`](./architecture.md) for the implemented-versus-planned
diagram and [`03_backend_architecture.md`](./03_backend_architecture.md) for
the backend design draft.

## 4. Data protection and reliability boundaries

The database design uses customer-scoped repository operations and composite
foreign keys for relational consistency. The tests exercise database and
repository behavior independently of the Streamlit fake backend. The login
selector is simulated and is not production authentication.

Because the chat does not use the database or real tools, its operation flows
do not demonstrate real authorization, cross-customer isolation, transaction
handling, or confirmed writes. The fake backend's demo confirmation is not a
database operation. The evaluation runner has no tool-call telemetry and
therefore cannot verify those runtime properties.

No real personal, order, or payment information should be entered into the
prototype.

## 5. Verification

The following checks were run on 2026-11-11:

| Check | Result |
|---|---|
| `uv run python -m pytest -q` | **545 passed** |
| `uv run python scripts/eval/run_eval.py` | Processed **70 cases** across **17 categories** |
| Evaluator answer/tool/safety metrics | `not_assessed` (as designed for the fake backend) |
| `uv sync --check` | Passed |
| `docker compose config --quiet` | Passed |
| `git diff --check` | Passed for the documentation changes |

These results verify the available tests, configuration, and smoke harness.
They do **not** measure answer quality or establish the safety of an
LLM-powered assistant.

## 6. Limitations and future work

The main limitation is that the conversational UI remains a fake-backend
prototype. Completing the target system would require implementing and
integrating the backend API, validated tools, product lookup, policy retrieval,
LLM orchestration, and observable tool-call logging. A subsequent evaluator
could then assess grounded answers, actual tool calls, safety behavior, and
hallucination risks against the labeled cases.

The database checkpoint status discrepancy should be resolved with the
Technical Lead before changing canonical checkpoint authorization. Documentation
and presentation materials should continue to distinguish code that exists
from work formally authorized and from future design.

## 7. Reproduction

From the repository root, with Python 3.13+ and `uv` installed:

```bash
uv sync
uv run streamlit run app/ui/streamlit_app.py
```

Run the test suite and demo-only evaluation harness with:

```bash
uv run python -m pytest
uv run python scripts/eval/run_eval.py
```

For the containerized demo:

```bash
docker compose up --build
```

See the [README](../README.md) for setup details and limitations.
