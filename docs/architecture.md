# Project Architecture

The local Streamlit app and Docker Compose use the real backend by default.
`UI_BACKEND=fake` selects the canned, database-free demo; only the evaluation
runner uses that fake mode.

```mermaid
flowchart LR
    Customer["Customer<br/>(simulated login)"] --> UI["Streamlit UI<br/>app/ui/streamlit_app.py"]
    UI --> Client["UI adapter<br/>app/ui/client.py"]
    Client -->|UI_BACKEND=real (default)| API["app.backend"]
    Client -->|UI_BACKEND=fake| Fake["app.ui.fake_backend<br/>canned replies"]

    API --> Orchestrator["Orchestrator"]
    Orchestrator --> Tools["Validated customer-scoped tools"]
    Orchestrator --> RAG["Lexical BM25 retrieval"]
    Orchestrator --> Catalog["Read-only catalog lookup"]
    Orchestrator --> LLM["LLM interface<br/>rule-based default"]
    Tools --> DB["SQLite<br/>customers, orders, items,<br/>returns, support tickets"]
    RAG --> Docs["FAQ.md · Policies.md"]
    Catalog --> Products["Products.json"]
    Fake --> UI
    API --> UI

    Cases["70 synthetic cases"] --> Eval["scripts/eval/run_eval.py"]
    Eval --> Fake
    Eval --> EvalReport["Replies and labels<br/>all quality/safety metrics not_assessed"]

    Docker["Docker Compose"] -->|UI_BACKEND=real| UI
```

## Runtime behavior

- The UI imports backend functions through `app/ui/client.py`; it does not
  access SQLite directly.
- The default `app.backend` path uses the simulated login identity to create a
  session, then uses customer-scoped repository operations for order, return,
  and ticket data. Return and ticket writes require an explicit confirmation.
- Policy and FAQ retrieval is lexical BM25 over `FAQ.md` and `Policies.md`.
  Product lookup reads `Products.json` separately; it is not part of RAG.
- `RuleBasedLLM` is the offline default and handles a fixed set of phrasings.
  OpenAI Responses API and Anthropic adapters have mocked transport tests but
  have not been verified against live services.
- Backend sessions and Streamlit chat history are in-memory. A process restart
  ends sessions; this is a single-process demo, not durable production service.
- The SQLite schema has five tables: customers, orders, order items, return
  requests, and support tickets. There are no payment or shipment tables.

## Alternate demo paths and evaluation

- `UI_BACKEND=fake` uses `app/ui/fake_backend.py`, which returns canned replies
  and performs no database reads or writes.
- Docker Compose sets `UI_BACKEND=real`. On first start, the entrypoint seeds
  the persistent `app-data` volume if its database file is absent; the
  container uses that file through `APP_DB_PATH`.
- `scripts/eval/run_eval.py` runs each CSV case in a fresh fake session. It
  records replies and expected labels, but has no real tool-call trace and
  marks answer accuracy, tool-call correctness, hallucination checks, and
  safety checks `not_assessed`. It is a smoke harness, not a quality or safety
  benchmark.

## Security boundaries and limitations

The simulated customer selector demonstrates customer-scoped behavior; it is
not production authentication. Customer identity comes from the application
session, not user text or LLM-generated tool arguments. The assistant does not
integrate with a real store, payment processor, courier, or refund system.

See [`03_backend_architecture.md`](./03_backend_architecture.md) for backend
design and implementation details, and [`04_frontend_handover.md`](./04_frontend_handover.md)
for the UI contract.
