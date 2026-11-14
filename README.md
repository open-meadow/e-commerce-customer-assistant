# E-Commerce Customer Support Assistant

This repository is an educational capstone prototype for a text-and-voice
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
| Policy retrieval | Persistent Chroma vector search over FAQ and policy documents using OpenAI embeddings |
| Voice input | LiveKit microphone streaming with OpenAI speech-to-text |
| Spoken replies | Fish Audio text-to-speech through LiveKit Inference |
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

## 4. Technology Stack

| Area | Technology |
|---|---|
| Programming language | Python |
| User interface | Streamlit |
| Database | SQLite |
| Structured data | JSON |
| Knowledge base | Markdown documents |
| AI orchestration | Provider-agnostic LLM interface; deterministic rule-based default |
| Knowledge retrieval | ChromaDB with OpenAI `text-embedding-3-small` embeddings over Markdown FAQ and policy documents |
| Voice input | LiveKit Agents with OpenAI speech-to-text |
| Voice output | Fish Audio TTS through LiveKit Inference |
| Testing | Pytest |
| Version control | Git and GitHub |

OpenAI Responses API and Anthropic adapters are available. The OpenAI adapter was evaluated live with `gpt-4o-mini` (see `docs/evaluation/README.md`); the Anthropic adapter has only a mock-tested transport and has not been run live. The default is `rule_based`, which matches a limited set of phrasings. Policy retrieval uses OpenAI embeddings and requires `OPENAI_API_KEY` when searching or indexing, even when the LLM remains in `rule_based` mode.

Voice calls use a LiveKit room and agent. OpenAI handles speech transcription
and voice-agent turn routing; the exact transcript is sent to the support
backend. Backend reply text is sent to Fish Audio through LiveKit Inference for
speech synthesis. The voice transcript appears in the voice panel and is not
merged into the Streamlit text-chat history. `FISH_API_KEY` is not used for
this LiveKit Inference path.

## 5. High-Level Architecture

```text
Customer -> Streamlit UI -> UI client -> app.backend (real, default)
                                      -> fake backend (when UI_BACKEND=fake)

app.backend -> orchestrator -> customer-scoped tools -> SQLite
                            -> Chroma vector search -> FAQ.md / Policies.md
                            -> product lookup -> Products.json
                            -> LLM interface -> RuleBasedLLM (default)
Browser microphone -> LiveKit room -> voice agent
                                  -> OpenAI STT -> transcript -> support backend
Support reply -> Fish Audio via LiveKit Inference -> LiveKit room -> browser
```

`UI_BACKEND=real` is the local default. `UI_BACKEND=fake` selects the canned,
database-free demo. Docker Compose uses the real backend and seeds a persistent
database volume on first start. The demo runner `scripts/eval/run_eval.py` still uses the fake
backend.

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
│   ├── rag/            # Chroma vector search over policy/FAQ documents
│   ├── catalog/        # read-only product lookup
│   └── ui/             # Streamlit UI and optional fake backend
├── docs/
├── scripts/eval/       # run_eval.py (demo, fake backend); real_eval.py (real backend, live-capable)
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
Docker uses that backend with a seeded SQLite volume. The evaluation runner
remains a fake-backend smoke run and does not measure real assistant quality or
safety.

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

FAQ and policy search uses OpenAI embeddings, so set `OPENAI_API_KEY` in `.env`
even if `LLM_PROVIDER` is unset and the assistant uses the offline
rule-based model. The persistent Chroma index is stored separately from the
business database at `VECTOR_STORE_DIR` (default
`app/db/data/generated/vector_store`). It is rebuilt from the source documents
when their content changes.

To use the existing default database, simply run:

```bash
uv run python -m streamlit run app/ui/streamlit_app.py
```

Voice calls additionally require the LiveKit URL, API key and secret,
`OPENAI_API_KEY`, and the voice model/ID settings shown in `.env.example`.
`FISH_API_KEY` is not required. For local voice development, set
`SUPPORT_API_URL=http://127.0.0.1:8000` in `.env`, then run the support API and
LiveKit worker in separate terminals in addition to Streamlit:

```bash
uv run uvicorn app.backend.http_api:app --host 127.0.0.1 --port 8000
uv run python -m app.voice.agent start
```

The API uses the same initialized database as the local UI. The worker and
Streamlit process read settings from the repository-root `.env`.

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
cp .env.example .env
uv lock
uv sync
# Set OpenAI and LiveKit values in .env; see the configuration notes below.
docker compose up --build
```

Open <http://localhost:8501>. On first start, the container seeds a new database
in the `app-data` volume and uses it through `APP_DB_PATH`; the Chroma index is
also persistent at `/app/data/vector_store`. Compose runs separate Streamlit,
FastAPI, and LiveKit worker services; only Streamlit publishes a host port.
Set `OPENAI_API_KEY` for retrieval and voice processing, plus `LIVEKIT_URL`,
`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_STT_MODEL`,
`LIVEKIT_TTS_MODEL`, and `FISH_AUDIO_VOICE` in `.env`. The API and worker use
the LiveKit credentials; the Streamlit container receives blank API-secret
values. The browser connects to LiveKit with a short-lived participant token.
The browser also loads the pinned LiveKit JavaScript SDK from JSDelivr, so it
needs internet access.
Microphone audio is streamed through LiveKit to the voice agent; OpenAI
provides speech transcription and voice-agent turn routing, while Fish Audio
generates speech from backend reply text through LiveKit Inference. The default
support LLM remains offline and rule-based unless configured otherwise.

Stop the service with `Ctrl+C`; use `docker compose down` to stop and remove the
container and network. The `app-data` volume preserves the database and Chroma
index.

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
`demo_only` / `not_assessed`. The real-backend evaluation is separate: see `docs/evaluation/README.md` and `scripts/eval/real_eval.py`.

## Security and scope limitations

- The customer selector is simulated and is not production authentication.
- Backend sessions and UI chat history are in-memory and single-process.
- The default assistant is deterministic rule-based behavior, not a general
  language model. The OpenAI adapter was evaluated live on `gpt-4o-mini` (single runs, small sample); the Anthropic adapter and the voice channel have not been run live.
- If an external LLM provider is explicitly configured, prompts may be sent to
  that provider; the offline rule-based default makes no LLM provider call.
- Voice audio is streamed through LiveKit. OpenAI provides speech
  transcription and voice-agent turn routing; the transcript is sent to the
  support backend, and backend reply text is sent to Fish Audio through LiveKit
  Inference for speech synthesis. The browser receives a short-lived LiveKit
  participant token, not the LiveKit API credentials. Voice transcripts are
  shown in the voice panel and are not merged into Streamlit's text-chat
  history.
- FAQ and policy retrieval uses Chroma with OpenAI embeddings. It requires an
  OpenAI API key and sends source text when indexing and each search query to
  OpenAI. The local index is persistent; Docker Compose stores it on the
  `app-data` volume. Product lookup remains a separate structured lookup.
- There are no payment or shipment tables and no real payment, refund,
  courier, cancellation, or address-change integrations.
- Evaluation cases and sample logs are synthetic. The demo runner verifies
  only that the fake backend can be called for each case; it does not establish
  real assistant correctness or safety.

Do not enter real personal, order, or payment information.

