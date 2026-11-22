# E-Commerce Customer Support Assistant

An AI customer-support assistant for an online store. A customer signs in, asks about orders, deliveries, refunds, returns and
store policy in **English, Hinglish or Hindi**, and the assistant answers from the customer's own records and the store's
policy documents. It can start a **return request** or a **support ticket**, but only after the customer explicitly confirms.

It is built around one rule: **the language model proposes, code decides.** The model can suggest a tool call or some text;
deterministic code validates it, enforces who may see what, and owns every change to the database.

> Capstone project. All data is synthetic: no real store, customer, payment or courier is involved. Do not enter real personal
> or payment information.

## What it does

| Capability | How it works |
|---|---|
| Order and delivery status | Looks up the signed-in customer's own orders, items, dates, payment and refund state |
| Policy and FAQ answers | Retrieval over the FAQ and policy documents (Chroma vector index, OpenAI embeddings) |
| Returns | Checks the return window and eligibility, then creates a request after an explicit Confirm |
| Support tickets | Offers a ticket when a return is not possible or a human is asked for; created only after confirmation. An optional fresh-description flow is OFF by default (see [Support tickets](#support-tickets-fresh-description-flow-and-schema-version-2)) |
| Product questions | Read-only lookup from `Products.json` |
| Three languages | The customer's latest message decides the language; all fixed replies exist in English, Hinglish and Hindi |
| Voice (optional) | LiveKit microphone channel with speech-to-text and text-to-speech |

## Built-in safeguards

Each is enforced in code and covered by offline tests. Which of them were also confirmed against the live model is stated under
[Testing and evaluation](#testing-and-evaluation); the refund wording, ambiguity and return-journey safeguards are offline-tested only.

- **Customers see only their own data.** Ownership is enforced in SQL, and no tool takes a customer ID from the model. An
  order that does not exist and another customer's order give an identical "not found".
- **Nothing is written without a Confirm.** A pending action is bound to the session, expires after 10 minutes and is claimed once.
- **No false promises.** Replies that promise a refund, say one arrived, or quote a duration no source supports are corrected
  by code before the customer sees them.
- **It asks instead of guessing.** If two of your orders hold the product you named, it asks which one before reading anything.
- **A misconfigured provider fails loudly.** It never silently falls back to a different model.

Details and the reasoning behind each choice: [docs/architecture.md](docs/architecture.md).

## Architecture

```
Customer ─▶ Backend ─▶ Orchestrator ─▶ LLM (proposes)
                           │
                           ├─▶ Tool registry ─▶ Business logic ─▶ SQLite (customer-scoped SQL)
                           ├─▶ Retrieval ─────▶ Chroma over FAQ.md and Policies.md
                           └─▶ Guards and code-owned replies (language, refunds, durations, ambiguity)
```

## Technology

Python 3.13 · uv · Streamlit · FastAPI · SQLite · ChromaDB · OpenAI Responses API (`gpt-4o-mini`) and
`text-embedding-3-small` · LiveKit (voice) · pytest. An Anthropic adapter exists but has only been tested against a mock.

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/). Run everything from the repository root.

```bash
uv sync
cp .env.example .env
```

Create a demo database (six synthetic customers, 15 orders), then start the chat:

```bash
mkdir -p demo
uv run python -m app.db.seed_data --db-path demo/demo.db --reference-time 2026-11-07T00:00:00Z
```

```bash
APP_DB_PATH=demo/demo.db uv run python -m streamlit run app/ui/streamlit_app.py
```

Choose a customer in the sidebar and start chatting. With no API key the app uses an **offline rule-based stand-in**: it
understands a fixed set of phrasings and is a demo path, not a language model. Policy search needs `OPENAI_API_KEY` even in that
mode. To use a real model set `LLM_PROVIDER`, `LLM_MODEL` and the matching key in `.env`.

Docker (real backend, seeded database volume, port 8501). **Partly verified on 2026-11-19:** the `api` and `app` images were built from the merged `master` (`6e82d86`) and started without a `.env`; `api` reported healthy, Streamlit's health check answered `ok`, and the database was initialised in the volume (6 customers, schema version 1). This does **not** verify the whole stack: the optional voice `worker` has **not been tested**, the Docker UI was not driven in a browser, and no live provider was used. Details: [docs/06_demo_runbook.md](docs/06_demo_runbook.md):

```bash
docker compose up --build
```

Optional HTTP API and voice worker (voice needs LiveKit credentials and has never been run live; every setting is documented in
`.env.example`):

```bash
uv run uvicorn app.backend.http_api:app --host 127.0.0.1 --port 8000
uv run python -m app.voice.agent start
```

The demo runbook ([docs/06_demo_runbook.md](docs/06_demo_runbook.md)) covers Docker, the scripted verification and troubleshooting.

## Example conversations

**These are historical live samples, not a demonstration of the final code.** They are replies from a live run of the assistant on
`gpt-4o-mini` (code `78f9790`), copied unedited from the archived
[final validation run](docs/evaluation/corrective_v2/raw/live_v2/final/final_validation_report.json) and the seeded test customers.
The changes made after that run (refund-status wording replacement, the localised return journey, the order-ambiguity guards,
`ORD-1010`-style IDs) are **offline-tested only** and are not shown or proven by these conversations. Two kinds of reply appear:

- **Code-owned sentence:** fixed text chosen by code. Checked against the current catalogue: the English wording is identical today.
- **Model-written reply:** the model's own words. A rerun would phrase it differently.

**Someone else's order: nothing is revealed** (code-owned sentence)

> **Customer (C102):** ORD1010 is my flatmate's order, what's its status?
> **Assistant:** I couldn't find ORD1010 on your account. If you'd like, I can list your own orders.

**Which order?** It asks instead of guessing (code-owned sentence)

> **Customer (C101):** I already told you my order number, why do I have to repeat everything?
> **Assistant:** Sorry about the repetition. Here are your orders: ORD1001 (Delivered), ORD1002 (Shipped), ORD1014 (Processing). Which one is this about?

**Refund status**: grounded in the policy text (model-written reply)

> **Customer (C105):** Where is my refund for ORD1010?
> **Assistant:** Your refund for order **ORD1010** is currently **pending**. The refund amount is **₹1,999**. Once the returned item passes quality checks, refunds are typically processed within **3–7 business days**, depending on your payment method and bank.

**Hinglish**: a human-agent request becomes a ticket offer (model-written reply)

> **Customer (C102):** kisi insaan se baat karao
> **Assistant:** Zaroor, main ticket bana deta hoon. Aap kis baare mein ticket chahte hain? Agar order ID hai toh woh bhi bata dijiye.

A return runs as: describe the problem, the assistant checks the window, shows a confirmation card, and only the **Confirm** button
creates the request. The offline tests cover this journey in all three languages; it has not been re-run against the live model
since the localisation work. Screenshots are not included.

## Support tickets: fresh-description flow and schema version 2

Two changes are implemented and tested offline. Neither has been used against a protected or production database or against a live
model.

- **Fresh-description flow (Phase 1.1), default OFF.** `SUPPORT_TICKET_FRESH_FLOW_ENABLED` accepts only `true` or `false` and defaults to
  `false`. When ON, the customer's own current message is the ticket description (kept exactly as typed, at most 2,000 characters),
  then they pick one of their own orders or "no order", read a plain-text preview and press Confirm; typing "yes" never confirms.
  With the gate OFF the earlier flow is unchanged. With the gate ON the application starts only with the real repository writer
  and a migrated database, and otherwise refuses to start.
- **Database schema version 2 (Phase 2).** `support_tickets` is rebuilt with a nullable `conversation_summary` (new tickets have none;
  legacy summaries are untouched) and `ticket_events`, `ticket_erasure_log` and `ticket_write_intents` are added. Runtime code
  requires version 2 exactly, whatever the gate says: a version-1 database, including an existing `app_d2.db` or Docker volume, is
  refused until it is migrated or re-seeded. **Nothing migrates automatically.**
- **Manual, scratch-only migration.** `python -m app.db.migrate` (needs `--scratch`) takes a verified backup, rehearses a restore and
  refuses `app/db/data/app.db`, `app_d2.db` and any non-scratch use. It has been run on scratch databases only; no protected database
  has been migrated.
- **Creation.** Each confirmation card carries a request key. The key and the immutable payload are stored before the insert, so a retry
  of the same confirmation never creates a second ticket. Duplicate detection (a new request for an issue that already has an active
  ticket) is a separate outcome from idempotency. The account-level rate limit is counted inside the creation transaction.
- **Uncertain outcomes.** If the database cannot say whether a write committed, the card, key and payload are kept and no second
  creation is allowed. After a session expires or the process restarts, the next ticket request first reconciles the customer's own
  stored requests with their original key and payload.
- **Outstanding.** The delayed-replay policy (how long a stored confirmed request may be replayed without a fresh confirmation) is
  undecided; four English messages are proposals awaiting owner approval (`ticket_duplicate_active`, `ticket_idempotency_conflict`,
  `ticket_recovery_unknown`, `ticket_recovered_created`); the Hindi and Hinglish wording awaits a fluent reviewer. Production
  enablement stays blocked until these are settled.

## Testing and evaluation

| Tier | What it covers | Status |
|---|---|---|
| **Live-validated** | Real `gpt-4o-mini` runs: Evaluations A and B, then two post-fix checks, last on code `78f9790` (12 targeted conversations passed) | Replies read by hand; not signed by an independent reviewer |
| **Offline-tested** | Latest recorded full run: **4,314 passed, 0 failed, 0 skipped** (one ChromaDB deprecation warning), no network; it was not re-run after the later documentation-only edits. It includes the ticket flow and schema version 2 work. This includes every change after `78f9790` (the ticket-scope gate and compact prompt, refund-status wording, the localised return journey, the order-ambiguity guards, the guard that settles a return request the model answered in prose) | Shows the code does what it says; **not** verified against the live model |
| **Awaiting human review** | All 150 Hinglish and Hindi sentences are drafts; 26 are marked high priority and need a fluent reader. Four English ticket messages are also proposals awaiting owner approval | [Review sheets](docs/localization_review_hinglish_hindi.md) |

```bash
uv run python -m pytest tests -q
```

Read the full methodology, sample sizes and caveats in the [evaluation report](docs/evaluation/EVALUATION_REPORT.md). Raw evidence
(143 files with SHA-256 manifest, including failed attempts) is archived under
[docs/evaluation/corrective_v2/raw/](docs/evaluation/corrective_v2/raw/MANIFEST.md); 18 of them have a local user path redacted
([REDACTIONS.md](docs/evaluation/corrective_v2/raw/REDACTIONS.md)), and `verify_archive.py` there rechecks every hash.

## Known limitations

- The login is simulated; sessions are in memory and single-process. Not production authentication.
- Hinglish and Hindi wording is unreviewed. The model's own free prose is steered towards the customer's language, not enforced.
- The evaluation sets are small (one to five repetitions, temperature 0.2) and were written by the same team as the prompts.
- Order IDs are recognised in the form `ORD1010` (also `ORD-1010`, `ord 1010`); a bare number is left to the model.
- Voice has never been run live, and the optional voice `worker` container has not been tested. Docker `api` and `app` were built and
  health-checked on 2026-11-19; the rest of the stack, the Docker UI in a browser and any live provider were not exercised.
- The ambiguity guards act on tool calls; an order the model describes without calling a tool is not caught.
- Not verified: how a live model behaves with the fresh-description prompt, and several processes writing to one database file at once
  (SQLite single-file locking only). Schema version 2 has been migrated on scratch databases only.

The full list is in section 11 of the evaluation report.

## Repository map

| Path | Contents |
|---|---|
| `app/` | Application: `backend`, `orchestrator`, `llm`, `tools`, `rag`, `db`, `catalog`, `ui`, `voice` |
| `tests/` | Offline test suite (temporary databases only; never touches the real database or the network) |
| `scripts/eval/` | Evaluation tooling: real-backend harness, usage ledger, retry and composition tools |
| `docs/` | Documentation; start at [docs/README.md](docs/README.md) |
| `docs/evaluation/` | The evaluation report, results summaries and the archived raw evidence |

## Credits

Contributors, as named on the project deck: **Rukum Maurya** and **Rohan Varughese**. Commits in this repository are authored by
Rukum Maurya and by the `open-meadow` account. Individual responsibilities are not stated here because the attribution has not been
confirmed; they will be added once the contributors have confirmed them.

Contributor and agent guidance: [CLAUDE.md](CLAUDE.md), [AGENTS.md](AGENTS.md). Technical handover:
[docs/PROJECT_HANDOVER.md](docs/PROJECT_HANDOVER.md).
