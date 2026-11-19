# Handover to Person B

**From:** Person A (Rukum, backend and AI). **To:** Person B (Rohan, frontend and delivery).
**Written against:** local `master` at `fdb372a` on 2026-11-13, first written 2026-11-12. `fdb372a` is the merge of `rohan/voice` and is **not pushed**: `origin/master` is still `967cfe9`, and local `master` is 24 commits ahead of it. **Deadline:** about 2026-11-13.

This note says what is finished, how to get running in ten minutes, who owns what, and what is left for each of us. It is a status note, not a design document. The contracts are `docs/04_frontend_handover.md` (UI to backend) and `docs/03_backend_architecture.md` (backend design); retrieval is described in `docs/07_retrieval_and_embeddings.md`.

## 1. What is finished

- The database layer, the backend and the Streamlit UI. The UI uses the **real backend by default** (`UI_BACKEND=real`); `UI_BACKEND=fake` keeps the old canned demo.
- Person A's delivery tasks A3 to A6 are committed locally: the real-backend evaluation runner `scripts/eval/real_eval.py` (A3), the documentation refresh (A4), `scripts/verify_real_backend.py` with `docs/06_demo_runbook.md` (A5) and the clean-clone acceptance of `e8ab746` (A6; its Streamlit smoke test used the fake backend only).
- **`rohan/voice` is integrated into local `master`** (`fdb372a`): the FastAPI service (`app/backend/http_api.py`), the LiveKit worker (`app/voice`), the Streamlit voice panel (`app/ui/livekit_*.py`), Chroma retrieval with OpenAI embeddings (`app/rag/vector_index.py`), the three-service Docker Compose setup, and a `.gitattributes` rule (`01adb32`) that keeps `*.sh` files LF. Person A added the testability refactor that makes the Chroma retrieval testable without a key (an injectable embedding function and a lexical local embedding, `docs/07_retrieval_and_embeddings.md`).
- **Docker now runs the real backend**, not the fake one. `api` seeds a database in a named volume on its first start, `app` serves Streamlit on port 8501, and `worker` runs the LiveKit agent (`docs/06_demo_runbook.md` Section 5).
- Measured on 2026-11-13, all offline with no key and no `.env`: the full suite gave **1256 passed** (the network audit plugin counted 0 external attempts), `scripts/verify_real_backend.py` gave **28/28**, and a Docker build plus start of `api` and `app` worked (`api` healthy, Streamlit health `ok`, a seeded database in the volume). The `worker` was built but not started.
- The assistant is the offline `RuleBasedLLM`, a deterministic stand-in, not a language model.
- **Policy and FAQ answers need `OPENAI_API_KEY`.** Retrieval uses OpenAI `text-embedding-3-small` through Chroma and has no BM25 fallback; this is separate from `LLM_PROVIDER`. Without the key the assistant says it cannot look up the FAQ and policies and offers a ticket. Orders, returns and tickets work without any key.
- **Not verified live:** the OpenAI and Anthropic LLM adapters (mocked tests only, plus one guarded one-request OpenAI probe reported by the technical lead; it is not an application evaluation), **real OpenAI embedding retrieval** (never run; its quality and the 0.7 distance cut-off are unmeasured) and **the LiveKit voice worker** (never run against LiveKit, OpenAI speech or Fish Audio). Everything above was measured with local, lexical test embeddings and no live service.

## 2. Get running

```bash
git pull   # only after Person A has pushed the integrated master; until then it brings nothing new
uv sync
cp .env.example .env
uv run python -m pytest tests -q
```

The test suite never needs a key or the network. Leave `OPENAI_API_KEY` and the LiveKit variables unset in `.env` unless you have the technical lead's authorization for live calls: a key in `.env` makes policy questions send live embedding requests.

Create your own database (never reuse someone else's; the parent folder must exist, the file must not). Use the **current UTC time** as the reference time, never a future one:

```bash
uv run python -m app.db.seed_data --db-path <your path>.db --reference-time <now, e.g. 2026-11-13T10:00:00Z>
```

Run the UI against it. `.env` points `APP_DB_PATH` at `app/db/data/app_d2.db`, so always set `APP_DB_PATH` explicitly for a scratch database.

```bash
APP_DB_PATH=<your path>.db uv run python -m streamlit run app/ui/streamlit_app.py
```

PowerShell: `$env:APP_DB_PATH = "<your path>.db"` first. Add `LLM_PROVIDER=rule_based` if you want to be certain no LLM provider is used. `UI_BACKEND=fake` runs without any database. For Docker use `docker compose up --build api app` and `docker compose down` (add `-v` only to delete the database and index volume); details and health checks are in `docs/06_demo_runbook.md` Section 5.

- The seed is not idempotent: to reseed, delete your file and run the command again.
- The 48-hour damaged scenario (ORD1005, customer C103) is valid for about 12 hours after the reference time. Reseed on demo day. In Docker the database is seeded once, with the current UTC time, when its volume is first created, so run `docker compose down -v` and start again on demo day.
- Never open, seed or modify `app/db/data/app.db` (protected legacy file). `app/db/data/app_d2.db` is the working database; use a scratch file for experiments.

## 3. Ownership (to avoid merge conflicts)

| Person B owns | Person A owns |
|---|---|
| `app/ui` (including `livekit_client.py` and `livekit_component.py`), `app/voice`, `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `.dockerignore`, `.streamlit/`, `scripts/`, `app/db/data/eval`, README, `docs/architecture.md`, `docs/technical_report.md`, `docs/presentation.*` | `app/db`, `app/backend`, `app/orchestrator`, `app/tools`, `app/llm`, `app/rag`, `app/catalog`, their tests, `docs/01` to `docs/07`, `AGENTS.md`, `PROJECT_STATUS.md` |

Not previously assigned (proposal, to be confirmed by the technical lead): `app/backend/http_api.py` and `app/rag/vector_index.py` were written on `rohan/voice` but sit in Person A's directories, so changes to them go through Person A; `.gitattributes` and `.env.example` are shared.

Working rules:
- Branch from `master` (for example `rohan/docs-real-backend`), keep commits small, and run the full suite before asking for a merge.
- If you need a change in Person A's area, ask; do not edit it. The text UI imports only `app.backend` (through `app/ui/client.py`), never `app.db` or `sqlite3`; the voice panel talks to the FastAPI service over HTTP.
- `.env` is git-ignored and may hold keys. Never commit it or paste its contents anywhere.
- Do not modify `Products.json`, `FAQ.md` or `Policies.md`.

## 4. Person B: remaining tasks

Estimates are the 2026-11-12 ones; new tasks have none.

| # | Task | Status on 2026-11-13 | Done when | Priority |
|---|---|---|---|---|
| B1 | Update the report (`docs/technical_report.md`), architecture diagram (`docs/architecture.md`), README and deck (`docs/presentation.md`) for the real backend, Chroma retrieval and voice. Regenerate `docs/presentation.pptx`. (4h) | README updated on `rohan/voice`. The report, architecture document and deck still contain "fake" references (14, 9 and 4 lines) and do not mention Chroma, embeddings or LiveKit. Check each reference: some describe `UI_BACKEND=fake` on purpose. | No document claims the chat uses a fake backend, except where it describes `UI_BACKEND=fake` on purpose, and each describes the retrieval and voice setup as it is. | Must |
| B2 | Add the evidence to the report: the 2026-11-13 test count (1256), 28/28 verification, the Docker check, screenshots, and honest limitations (rule-based assistant, retrieval needs `OPENAI_API_KEY`, real embedding quality and the voice worker unverified, in-memory single-process sessions, provider adapters not live-tested). (2h) | Open | Every claim matches section 1 and `PROJECT_STATUS.md`. | Must |
| B3 | Evaluation write-up: A3 exists. The 70 cases use only C101 and C102; relabel or widen them if time allows, and reword the evaluation section, including the local versus production retrieval modes. (1.5h) | Open | The report says what the evaluation does and does not measure, and that local retrieval is lexical and says nothing about OpenAI embeddings. | Should |
| B4 | Docker from a clean clone of the final commit. | Docker uses the real backend (decided). `api` and `app` were started on 2026-11-13 from a clean worktree without `.env`. A fresh-clone run of the pushed commit and the `worker` are outstanding. | `docker compose up --build api app` works from a clean clone. | Should |
| B5 | UI polish: a rejected over-long message is still echoed in the chat; policy citations (`reply.citations`) come back from the backend but are not shown. (1h) | Not re-checked | Both fixed with tests, or skipped. | Optional |
| B6 | Demo rehearsal: slides, screenshots, timing, and a fallback (`UI_BACKEND=fake`) if the database fails live. Without `OPENAI_API_KEY` a policy question shows the "cannot look up" reply, so rehearse the demo you will give. (2h) | Open | One full dry run. | Must |
| B7 | Final consistency pass: README commands, test counts, `git diff --check`. (0.5h) | Open | Clean. | Should |
| B8 | Voice worker verification: run the LiveKit worker and a voice call against real services. Needs LiveKit, OpenAI and Fish Audio credentials and the technical lead's explicit authorization; no live call has been made. | Not started | A recorded result, or the report says the voice path is unverified. | Optional |

## 5. Person A: remaining tasks (for your awareness)

| # | Task | Status on 2026-11-13 | Priority |
|---|---|---|---|
| A1 | Live OpenAI verification: the guarded probe is reported to have succeeded (technical lead; the result file is not in the repository). The minimal application sequence (FAQ, owned order, unowned order, return with Confirm, ticket) on a fresh temporary database with a real provider is outstanding and needs explicit authorization. | Open | Should |
| A2 | Publish: push local `master` (24 commits ahead of `origin/master`), then decide about the stale branches (`rukum/db`, `rukum/backend` and the `rohan/*` branches). Nothing is pushed. | Waiting for approval | Must |
| A3 | Real-backend evaluation runner `scripts/eval/real_eval.py` (`66c60fe`), now with `--retrieval {local,production}`. | Done; production retrieval mode never run | Done |
| A4 | Status documents. | Refreshed again on 2026-11-13 (`docs/04` to `docs/06`, `PROJECT_STATUS.md`, `AGENTS.md`); path notes for `docs/01` to `docs/03` still need the technical lead's approval | Must |
| A5 | Demo runbook and `scripts/verify_real_backend.py` (`e8ab746`). | Done; runbook refreshed for Docker and retrieval | Done |
| A6 | Final acceptance from a clean clone; review Person B's deliverables and work. | The 2026-11-12 acceptance covers `e8ab746` only. A real-backend UI smoke test in a clean clone, an acceptance of the final commit and the review of Person B's work are outstanding. | Must |
| A7 | Real OpenAI embedding retrieval: build the index with a real key, check a handful of paraphrased questions, and record whether the 0.7 cut-off holds; optionally `real_eval.py --retrieval production --allow-live`. Needs explicit authorization for live, billed requests. | Not started | Should |

Dependencies: A2 comes first, so Person B branches from the updated `master`. B2 and B6 should state that real OpenAI retrieval and the voice worker are unverified unless A7 or B8 finish. B6 and A6 are last.

## 6. What the rule-based assistant understands

It matches fixed phrasings, so rehearse with these. The first five were exercised in the 2026-11-12 verification; the last three come from reading the code and were not part of those 20 checks.

| Say | Result |
|---|---|
| `Where is my order ORD1001?` | Order status from the database (C101). |
| `status of ORD1003` as C101 | "I couldn't find that on your account." (it is C102's order) |
| `I want to return item IT101 from order ORD1001 because I changed my mind about it` | Reason-category buttons; pick **Another reason**, then **Confirm**. A `RET_...` id appears. |
| `show my returns` / `show my tickets` | Lists the signed-in customer's own records. |
| `I want to speak to a human agent`, then a description such as `My earbuds from ORD1001 stopped charging after two days and I need help.` | Ticket proposal, then **Confirm**. A `TKT_...` id appears. |
| `my orders` | Lists the customer's orders. |
| `What is your refund policy?` | With `OPENAI_API_KEY` set, a policy passage with a source citation (live embedding requests; not run live in this project). Without the key, the assistant says it cannot look up the FAQ and policies and offers a ticket. |
| `Tell me about the wireless earbuds` | Product lookup. |

Tips:
- A return message needs a few words of reason; a bare "return IT101" makes the bot ask for one.
- Typing `cancel` cancels an open choice or confirmation.
- Limits per session: 5 returns, 3 tickets, 60 messages. A server restart ends all sessions.
- Seeded customers: C101 Alice Walker, C102 Bob Smith, C103 Charlie Patel, C104 Diana Roy, C105 Evan Sharma, C106 Fiona Das. Useful orders: ORD1001 (C101, delivered, item IT101), ORD1002 (C101, shipped, so a return is refused), ORD1003 (C102, delivered long ago, outside the window), ORD1005 (C103, two items, inside the 48-hour window).

## 7. Order of work

- First: A2 (publish) so that everything branches from the integrated `master`.
- Then in parallel: B1, B2 and B6 for Person B; A6 (acceptance of the final commit and a real-backend UI smoke test) for Person A. One dry run of the demo with both.
- If time runs out, drop in this order: B8, B5, A7, A1, B4, B3, B7. The rule-based assistant stays the demo path, and the report says that the OpenAI adapters, real embedding retrieval and the voice worker are unverified.
