# Handover to Person B

**From:** Person A (Rukum, backend and AI). **To:** Person B (frontend and delivery).
**Written against:** `master` at `c9035c5` plus one local, unpublished branch (see section 1), on 2026-11-12. **Deadline:** about 2026-11-13. The task split in sections 4 and 5 was refreshed the same day so that each of us has about 13.5 hours of work (estimates).

This note says what is finished, how to get running in ten minutes, who owns what, and what is left for each of us. It is a status note, not a design document. The contracts are `docs/04_frontend_handover.md` (UI to backend) and `docs/03_backend_architecture.md` (backend design).

## 1. What is finished

- The database layer, the backend and the Streamlit UI are merged on `master`. The UI uses the **real backend by default** (`UI_BACKEND=real`); `UI_BACKEND=fake` keeps the old canned demo.
- Full test suite on `master`: **1028 passed, 0 failed** (`uv run python -m pytest tests -q`). The local feature branch below brings it to 1119.
- Person A has an OpenAI Responses API adapter committed on the local branch `feature/openai-responses-adapter` (`f25b7c4`, mocked tests only, not yet pushed or merged). A first live one-request probe was prepared but has not run, so nothing has been verified against the real OpenAI API.
- The real backend was verified on 2026-11-12 against a temporary seeded database: 20 of 20 scripted checks (login, ownership isolation, return rejections and creation, duplicate protection, ticket creation and retrieval) and seven browser checks on the running Streamlit app.
- The assistant is the offline `RuleBasedLLM`, a deterministic stand-in, not a language model. No network or API key is needed. The OpenAI and Anthropic adapters exist but have **never been run against a live service**.
- Docker builds and serves the **fake-backend** UI on port 8501 (`UI_BACKEND=fake` in `docker-compose.yml`). The image holds no database.

## 2. Get running

```bash
git pull
uv sync
cp .env.example .env
uv run python -m pytest tests -q
```

Create your own database (never reuse someone else's; the parent folder must exist, the file must not). Use the **current UTC time** as the reference time, never a future one:

```bash
uv run python -m app.db.seed_data --db-path <your path>.db --reference-time <now, e.g. 2026-11-12T10:00:00Z>
```

Run the UI against it. `.env` points `APP_DB_PATH` at `app/db/data/app_d2.db`, so always set `APP_DB_PATH` explicitly for a scratch database.

```bash
APP_DB_PATH=<your path>.db uv run python -m streamlit run app/ui/streamlit_app.py
```

PowerShell: `$env:APP_DB_PATH = "<your path>.db"` first. Add `LLM_PROVIDER=rule_based` if you want to be certain no provider is used. `UI_BACKEND=fake` runs without any database.

- The seed is not idempotent: to reseed, delete your file and run the command again.
- The 48-hour damaged scenario (ORD1005, customer C103) is valid for about 12 hours after the reference time. Reseed on demo day.
- Never open, seed or modify `app/db/data/app.db` (protected legacy file). `app/db/data/app_d2.db` is the working database; use a scratch file for experiments.

## 3. Ownership (to avoid merge conflicts)

| Person B owns | Person A owns |
|---|---|
| `app/ui`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `scripts/`, `app/db/data/eval`, README, `docs/architecture.md`, `docs/technical_report.md`, `docs/presentation.*` | `app/db`, `app/backend`, `app/orchestrator`, `app/tools`, `app/llm`, `app/rag`, `app/catalog`, their tests, `docs/01` to `docs/04`, `AGENTS.md`, `PROJECT_STATUS.md` |

Working rules:
- Branch from `master` (for example `rohan/docs-real-backend`), keep commits small, and run the full suite before asking for a merge.
- If you need a change in Person A's area, ask; do not edit it. The UI imports only `app.backend` (through `app/ui/client.py`), never `app.db` or `sqlite3`.
- `.env` is git-ignored and may hold keys. Never commit it or paste its contents anywhere.
- Do not modify `Products.json`, `FAQ.md` or `Policies.md`.

## 4. Person B: remaining tasks (about 13.5 hours)

| # | Task | Est. | Done when | Priority |
|---|---|---|---|---|
| B1 | Update the report (`docs/technical_report.md`), architecture diagram (`docs/architecture.md`), README and deck (`docs/presentation.md`) for the real backend. Regenerate `docs/presentation.pptx`. | 4h | No document claims the chat uses a fake backend, except where it describes `UI_BACKEND=fake` on purpose. | Must |
| B2 | Add the new evidence to the report: test counts, the real-backend verification above, screenshots, and honest limitations (rule-based assistant, lexical BM25 retrieval, in-memory single-process sessions, provider adapters not live-tested unless A1 finishes). | 2h | Every claim matches section 1. | Must |
| B3 | Evaluation set and write-up: the 70 cases use only C101 and C102. After Person A's A3 exists, relabel or widen them and reword the evaluation section. | 1.5h | The report says what the evaluation does and does not measure. | Should |
| B4 | Docker: build and run from a fresh clone, then decide fake or real. If real, agree the environment contract with Person A (database on a volume, a one-off seed step, `LLM_PROVIDER` default `rule_based`). One transient DNS failure occurred during a build; a retry succeeded. | 2.5h | `docker compose up` works from a clean clone. | Should |
| B5 | UI polish: a rejected over-long message is still echoed in the chat; policy citations (`reply.citations`) come back from the backend but are not shown. | 1h | Both fixed with tests, or skipped. | Optional |
| B6 | Demo rehearsal: slides, screenshots, timing, and a fallback (`UI_BACKEND=fake`) if the database fails live. | 2h | One full dry run. | Must |
| B7 | Final consistency pass: README commands, test counts, `git diff --check`. | 0.5h | Clean. | Should |

## 5. Person A: remaining tasks (about 13.5 hours, for your awareness)

| # | Task | Est. | Priority |
|---|---|---|---|
| A1 | Live OpenAI verification: apply the approved probe fixes, run the guarded one-request probe, then a minimal sequence (FAQ, owned order, unowned order, return with Confirm, ticket) on a fresh temporary database. | 3.5h | Should |
| A2 | Publish and merge: push the OpenAI adapter branch, merge to `master`, push this note, and decide what to do with the stale remote branches (`rukum/db`, `rukum/backend`, the `rohan/*` branches). | 1.5h | Must |
| A3 | Real-backend evaluation: add a real mode to `scripts/eval/run_eval.py` against a seeded temporary database, rerun it, and record which metrics are measurable. Today it runs only the fake backend and every score is `not_assessed`. | 4h | Must (at least the limitation) |
| A4 | Refresh status docs: `PROJECT_STATUS.md` (still says checkpoint 3 is next), `AGENTS.md` (backend "uncommitted", work "on `rukum/db`"), `CLAUDE.md` (known-breakage and test-count lines), `docs/04` ("OpenAI-compatible endpoint"). Path notes for `docs/01` to `docs/03` need the technical lead's approval. | 1.5h | Must |
| A5 | Demo runbook: reseed steps, the phrasing cheat-sheet (section 6), and the scripted verification kept as a script. | 1.5h | Must |
| A6 | Final acceptance from a clean clone; review Person B's pull requests. | 1.5h | Must |

Dependencies: A2 comes first, so Person B branches from the updated `master`. B3 waits for A3. B2 should include A1's live results if they exist; otherwise it states that the provider adapters are mock-tested only. B6 and A6 are last.

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
| `What is your refund policy?` | Policy passage with a source citation. |
| `Tell me about the wireless earbuds` | Product lookup. |

Tips:
- A return message needs a few words of reason; a bare "return IT101" makes the bot ask for one.
- Typing `cancel` cancels an open choice or confirmation.
- Limits per session: 5 returns, 3 tickets, 60 messages. A server restart ends all sessions.
- Seeded customers: C101 Alice Walker, C102 Bob Smith, C103 Charlie Patel, C104 Diana Roy, C105 Evan Sharma, C106 Fiona Das. Useful orders: ORD1001 (C101, delivered, item IT101), ORD1002 (C101, shipped, so a return is refused), ORD1003 (C102, delivered long ago, outside the window), ORD1005 (C103, two items, inside the 48-hour window).

## 7. Timeline

- End of today: A2 done; B1 drafted; A4 and A5 done.
- Tomorrow morning: B2 and B6 finished; one clean-clone run (Person A) and one dry run of the demo (both).
- The Must items are about 10 hours for Person A (A2 to A6) and 8 hours for Person B (B1, B2, B6), which is still heavy for one day. If time runs out, drop in this order: B5, A1, B4, B3, B7. The rule-based assistant stays the demo path, and the report says the OpenAI adapter is mock-tested only if A1 is dropped.
