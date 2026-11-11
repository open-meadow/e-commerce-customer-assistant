# E-Commerce Customer Support Assistant

This repository is an educational capstone prototype for a text-based
e-commerce customer-support assistant. It uses synthetic data only and is not
connected to a real store, customer account, payment provider, or courier.

## Current prototype

The runnable Streamlit UI has a **fake backend** with canned responses and
demo-only return-choice and confirmation flows. Simulated login options are
C101 and C102. The database schema, seed data, and customer-scoped repository
are implemented and tested separately, but the UI does not currently use them.

| Area | Current state |
|---|---|
| Streamlit chat | Implemented |
| Simulated customer selector | Implemented for C101 and C102; not real authentication |
| Return choice and confirmation | Demo flows only; no real return or ticket is submitted |
| SQLite schema, seed, repository | Implemented separately; not wired to the chat UI |
| Evaluation data | 70 synthetic labeled cases in `data/eval/queries.csv` |
| Evaluation runner | Demo-only smoke harness; it does not assess answer quality or observe tools |
| LLM, RAG, product lookup, live order tools | Not integrated into the runnable chat |

The target architecture includes retrieval over the FAQ and policy documents,
product lookup, customer-scoped tools, and LLM orchestration. These are future
capabilities, not current UI behavior.

## Data and policy sources

- `data/source/FAQ.md` — sample customer questions and answers.
- `data/source/Policies.md` — sample policy source of truth.
- `data/source/Products.json` — synthetic product catalog.
- `data/source/customer_queries_sample.csv` — original sample query set.
- `data/eval/queries.csv` — expanded labeled evaluation cases.

The current fake chat backend does not retrieve or query these sources to
answer messages. Database records and product details are synthetic.

## Requirements and setup

- Python 3.13 or newer.
- [uv](https://docs.astral.sh/uv/).
- Docker Engine with Docker Compose (optional, for the container run).

From the repository root, install the locked project and development
dependencies:

```bash
uv sync
```

## Run locally

From the repository root:

```bash
uv run streamlit run app/ui/streamlit_app.py
```

Open the local URL printed by Streamlit, then choose C101 or C102 in the
sidebar. Replies are canned demo text; live order, payment, product, policy,
return, refund, and ticket data are not connected. Confirming a demo action
does not submit a real request.

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
