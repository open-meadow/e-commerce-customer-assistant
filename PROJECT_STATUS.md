# Project Status

**Project:** AI E-Commerce Customer Support Assistant (capstone). **Deadline:** about 2026-11-13. **Snapshot date:** 2026-11-13.

**Current phase:** integration and delivery. The database layer, the backend, the Streamlit UI wired to the real backend, the OpenAI Responses API adapter, a real-backend evaluation runner and (on local `master`) Person B's voice work with Chroma retrieval are implemented. What is left is publishing, verification against live services (none has been done for embeddings, the LLM application flow or voice), the remaining Person B documents and demo preparation (see "Remaining work").

This file is a snapshot. Verify against files and Git rather than trusting any status text, including this one.

## Git position

- Local `master` is at `fdb372a`, the **merge of `rohan/voice`** (parents `01adb32` `.gitattributes` and `11e2b1c`). `origin/master` is at `967cfe9` (the merge of `feature/openai-responses-adapter`), so **local `master` is 24 commits ahead of `origin/master` and nothing has been pushed**: the four delivery commits A3 `66c60fe` (real-backend evaluation runner), A4 `746b9b1` (documentation refresh), A5 `e8ab746` (`scripts/verify_real_backend.py`, its tests and the demo runbook) and A6 `a0a0410` (clean-clone acceptance and status refresh); the 18 commits of `rohan/voice` (Person B's Chroma retrieval, LiveKit voice, Docker and UI work); `01adb32`, which adds `.gitattributes` with `*.sh text eol=lf`; and the merge `fdb372a`.
- `fdb372a` also contains the retrieval testability refactor (injectable embeddings, `app/rag/local_embeddings.py`, the new tests) and the hand-merged `CLAUDE.md`. The documentation refresh of 2026-11-13 (`docs/04` to `docs/06`, this file and `AGENTS.md`) is **not** part of `fdb372a`; it is a separate change.
- At the time of writing `master` was not checked out in any worktree: the worktree `D:\ecommerce-integrate` is on the branch `integrate/rohan-voice` (same commit as `master`), and the original worktree is on `rohan/voice` (still `11e2b1c`) with uncommitted copies of files that are now in the merge. Neither branch was changed by the integration.
- Other local and remote feature branches (`rukum/db`, `rukum/backend`, `wire-ui-backend`, `feature/openai-responses-adapter`) are ancestors of `master`. Person B's older `rohan/*` branches still exist on the remote and are stale.

## Scope decision (confirmed by student)

- Text chat via Streamlit is in scope for the MVP and is the demo path.
- Real-time voice was a stretch goal. Person B's LiveKit voice code is now merged (`app/voice`, `app/backend/http_api.py`, `app/ui/livekit_*.py`) but has **never been run** against LiveKit, OpenAI speech or Fish Audio; it is not part of the verified MVP. The code is modular, so voice does not touch `tools/`, `db/` or `orchestrator/`.
- Data model: single `orders` table carrying `status`, `payment_status`, `refund_status`, `refund_amount`, `delivered_at`. No separate payment or shipment tables. A `return_requests` table tracks return lifecycle and a `support_tickets` table tracks tickets.

## What is built

**Architecture.** A modular Python monolith: Streamlit UI (`app/ui`), a public backend API (`app/backend`) over an orchestrator, a customer-scoped tool layer, Chroma retrieval and a pluggable LLM layer, on a SQLite database (`app/db`). A FastAPI service (`app/backend/http_api.py`) exposes sessions for voice. Details are in `CLAUDE.md` and `docs/03_backend_architecture.md`.

**Database layer (`app/db`).** Three connection factories (`APP_DB_PATH`, default `app/db/data/app_d2.db`, schema `user_version = 1`), the five-table schema, a deterministic seed (6 customers C101 to C106, 15 orders), customer-scoped repository functions, confirmed-write functions for returns and tickets, a health check and typed errors. The legacy `app/db/data/app.db` is protected and must never be opened or accessed. `app/db/data/app_d2.db` is protected too: it must not be opened, read, initialized, seeded or modified unless the technical lead explicitly authorizes that specific access. Development, tests, evaluation and UI verification use temporary or demo databases, and `APP_DB_PATH` must be checked before Streamlit is started (`.env.example` sets it to `app_d2.db`). The seed command is `python -m app.db.seed_data` (use `--db-path` for a demo file).

**Backend.** The LLM can only propose tool calls, text and an issue-relation label; the engine validates them and owns every state change. Writes happen only after an explicit confirmation on a pending action (10-minute expiry, claimed once). Sessions are in-memory and single-process. The default LLM is the offline `RuleBasedLLM`, a deterministic keyword stand-in and not a language model.

**Retrieval.** Policy and FAQ retrieval is a persistent Chroma index over `FAQ.md` and `Policies.md` with OpenAI `text-embedding-3-small` embeddings (`app/rag/vector_index.py`; cosine-distance cut-off 0.7; index in `VECTOR_STORE_DIR`). It is a **separate provider requirement from the LLM**: production retrieval always needs `OPENAI_API_KEY`, even with `LLM_PROVIDER=rule_based`. There is **no silent fallback to BM25**: without a key the policy tools report `unavailable`, the assistant says it cannot look up the FAQ and policies and offers a ticket, and orders, returns and tickets keep working. Importing modules never needs a key or the network. The lexical `KnowledgeBase` class remains but the default retriever does not use it. The embedding function is injectable (`app.rag.configure_embeddings`); tests and the offline evaluation inject `app/rag/local_embeddings.py`, a deterministic BM25-weighted vector embedding that is **lexical, not semantic** and says nothing about OpenAI embedding quality. Details: `docs/07_retrieval_and_embeddings.md`.

**LLM providers.** `LLM_PROVIDER` selects `rule_based` (default), `openai` or `anthropic`; a real provider also needs `LLM_MODEL` and its key variable, and a misconfigured provider raises `LLMConfigError` instead of falling back. The OpenAI adapter uses the **Responses API** (`POST {base}/responses`, stateless with `store: false`, native `function_call` and `function_call_output` items joined by `call_id`, incomplete responses rejected). It was reviewed, corrected and committed as `f25b7c4`, merged into `master` as `967cfe9` and pushed. The Anthropic adapter flattens tool exchanges to text. Provider failures raise `LLMError` with a `category`. `OPENAI_BASE_URL` applies to the LLM adapter only, not to embeddings.

**UI, Docker and voice.** The Streamlit UI talks to the real backend by default through `app/ui/client.py` (`UI_BACKEND=real`); `UI_BACKEND=fake` keeps the canned demo. The startup gate, login from `list_login_options()`, choice and confirmation buttons and error handling are in place. **Docker Compose now runs the real backend** with three services: `api` (FastAPI, port 8000 not published, seeds `/app/data/app_d2.db` in the named volume `app-data` on its first start, health check on `/openapi.json`), `app` (Streamlit on port 8501, `UI_BACKEND=real`, text chat in process) and `worker` (`python -m app.voice.agent start`, which exits without `LIVEKIT_URL`). Each service declares an optional `env_file: .env` (`required: false`), so Compose starts without one; when it exists, all its variables reach the containers at runtime (it is not copied into the image) except those Compose sets itself. The image contains no database. The real backend UI was verified on 2026-11-12 against a temporary seeded database: 20 of 20 scripted checks and 7 of 7 browser checks (historical).

**Evaluation.** Two runners, documented in `docs/04_frontend_handover.md` Section 10:
- `scripts/eval/run_eval.py`: fake UI backend only; every score is `not_assessed`.
- `scripts/eval/real_eval.py` (A3, `66c60fe`): runs the real `app.backend.Backend` on a temporary seeded database with 70 cases and 5 multi-turn scenarios, offline with `RuleBasedLLM` by default. Options: `--cases`, `--output`, `--llm {rule_based,env}` (default `rule_based`), `--allow-live`, `--retrieval {local,production}` (default `local`) and `--compare FIRST SECOND`. `--retrieval local` injects the lexical test embedding into the production Chroma code; `--retrieval production` uses OpenAI embeddings and requires `--allow-live` and `OPENAI_API_KEY` (it is refused without `--allow-live`). Every report records its retrieval mode (`embedding_quality_assessed` is always false), and `--compare` shows and compares the LLM mode and the retrieval mode of two reports.

**Demo and scripted verification (A5, `e8ab746`).** `scripts/verify_real_backend.py` runs 28 business-behaviour checks (customer scoping, return eligibility and rejection, explicit confirmation, exact reason, duplicate protection, ticket flow, customer-scoped retrieval, final database state) against a fresh temporary database with `RuleBasedLLM` only, refuses to open the protected databases and requires zero network attempts; it asks no policy questions. `tests/test_verify_real_backend.py` tests the script (9 tests). `docs/06_demo_runbook.md` is the demo runbook (refreshed 2026-11-13 for the Docker services and the retrieval requirement).

## Latest measured results

All offline: no live calls, no `.env`, no database other than temporary ones, credentials unset.

**2026-11-13, integration of `rohan/voice` (the tree committed as `fdb372a`).**

| Run | Command | Result |
|---|---|---|
| Full suite | `uv run --offline --frozen python -m pytest tests -q -p net_guard_plugin` (temporary environment, `PYTHON_DOTENV_DISABLED=1`, provider and LiveKit variables blank) | **1256 passed**, nothing failed or skipped; the audit plugin counted **0 external network attempts** (it allowed 26 loopback connections, asyncio's self-pipe) |
| Scripted verification | `uv run --offline --frozen python scripts/verify_real_backend.py` | **28/28 checks passed**, exit code 0 |
| Offline evaluation | `real_eval.py` (defaults: `rule_based`, `--retrieval local`) | the same pass counts and failing case IDs as the table below |
| Docker | build, then `up -d api app` in a separate worktree without `.env`, separate Compose project name | build succeeded; `api` healthy; `app` running; Streamlit `/_stcore/health` `ok`; volume held `app_d2.db` (6 customers, schema version 1); no traceback in the logs; project removed afterwards. The `worker` was built but **not started** |

`net_guard_plugin` is a scratch audit plugin kept outside the repository; the documented command is `uv run python -m pytest tests -q`. The suite never needs a key or the network. Earlier figures (939, 1023, 1028, 1119, 1143, 1172 and 1181) are historical; 1256 is 75 more than the 1181 measured at A6 (mainly the new retrieval, telemetry, import-without-credentials and evaluation tests; the exact breakdown was not recorded).

**2026-11-12 (historical).** The A3 targeted run `tests/test_real_eval.py tests/test_eval_runner.py` gave 59 passed. The A6 clean-clone acceptance of `e8ab746` gave **1181 passed** in 42.52 s and 28/28 verification checks, plus a fake-backend Streamlit smoke test on loopback (health `ok`, demo login page) that does not cover the real-backend UI or any language model. Nothing was pushed in A6 and Docker was not run then.

**Offline evaluation results (`RuleBasedLLM`, `--retrieval local`, 70 cases).** First measured on 2026-11-12 with the BM25 retriever and reproduced on 2026-11-13 on the integrated code. These measure a deterministic keyword stand-in against mechanical rules through a lexical test embedding. They are **not live-provider results, not response-quality results and not evidence about OpenAI embeddings.**

| Check | Tag | Passed |
|---|---|---|
| `no_unconfirmed_write` | backend_code | 70/70 |
| `scope_guard` | backend_code | 3/3 |
| `no_false_success_claim` | backend_code | 70/70 |
| `reply_well_formed` | provider_and_backend | 70/70 |
| `confirmation_gate` | llm_proposals | 3/3 |
| `tool_selection` | llm_proposals | 26/28 (Q016, Q058) |
| `no_unexpected_tool` | llm_proposals | 36/42 (Q018, Q034, Q035, Q039, Q041, Q070) |
| `cross_customer_isolation` | llm_text | 10/10 |
| `owned_fact_grounding` | llm_text | 9/9 |
| `grounded_identifiers` | llm_text | 70/70 |
| `no_invented_contact_details` | llm_text | 65/70 (Q033, Q045, Q048, Q055, Q057) |

All 5 multi-turn scenarios pass. No case wrote without confirmation. Semantic answer quality is not scored (70 of 70 `not_assessed`).

**A1 live probe.** Reported by the technical lead: a single guarded live request to the OpenAI Responses endpoint succeeded and returned the expected sentinel response. It confirms only that the endpoint accepts one request and returns text in the expected shape. It is **not** an application evaluation: no tool calling, no multi-turn `function_call_output` round trip, no confirmation flow and no cases were run live. The result file is not in the repository and was not re-run. Full live application and tool-calling evaluation remains outstanding and needs separate explicit authorization.

## Remaining work

- **Publish:** push local `master` (24 commits ahead of `origin/master`) when approved. Nothing has been pushed. Decide about the stale remote branches (`rohan/*` and the already-merged feature branches).
- **Verify real OpenAI embedding retrieval (not started):** with a real `OPENAI_API_KEY`, build the index, check paraphrased questions and decide whether the 0.7 cut-off suits real vectors. Optionally `real_eval.py --retrieval production --allow-live`. Live, billed requests: explicit authorization required.
- **Live LLM evaluation (A1, remainder):** run the real backend with a real provider through `real_eval.py --llm env --allow-live`, only after explicit authorization, and review replies by hand. Nothing has been verified about real-model tool selection or answer quality.
- **Verify the voice worker (not started):** needs LiveKit, OpenAI speech and Fish Audio credentials and explicit authorization.
- **A6, remainder:** a real-backend UI smoke test in a clean clone, an acceptance run of the final pushed commit, and the review of Person B's work. The 2026-11-12 acceptance covers `e8ab746` only, and the Docker check above was run in a worktree, not a clone.
- **Person B (`docs/05_handover_person_b.md`):** the report, architecture document and deck still describe the fake backend and do not mention Chroma retrieval or voice; Docker from a clean clone; evaluation write-up; rehearsal; consistency pass.
- **Path and content corrections in `docs/01` to `docs/03`:** listed for approval, not made.

## Known limitations

- **Retrieval needs `OPENAI_API_KEY` and has never been run against real OpenAI embeddings.** Paraphrase handling and the 0.7 distance cut-off are unmeasured. All automated retrieval tests and the offline evaluation use the lexical local embedding, which proves wiring (indexing, ranking plumbing, citations, failure handling), not semantic quality.
- **The LiveKit voice worker and the FastAPI voice sessions have never been run** against live services. The text chat does not depend on them.
- `RuleBasedLLM` is a keyword stand-in. Offline evaluation does **not** establish real LLM tool selection or response quality.
- Docker Compose passes the whole `.env` (when present) to every service at runtime, so a key in `.env` reaches the containers and makes policy questions send live embedding requests. Person B accepted this configuration. The Streamlit and Chroma telemetry settings are the voice branch's accepted production configuration and were not changed (tests force Chroma telemetry off).
- Some `queries.csv` labels are demo-era and outdated (for example cancellation and policy cases labelled tool `none`, injection cases requiring a return tool). Mismatches are reported as failures, not hidden.
- `tool_selection` can penalize safe refusals: for cross-customer and injection cases it requires the model to propose a lookup, so a real model that refuses without a tool call would score a failure.
- `owned_fact_grounding` matches the status word exactly (case-insensitive substring), so a paraphrase would be scored as a failure.
- Contact details can leak: `FAQ.md` Q6.2 and `Policies.md` Section 7 contain the placeholder `support@example.com`, which the backend returns unredacted (Q033 and others).
- Assistant and guard behavior gaps exposed by the evaluation: **Q034** ("Can you cancel ORD1002?") is not recognized as an unsupported cancel because the pattern needs the word "order"; **Q070** ("The charger is faulty yaar") is not clarified because a catalog product name makes the message "specific". Not fixed.
- The result-file secret scan checks only `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`, matches the serialized JSON text (so a value that JSON escapes would be missed), and does not scan stderr or logs. `live_requests_made` in a result file records LLM-provider authorization (`--allow-live` with a real provider), not an observed request count and not embedding requests; use the report's `retrieval` section for the retrieval mode.
- Isolation and grounding checks are string heuristics; semantic correctness needs human review. Cases use only customers C101 and C102. Clocks are fixed, so 10-minute expiry is not exercised.
- Sessions and pending actions are in memory (one process); login is simulated.
- `scripts/init_demo_db.py` was fixed (`c9035c5`); the earlier breakage note no longer applies.

## Open decisions

Recorded in `docs/01_database_design.md` Section L. Not re-verified in this refresh: simulated-login approach, synthetic data volume, a non-returnable product category with no current seed product, and explicit sign-off of the status and priority enums. Also open: who owns `app/backend/http_api.py` and `app/rag/vector_index.py` (written by Person B in Person A's directories; see `docs/05_handover_person_b.md` Section 3).

## History (compact)

- Phase 0 discovery (`docs/00`), Phase 1 database design (`docs/01`) and Phase 2 implementation plan (`docs/02`) were approved. Database checkpoints 1 to 10 and the D2 contract (D3 to D8) were completed and merged, commit `7dfd2f0` among them.
- The backend (tools, sessions, scope guard, issue lifecycle, ticket summaries, BM25 retrieval, LLM layer, `app.backend`) was implemented on `rukum/backend`; Person B's UI, Docker, evaluation and report work were merged with it (merge commit `3b68b12`).
- The UI was wired to the real backend on `wire-ui-backend` (plan: `docs/plans/ui_real_backend_plan.md`), data moved to `app/db/data/`, `.env` loading added, Docker configuration cleaned.
- 2026-11-12: OpenAI Responses adapter (`f25b7c4`), its merge (`967cfe9`), real-backend evaluation runner (`66c60fe`), documentation refresh (`746b9b1`), verification script and demo runbook (`e8ab746`), clean-clone acceptance (A6, `a0a0410`).
- 2026-11-13: Person B's `rohan/voice` branch (Chroma retrieval with OpenAI embeddings, LiveKit voice, Docker services) was integrated into local `master` as `fdb372a`, together with the injectable-embedding testability refactor (the BM25-to-Chroma change made the suite depend on `OPENAI_API_KEY`; embeddings are now injectable and tests use a lexical local embedding) and `.gitattributes` (`01adb32`). Verified offline: 1256 tests, 28/28, Docker `api` and `app`. Nothing pushed.
