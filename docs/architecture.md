> **Superseded diagram.** The diagram below shows the earlier fake-backend wiring and is kept for history only. The current system is: Streamlit UI -> `app.backend` -> orchestrator -> nine customer-scoped tools -> SQLite, with Chroma retrieval over `FAQ.md` and `Policies.md`, an LLM adapter (OpenAI Responses live-evaluated; Anthropic mock-tested; offline rule-based stand-in), and an optional LiveKit voice channel. See `docs/technical_report.md` section 2.

# Project Architecture

> **Update (branch `wire-ui-backend`):** the Streamlit UI now reaches the real
> backend through `app/ui/client.py` (`UI_BACKEND=real`, the default); the
> diagram and text below still describe the earlier fake-backend wiring, which
> remains available with `UI_BACKEND=fake` and is what the evaluation runner and
> Docker image still use. Person B to redraw the diagram and revise the report.

This diagram distinguishes the current implementation from the target design.
The target design is not implemented in the runnable chat.

```mermaid
flowchart LR
    subgraph Current["Implemented in this checkout"]
        subgraph Chat["Runnable Streamlit demo"]
            Customer["Customer<br/>(simulated C101 / C102)"] --> UI["Streamlit UI<br/>app/ui/streamlit_app.py"]
            UI --> Client["UI adapter<br/>app/ui/client.py"]
            Client --> Fake["Fake backend<br/>canned replies and demo flows"]
            Fake --> UI
            UI -. "conversation state in Streamlit session" .-> Session["UI session state"]
        end

        subgraph Database["Separate SQLite implementation"]
            Init["scripts/init_demo_db.py"] --> Connection["Connection factory"]
            Connection --> Schema["Schema and seed"]
            Connection --> Repository["Customer-scoped repository"]
            Schema --> SQLite["SQLite database<br/>app/db/data/app.db by default"]
            Repository --> SQLite
            DBTests["Database tests<br/>(temporary databases)"] -. "exercise separately" .-> Schema
            DBTests -. "exercise separately" .-> Repository
        end

        subgraph Eval["Demo-only evaluation"]
            Cases["70 labeled synthetic cases<br/>app/db/data/eval/queries.csv"] --> Runner["scripts/eval/run_eval.py"]
            Runner --> EvalFake["Fake backend<br/>fresh session per case"]
            EvalFake --> Report["JSON replies and labels<br/>quality / safety / tools: not assessed"]
            Logs["Sample conversations<br/>demo_only / not_assessed"]
            Cases -. "case-linked examples" .-> Logs
        end

        Sources["FAQ.md · Policies.md<br/>Products.json"]:::unwired
        Sources -. "not connected to current chat" .- UI
    end

    subgraph Future["Target design — planned, not implemented"]
        Backend["Backend API<br/>session and response handling"]
        Orchestrator["LLM orchestrator"]
        Tools["Validated, customer-scoped tools"]
        RAG["RAG over FAQ and Policies"]
        Catalog["Read-only product lookup"]
        Backend --> Orchestrator
        Orchestrator --> Tools
        Orchestrator --> RAG
        Orchestrator --> Catalog
        Tools --> FutureDB["SQLite repository"]
        RAG --> FutureSources["FAQ.md · Policies.md"]
        Catalog --> FutureProducts["Products.json"]
    end

    Client -. "future replacement; not connected" .-> Backend

    classDef implemented fill:#e7f4e4,stroke:#367c2b,color:#173b13
    classDef future fill:#fff3cd,stroke:#9a7615,color:#4d3b00
    classDef unwired fill:#f2f2f2,stroke:#777,color:#333
    class Customer,UI,Client,Fake,Session,Init,Connection,Schema,Repository,SQLite,DBTests,Cases,Runner,EvalFake,Report,Logs implemented
    class Backend,Orchestrator,Tools,RAG,Catalog,FutureDB,FutureSources,FutureProducts future
    class Sources unwired
```

## Reading the diagram

- **Implemented** shows the paths present in source. The chat is wired only to
  the fake backend; it does not query SQLite or the supplied knowledge files.
- The database implementation is separate. Its initializer, schema, seed,
  repository, and tests operate independently from the Streamlit conversation.
- The evaluation runner exercises the fake backend, emits expected labels and
  replies, and does not observe tool calls or assess answer quality or safety.
- **Target design** summarizes planned components. It is not evidence that an
  LLM, RAG pipeline, tool layer, or backend API is available in this checkout.
  See [`03_backend_architecture.md`](./03_backend_architecture.md), a design
  draft whose proposed backend implementation does not yet exist.
