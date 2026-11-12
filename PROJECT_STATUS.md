# Project Status

**Project:** AI E-Commerce Customer Support Assistant (capstone). **Deadline:** about 2026-11-13. **Snapshot date:** 2026-11-12.

**Current phase:** integration and delivery. The database layer, the backend, the Streamlit UI wired to the real backend, the OpenAI Responses API adapter and a real-backend evaluation runner are implemented and merged on `master`. What is left is verification against a live provider, documentation and demo preparation (see "Remaining work").

This file is a snapshot. Verify against files and Git rather than trusting any status text, including this one.

## Git position

- Branch `master`, working tree clean when this refresh began.
- `origin/master` is at `967cfe9` (the merge of `feature/openai-responses-adapter`, which carries the adapter commit `f25b7c4`). Both were pushed on 2026-11-12.
- **Local `master` is two commits ahead of `origin/master`:** `66c60fe` `feat(eval): add real-backend evaluation runner` (A3) and the A4 documentation refresh (`docs: refresh project status and evaluation guidance`). Both are committed locally. **Neither A3 nor A4 has been pushed.**
- Every other local and remote feature branch (`rukum/db`, `rukum/backend`, `wire-ui-backend`, `feature/openai-responses-adapter`) is an ancestor of `master`. Person B's `rohan/*` branches still exist on the remote and are stale.

## Scope decision (confirmed by student)

- Text chat via Streamlit is in scope for the MVP.
- Real-time voice is deferred and a stretch goal only. The code is modular so voice could be added later without reworking `tools/`, `db/` or `orchestrator/`.
- Data model: single `orders` table carrying `status`, `payment_status`, `refund_status`, `refund_amount`, `delivered_at`. No separate payment or shipment tables. A `return_requests` table tracks return lifecycle and a `support_tickets` table tracks tickets.

## What is built

**Architecture.** A modular Python monolith: Streamlit UI (`app/ui`), a public backend API (`app/backend`) over an orchestrator, a customer-scoped tool layer, lexical retrieval and a pluggable LLM layer, on a SQLite database (`app/db`). Details are in `CLAUDE.md` and `docs/03_backend_architecture.md`.

**Database layer (`app/db`).** Three connection factories (`APP_DB_PATH`, default `app/db/data/app_d2.db`, schema `user_version = 1`), the five-table schema, a deterministic seed (6 customers C101 to C106, 15 orders), customer-scoped repository functions, confirmed-write functions for returns and tickets, a health check and typed errors. The legacy `app/db/data/app.db` is protected and must never be opened or accessed. `app/db/data/app_d2.db` is protected too: it must not be opened, read, initialized, seeded or modified unless the technical lead explicitly authorizes that specific access. Development, tests, evaluation and UI verification use temporary or demo databases, and `APP_DB_PATH` must be checked before Streamlit is started (`.env.example` sets it to `app_d2.db`). The seed command is `python -m app.db.seed_data` (use `--db-path` for a demo file).

**Backend.** The LLM can only propose tool calls, text and an issue-relation label; the engine validates them and owns every state change. Writes happen only after an explicit confirmation on a pending action (10-minute expiry, claimed once). Sessions are in-memory and single-process. Retrieval is lexical BM25 over `FAQ.md` and `Policies.md` (no embeddings, no vector store). The default LLM is the offline `RuleBasedLLM`, a deterministic keyword stand-in and not a language model.

**LLM providers.** `LLM_PROVIDER` selects `rule_based` (default), `openai` or `anthropic`; a real provider also needs `LLM_MODEL` and its key variable, and a misconfigured provider raises `LLMConfigError` instead of falling back. The OpenAI adapter uses the **Responses API** (`POST {base}/responses`, stateless with `store: false`, native `function_call` and `function_call_output` items joined by `call_id`, incomplete responses rejected). It was reviewed, corrected and committed as `f25b7c4`, merged into `master` as `967cfe9` and pushed. The Anthropic adapter flattens tool exchanges to text. Provider failures raise `LLMError` with a `category`.

**UI and integration.** The Streamlit UI talks to the real backend by default through `app/ui/client.py` (`UI_BACKEND=real`); `UI_BACKEND=fake` keeps the canned demo. The startup gate, login from `list_login_options()`, choice and confirmation buttons and error handling are in place. The real backend was verified on 2026-11-12 against a temporary seeded database: 20 of 20 scripted checks and 7 of 7 browser checks. **Docker still runs the fake-backend UI** (`UI_BACKEND=fake` in `docker-compose.yml`); the image holds no database.

**Evaluation.** Two runners, documented in `docs/04_frontend_handover.md` Section 10:
- `scripts/eval/run_eval.py`: fake UI backend only; every score is `not_assessed`.
- `scripts/eval/real_eval.py` (A3, `66c60fe`): runs the real `app.backend.Backend` on a temporary seeded database with 70 cases and 5 multi-turn scenarios, offline with `RuleBasedLLM` by default.

## Latest measured results (2026-11-12)

Commands were run offline (`--offline --no-sync`); no live calls and no database other than temporary ones.

| Run | Command | Result |
|---|---|---|
| Targeted | `uv run --offline --no-sync python -m pytest tests/test_real_eval.py tests/test_eval_runner.py -q` | **59 passed** (53 real-runner tests, 6 fake-runner tests) |
| Full suite | `uv run --offline --no-sync python -m pytest tests -q -p net_guard_plugin` | **1172 passed**, 0 failed, 0 skipped; the audit plugin counted 0 external network attempts (it allowed 26 loopback connections, asyncio's Windows self-pipe) |

`net_guard_plugin` is a scratch audit plugin kept outside the repository; the documented command is `uv run python -m pytest tests -q`. Earlier figures (939, 1023, 1028, 1119, 1143) are historical.

**A3 offline results (`RuleBasedLLM`, 70 cases).** These measure a deterministic keyword stand-in against mechanical rules. They are **not live-provider results and not response-quality results.**

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

All 5 multi-turn scenarios pass. No case wrote without confirmation. Semantic answer quality is not scored (70 of 70 `not_assessed`). Two runs of the offline runner produced byte-identical output before the check-description edits; the in-suite reproducibility test still passes.

**A1 live probe.** Reported by the technical lead: a single guarded live request to the OpenAI Responses endpoint succeeded and returned the expected sentinel response. It confirms only that the endpoint accepts one request and returns text in the expected shape. It is **not** an application evaluation: no tool calling, no multi-turn `function_call_output` round trip, no confirmation flow and no cases were run live. The result file is not in the repository and was not re-run for this refresh. Full live application and tool-calling evaluation remains outstanding and needs separate explicit authorization.

## Remaining work

- **Live evaluation (A1, remainder):** run the real backend with a real provider through `real_eval.py --llm env --allow-live`, only after explicit authorization, and review replies by hand. Nothing has been verified about real-model tool selection or answer quality.
- **A5:** demo runbook (reseed steps, phrasing cheat-sheet, the scripted verification kept as a script).
- **A6:** final acceptance from a clean clone and review of Person B's work.
- **Publish:** push the two local commits (A3 `66c60fe` and the A4 documentation commit) when approved. Neither has been pushed.
- **Person B (`docs/05_handover_person_b.md`):** report, diagram, README and deck still describe the fake backend; Docker from a clean clone and a fake-versus-real decision; evaluation write-up; rehearsal; consistency pass.
- **Stale remote branches:** `rohan/*` and the already-merged feature branches; decide whether to delete.
- **Path and content corrections in `docs/01` to `docs/03`:** listed for approval, not made (see the A4 review notes).

## Known limitations

- `RuleBasedLLM` is a keyword stand-in. Offline evaluation does **not** establish real LLM tool selection or response quality.
- Some `queries.csv` labels are demo-era and outdated (for example cancellation and policy cases labelled tool `none`, injection cases requiring a return tool). Mismatches are reported as failures, not hidden.
- `tool_selection` can penalize safe refusals: for cross-customer and injection cases it requires the model to propose a lookup, so a real model that refuses without a tool call would score a failure.
- `owned_fact_grounding` matches the status word exactly (case-insensitive substring), so a paraphrase would be scored as a failure.
- Contact details can leak: `FAQ.md` Q6.2 and `Policies.md` Section 7 contain the placeholder `support@example.com`, which the backend returns unredacted (Q033 and others).
- Assistant and guard behavior gaps exposed by the evaluation: **Q034** ("Can you cancel ORD1002?") is not recognized as an unsupported cancel because the pattern needs the word "order"; **Q070** ("The charger is faulty yaar") is not clarified because a catalog product name makes the message "specific". Not fixed.
- The result-file secret scan checks only `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`, matches the serialized JSON text (so a value that JSON escapes would be missed), and does not scan stderr or logs.
- `live_requests_made` in a result file records authorization (`--allow-live` with a real provider), not an independently observed request count.
- Isolation and grounding checks are string heuristics; semantic correctness needs human review. Cases use only customers C101 and C102. Clocks are fixed, so 10-minute expiry is not exercised.
- Sessions and pending actions are in memory (one process); retrieval is lexical; login is simulated.
- `scripts/init_demo_db.py` was fixed (`c9035c5`); the earlier breakage note no longer applies.

## Open decisions

Recorded in `docs/01_database_design.md` Section L. Not re-verified in this refresh: simulated-login approach, synthetic data volume, a non-returnable product category with no current seed product, and explicit sign-off of the status and priority enums.

## History (compact)

- Phase 0 discovery (`docs/00`), Phase 1 database design (`docs/01`) and Phase 2 implementation plan (`docs/02`) were approved. Database checkpoints 1 to 10 and the D2 contract (D3 to D8) were completed and merged, commit `7dfd2f0` among them.
- The backend (tools, sessions, scope guard, issue lifecycle, ticket summaries, BM25 retrieval, LLM layer, `app.backend`) was implemented on `rukum/backend`; Person B's UI, Docker, evaluation and report work were merged with it (merge commit `3b68b12`).
- The UI was wired to the real backend on `wire-ui-backend` (plan: `docs/plans/ui_real_backend_plan.md`), data moved to `app/db/data/`, `.env` loading added, Docker configuration cleaned.
- 2026-11-12: OpenAI Responses adapter (`f25b7c4`), its merge (`967cfe9`), real-backend evaluation runner (`66c60fe`).
