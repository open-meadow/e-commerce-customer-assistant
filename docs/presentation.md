---
title: "AI E-Commerce Customer Support Assistant"
subtitle: "Capstone prototype | Technical presentation"
author: "Rukum Maurya"
date: "2026-11-11"
---

> **Superseded.** This outline describes an earlier prototype state (fake backend, 545 tests, no LLM or RAG) and is kept for history. The current deck is `docs/Capstone_Presentation.pptx`.

## Project at a glance

**Goal:** Explore a text-based assistant for common e-commerce support questions.

- Streamlit chat demonstration with simulated customers
- Independent SQLite data layer and automated tests
- Synthetic FAQ, policy, product, and evaluation data

**Important:** This is a prototype, not a live customer-support service.

*Evidence: README.md; docs/technical_report.md*

## The support problem

Customers commonly need help with:

- Order and delivery status
- Payment, refunds, and returns
- Product details and store policies
- Escalating issues that need human support

The target system should answer from approved sources and use controlled,
customer-scoped actions.

*Evidence: docs/00_project_discovery.md; app/db/data/source/FAQ.md*

## Scope and data

**Included**

- Synthetic customers, orders, products, and sample policy content
- Simulated login choices: C101 and C102
- Local development and container run paths

**Not included**

- Real store, customer accounts, payments, or couriers
- Production authentication, checkout, or real refunds

*Evidence: README.md; app/db/data/source/Policies.md; app/db/data/source/Products.json*

## What runs today

| Component | Verified state |
|---|---|
| Streamlit interface | Runs with simulated login and chat state |
| Chat response path | UI adapter calls a fake backend with canned replies |
| Return confirmation | Demonstration flow only; no request is submitted |
| SQLite data layer | Implemented and tested separately from the chat |
| Evaluation runner | Calls the fake backend; smoke test only |

*Evidence: app/ui/; app/db/; scripts/eval/run_eval.py*

## Current architecture

```text
Runnable chat
Customer -> Streamlit UI -> UI adapter -> Fake backend -> Canned reply

Separate components
Database initializer -> SQLite schema / seed / repository -> Database tests
Evaluation cases -> Demo runner -> Fake backend -> JSON output
```

The chat is not connected to the database, FAQ, policy files, or product
catalog.

*Evidence: docs/architecture.md*

## Database layer: separate from chat

- Five tables: customers, orders, order items, return requests, support tickets
- SQLite connection enables and verifies foreign-key enforcement
- Repository operations are customer-scoped
- Seed data and product identifiers are synthetic

The UI does not call this repository. The canonical implementation plan's
write-checkpoint status conflicts with code and tests present in the checkout;
formal authorization remains unresolved.

*Evidence: docs/01_database_design.md; docs/02_database_implementation_plan.md; app/db/*

## Evaluation: useful coverage, limited evidence

- **70** labeled synthetic cases
- **17** categories, including policy, product, ambiguity, and safety-related prompts
- Runner starts a fresh simulated session for each case
- Expected labels are not evidence of actual tool calls

**Not assessed:** answer accuracy, tool-call correctness, hallucinations, and
safety behavior.

*Evidence: app/db/data/eval/queries.csv; scripts/eval/run_eval.py*

## Verification results

| Check | Result |
|---|---|
| Automated test suite | **545 passed** |
| Demo evaluation run | **70 cases**, **17 categories** |
| Evaluation quality and safety scores | `not_assessed` |
| Dependency lock consistency | Passed |
| Docker Compose configuration | Passed |

These checks do not establish the quality or safety of an LLM-powered assistant.

*Verified 2026-11-11; see docs/technical_report.md*

## Key limitations

- No LLM orchestration or retrieval-augmented generation (RAG)
- No live product lookup or customer-support tools in the chat
- No observable tool-call trace for evaluation
- Simulated login is not authentication
- Demo confirmations do not write to the database

Do not enter real personal, order, or payment information.

*Evidence: README.md; docs/architecture.md; docs/technical_report.md*

## Proposed next steps

1. Resolve the database checkpoint-status discrepancy with the Technical Lead.
2. Implement and integrate a backend with customer-scoped, validated tools.
3. Add policy retrieval and read-only product lookup.
4. Add observable tool-call logs and evaluate real responses.
5. Test authorization, confirmation, and failure paths before claiming support readiness.

These are future-work proposals, not implemented capabilities.

*Evidence: docs/03_backend_architecture.md; docs/technical_report.md*

## Run the prototype

```bash
uv sync
uv run streamlit run app/ui/streamlit_app.py
```

Run tests and the demo-only evaluator:

```bash
uv run python -m pytest
uv run python scripts/eval/run_eval.py
```

*Requirements and limitations: README.md*

## Takeaway

The repository demonstrates a tested UI shell, an independent SQLite layer,
and a reproducible demo evaluation harness.

**It does not yet demonstrate an integrated AI support assistant.**

Keep implemented behavior, measured evidence, and planned design clearly
separate.

*Further detail: docs/technical_report.md; docs/architecture.md*
