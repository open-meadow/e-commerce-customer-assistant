# Project handover: e-commerce customer-support assistant

Branch `rukum/corrective-v2` (kept locally; its content reached `master` through the sanitized branch `integrate/corrective-v2-public`, PR #2, 2026-11-19). Written at the end of the corrective-v2 cycle. For the full
evaluation evidence see `docs/evaluation/EVALUATION_REPORT.md`; for wording that needs a human reader see
`docs/localization_review_hinglish_hindi.md` and `.csv`.

## 0. Status at a glance (read this first)

| Category | What it covers | Evidence |
|---|---|---|
| **Live-validated** (real `gpt-4o-mini`, replies read by hand) | The behaviour of code `8fafd95` (Evaluations A and B); the first post-fix check of `bccbc12` (language switching, policy routing, refund lookup, N01/N02); the final check of `78f9790` (29 requests, 12 conversations all passed): named orders not on the account (H10, C027, N01, N02, H09, E07), the repeat-complaint order list (C003), Hinglish ticket question (C023), S02 and S03 | `docs/evaluation/EVALUATION_REPORT.md` sections 3 to 8 |
| **Offline-tested only** (3137 tests; NOT validated against the live model) | Everything added after `78f9790`: refund-status wording replacement; the whole localised return journey; the ambiguity guards (returns, eligibility, tickets in `41c1c9c`; status, refund and listing, and the "ask before any tool runs" rule in `4462cb5`); issue-management, greeting, thanks and scope localisation; Hindi and Hinglish courtesy detection; order IDs written `ORD-1010`; number-word and Hindi-unit durations | `tests/test_final_cycle.py`, `tests/test_handover_cycle.py` and the older files; offline tests show the code does what it says, not that the model behaves |
| **Human-review pending** | All 105 Hinglish and Hindi sentences, drafts written by the assistant; **13 are High priority and are awaiting fluent human review** (`lead_limit`, `created`, `not_on_account`, `which_order`, `grounding`, `reason_notice`, `return_card_lead`, `return_card_summary`, `return_submitted`, `ticket_offer_review`, `refund_pending`, `refund_processed`, `refund_failed`). Also every manual verdict in the evaluation report | `docs/localization_review_hinglish_hindi.md` and `.csv` |
| **Remaining known limitations** | Section 5 below | |

## 1. What this is

A customer-support assistant for a synthetic online electronics store. A customer signs in (simulated), chats in English,
Hinglish or Hindi, and the assistant answers from the customer's own orders, returns, refunds, tickets and the store's FAQ and
policies, and can start a **return request** or a **support ticket** after the customer explicitly confirms. Data is synthetic
only. The assistant is a language model that *proposes*; code *decides*.

## 2. Architecture

```
Streamlit chat (app/ui)  or  FastAPI (app/backend/http_api.py)  or  LiveKit voice (app/voice, never run live)
        |
        v
app.backend.Backend  ---- sessions in memory, bearer-handle ConversationState, 10-minute prompts
        |
        v
app/orchestrator/engine.py   the tool loop and every state change
   |-- guard.py              language, scope, order-ID extraction, refund and duration wording checks (code, no model)
   |-- issues.py             issue lifecycle (active / on hold)
   |-- summarizer.py         ticket summary
   |
   |-- app/llm               LLMClient: OpenAI Responses, Anthropic, offline RuleBasedLLM (a deterministic stand-in)
   |-- app/tools             registry.py (10 tools, schemas, notes), flow_texts.py (85+ fixed sentences, 3 languages),
   |                         return_texts.py (eligibility paragraphs with times)
   |-- app/rag               BM25-free: Chroma vector index over FAQ.md and Policies.md, OpenAI embeddings
   v
app/db  repository.py (the authorization boundary, SQL filters on the signed-in customer), schema.py (SQLite, 5 tables),
        seed_data.py, connection.py (three factories; never creates a file by accident), health.py
```

| Component | Responsibility | Where |
|---|---|---|
| Engine | Validates every model proposal; runs tools; owns confirmation, expiry, cancellation; decides which reply wording is code-owned | `app/orchestrator/engine.py` |
| Guard | Language of the customer (`decisive_language`, `customer_language`), scope, order IDs (`order_ids_in`), refund and duration wording, product matching | `app/orchestrator/guard.py` |
| Tools | `list_my_orders`, `get_order_status`, `get_my_returns`, `get_my_tickets`, `get_refund_policy`, `check_return_eligibility`, `lookup_product`, `search_policies`, `start_return_request`, `start_support_ticket`. No schema takes a customer ID | `app/tools/registry.py` |
| Catalogue | Every fixed sentence the engine speaks, English, Hinglish, Hindi, one file | `app/tools/flow_texts.py` |
| Repository | Every function takes the authenticated customer ID and filters on it in SQL; writes use `BEGIN IMMEDIATE` and recheck eligibility | `app/db/repository.py` |
| Evaluation tooling | Real-backend harness, retry runner, usage ledger, transport retry, attempt composition | `scripts/eval/` |

### Safety properties (all enforced in code, all tested)

* **Ownership**: a customer sees only their own data. An order that does not exist and another customer's order return the same
  `not_found`. A typed order ID the model ignored is looked up by code; if it is not theirs the reply is fixed text that offers
  (never lists) their own orders.
* **Confirmation**: nothing is written until the customer presses Confirm on a card bound to the session (10-minute expiry,
  claimed once). A return reason is the customer's own message, stored exactly.
* **No promises**: a sentence promising a refund, or saying one arrived, is replaced by the refund status said plainly; a duration
  no policy or tool result gave is removed; the closing-deadline and photo statements are appended by code.
* **No guessing among look-alikes**: when the customer typed no order ID, named a product, and two or more of their orders hold it,
  the assistant asks which order (IDs and statuses only) *before any tool runs*; the same for returns, eligibility checks and tickets.
* **Language**: the customer's latest message decides the language by code; a one-line per-turn instruction tells the model; all fixed
  sentences are chosen by code from the session language.

## 3. What was completed in this project

| Area | Result |
|---|---|
| Behaviour | Intent split for returns (request, eligibility question, status question); `check_return_eligibility` timing tool with item recovery; named-order lookup enforced; general policy questions routed to search; angry refund demands looked up first; repeat-complaint clarification; refund-status wording; ambiguity guards |
| Language | Code-decided language; 110 sentences in one three-language catalogue; Hindi and Hinglish greetings, thanks and off-topic detection |
| Evaluation engineering | Real-backend harness with denominators kept explicit; persistent usage ledger (every request, embeddings included, fail-closed ceiling); harness-only retry of pre-transmission failures; per-test attribution of transport failures; composition of attempts (first clean result per test, everything disclosed) |
| Tests | 3796 offline tests (3137 at the original handover), all passing (section 6) |

## 4. Evaluation results (details and caveats in `docs/evaluation/EVALUATION_REPORT.md`)

**Evaluation A, affected behaviours** (code `8fafd95`; 97 chat requests, five valid segments): generic requests about other customers
safe 20/20; named other-person orders never looked up (0/10) before the later fixes; own orders 4/4; return and refund status facts
14/14; English after Hinglish failed 5/5. **Evaluation B, missing coverage** (same code; composite of several attempts): original set
scenarios 5/5, conversations 11/12 (45/46 keys, 133/134 universal), single-turn tool selection 22/25, language match 27/28; identity set
7/7; eligibility set 9/10 conversations (all ten read correct); post-confirmation diagnostic correct. Two post-fix validations
(70 and 29 requests, separate from A and B): the second passed all 12 targeted conversations. **The two later correction cycles
(`41c1c9c`, `4462cb5`) and the commit after it were validated offline only.**

## 5. Known limitations and what still needs a person

0. **Ticket-scope gate**: The ticket-scope gate (`guard.ticket_scope`) is vocabulary-based: it recognises store complaints from word lists, so unusual wording can be asked a clarifying question instead of getting a card, and it has **not yet passed independent held-out validation** (protocol: `tests/data/ticket_scope_heldout.md`; the 190 regression tests in `tests/test_ticket_scope_gate.py` are the tuning set).
1. **Human review (blocking for production wording)**: all 110 Hinglish and Hindi sentences are drafts; 17 are marked High priority (13 original plus the four ticket-scope sentences) (the 105th, `which_order_to_return` (added after the review sheet's first 104 and not yet seen by a reviewer), is also a draft).
   Every manual verdict in the evaluation report is the assistant's own reading, not a signed review.
2. **Live validation of the last two cycles** was not run (ledger headroom 18 of 700 at the end). About 20 requests would cover the
   Hinglish refund wording, a Hindi return journey and the which-order question; the ceiling must be discussed first.
3. **Model-chosen orders in free text**: the ambiguity guards act on tool calls. A reply that describes an order the model chose without
   a tool call is not caught.
4. **Typed IDs only** (`ORD` plus digits, with `-`, `_` or a space accepted): "order 1010" or a bare number is left to the model.
5. **Conservative guards**: a sentence quoting an unsupported duration or a refund promise is removed even if harmless; Devanagari digits
   are not parsed; the language of replies in a fourth language is not checked.
6. **Sentences not in the catalogue**: the offline stand-in's own replies (`guard.unsupported_reply`), model prompts and tool notes
   (English, model-facing), and the model's free prose (steered by the per-turn language line, not enforced).
7. **Statistical weight**: small sets, one to five repetitions, temperature 0.2, written by the same team as the prompts.
8. **Voice (LiveKit)** has never been run live. **Docker**: the `api` and `app` services were built and health-checked on 2026-11-19 (see `docs/PROJECT_STATUS.md`); the voice `worker` was not tested.
9. **Request counts** are client-side bookkeeping at the `urllib`/`httpx` layer; other HTTP libraries would not be counted.

## 6. Final offline test result

`uv run python -m pytest tests -q -rs`: **3137 passed, 0 failed, 0 skipped**, 1 warning (a Chroma deprecation note), 239 seconds. Growth:
2639 before the evaluation tooling, 2825 after the second correction round, 2983 after the third, 3079 at the handover, 3137 after the return-prose fix (evaluation report, section 13).

What the 3137 tests cover for the order-ambiguity rule: every order-reading tool the model can propose (status lookup, refund lookup, both
candidates in one turn, a listing, an eligibility check, a return, a ticket); each way a customer can settle it (nothing typed: ask; typed in the
message; typed earlier in the conversation; a single matching order; orders that are not delivered; the customer asking for their own orders);
and account isolation (candidates come only from the signed-in customer's orders, another customer's ID typed after the question is still "not
found", nothing is written while asking, the model is never called again and sees no order data), on top of the repository-level isolation tests.
The tools that read no individual order (`get_my_returns`, `get_my_tickets`, `lookup_product`, `search_policies`) are deliberately outside this
rule and are not claimed to be covered by it.
Tests never open the application's databases (an autouse guard fails any test that does) and never touch the network (an audit plugin
reports external attempts: 0).

## 7. Git

* Branch **`rukum/corrective-v2`**: 15 commits on top of the preserved tag `corrective-v1-0ca21fa` (an annotated tag whose commit is `0ca21fa`), 34 commits ahead of local `master`.
  **The branch itself was never pushed** (it holds the unredacted evidence archive; its content was merged via PR #2). The original capstone PR branch `rukum/capstone-final` is unchanged (`f9883ea`, same as its remote).
  `ecommerce-customer-assistant` (the GitHub project behind `origin`) was not touched.
* Diff against the tag `corrective-v1-0ca21fa`: 39 files, +7068 / -279. Against `master`: 78 files, +24472 / -413 (`git diff --shortstat master..HEAD` gives the exact current figure).
* Preserved and unchanged: the historical v2, v5 and e47501e results, the corrective-v1 results and attempts, the held-out set (hash
  pinned by a test, with a loosening-only amendments file), and every earlier evaluation attempt (kept in the scratch evaluation
  folders; the reports in `docs/evaluation/` are unchanged).

| Commit | Summary |
|---|---|
| `9cb4899` | fix(prompt): decline generic requests for other customers' orders; use return records and order status together |
| `60b7dcf` | fix(language): treat "sorry, I meant ..." as English |
| `8fafd95` | feat(eval): retry runner, tighter timestamps, visible item recovery, explicit denominators |
| `f363b75` | feat(eval): persistent cumulative usage ledger for live evaluation runs |
| `0279143` | feat(eval): harness-only retry of requests that failed before transmission |
| `c18e0e1` | feat(eval): attribute transport failures to tests and compose attempts |
| `0d34a9c` | feat(eval): compose `--new-sets` reports from several attempts |
| `bccbc12` | fix(prompt,language): named orders always looked up; per-turn language line; policy vs eligibility; refund demands |
| `d5146c1`, `4dab4c5`, `437d71a` | docs: the evaluation and final reports |
| `78f9790` | fix: remaining defects (named orders, C003, ticket localisation, refund timelines) |
| `41c1c9c` | fix: refund-status wording, localised return journey, ambiguous-order guards |
| `4462cb5` | fix: ask which order before reading anything; localise the remaining sentences; reviewer sheets |
| the last commit (`git log -1`) | the handover documents and final report; the review sheets with the 13 High rows marked awaiting fluent human review; tests for every order-reading tool type, typed and earlier IDs and isolation |

After `224f7a5` the branch adds the raw-evidence archive (`docs/evaluation/corrective_v2/raw/`, one commit) and a documentation and
repository-presentation cleanup (README, architecture, status, contributor guides, document moves; no application code, prompt, dataset or
result changed). Superseded documents moved to `docs/history/`; see `docs/README.md` for the map.

## 8. Running and maintaining the project

**Setup** (Python 3.13 and `uv`; do not use pip or a separate venv): `uv sync`. Run everything from the repository root and use
`python -m`, so the root is on `sys.path`.

```bash
uv run python -m pytest tests -q                                   # the whole offline suite
uv run python -m pytest tests/test_final_cycle.py -q               # one file
uv run python -m streamlit run app/ui/streamlit_app.py             # the chat UI (real backend by default)
uv run uvicorn app.backend.http_api:app --host 127.0.0.1 --port 8000   # the HTTP API
docker compose up --build                                          # containers: real backend, seeded volume, port 8501
```

**Database.** Create a demo database with `uv run python -m app.db.seed_data --db-path demo/demo.db --reference-time 2026-11-07T00:00:00Z`
(it refuses existing files and `app/db/data/app.db`). **Never open, seed, reset or write `app/db/data/app.db`.** Treat `app/db/data/app_d2.db`
as protected unless its owner says otherwise; use `demo/demo.db` or a temporary database. Normal startup never creates a database.

**Configuration** (`.env` from `.env.example`; real environment variables override it). `LLM_PROVIDER` is `openai`, `anthropic` or unset
(offline stand-in); `LLM_MODEL`; the key variable for the provider; `OPENAI_API_KEY` is also required for vector retrieval (embeddings);
`LLM_TIMEOUT_SECONDS` (default 20); `UI_BACKEND=fake` runs the canned UI backend. A misconfigured real provider raises an error; it never
falls back silently. Never put real keys in a `.env` that Docker Compose will pass to containers for an offline demo.

**Changing wording.** A fixed sentence lives in `app/tools/flow_texts.py`, in all three languages. After a change run
`uv run python scripts/localization_review.py` (regenerates the reviewer sheets; a test fails if you forget, or if a new key has no `WHEN`
description). English text is pinned by tests; Hinglish and Hindi remain `draft` until a fluent reviewer changes `REVIEW_STATUS`.
**Changing the system prompt** (`SYSTEM_PROMPT` in `engine.py`): several tests pin exact phrases and a length bound (under 7000
characters); a prompt change needs a small live check, because offline tests cannot show whether the model follows it.
**Adding a tool**: add it to `registry.py` with a schema that contains no customer ID, scope its repository call to the session's customer, and
add an authorization test.

**Live evaluation, the safe procedure.**

```bash
# 1. a ledger with an explicit prior-usage statement and a ceiling (never default it to zero)
uv run python scripts/eval/usage_ledger.py --ledger ~/.ecommerce-support-eval/usage_ledger.jsonl init --ceiling 700 --prior-usage none --reason "..."
uv run python scripts/eval/usage_ledger.py --ledger ~/.ecommerce-support-eval/usage_ledger.jsonl status
# 2. a run (needs the key in the environment; every request is reserved against the ledger first)
uv run python scripts/eval/real_eval.py --cases app/db/data/eval/queries_conversational.csv --conversations app/db/data/eval/conversations.json \
  --llm env --allow-live --retrieval production --usage-ledger ~/.ecommerce-support-eval/usage_ledger.jsonl --transport-retries 2 --run-id my-run --output out.json
# other sets: --new-sets, --heldout, --affected FILE; failed segments: scripts/eval/compose_attempts.py unclean|compose
```

The ledger refuses to start when the baseline is pending or unresolved, stops a run at the ceiling (exit code 4), and records retries and
embeddings. Do not raise the ceiling without agreement. A run is scored only when `integrity.run_valid_for_scoring` is true, or as a disclosed
composite of attempts. Manual review is the authority; the automated checks only flag.

**Maintenance checklist.** Keep the suite green; keep the held-out set frozen; keep Evaluation A and B results separate; add a regression test
for every defect; regenerate the review sheets after wording changes; record any live run's request count in the ledger; commit locally, push
only when the owner asks.
