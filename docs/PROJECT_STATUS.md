# Project status

**Project:** E-commerce customer-support assistant (capstone). **Branch:** `master` (PRs #1 and #2 were merged on 2026-11-19, `6e82d86`). The development line `rukum/corrective-v2` is kept locally.
**Code checkpoint:** `224f7a5`, plus the return-prose fix described in section 13 of the evaluation report (the only application change after it). **Offline tests (that checkpoint):** 3,796 passed, 0 failed,
0 skipped (`uv run python -m pytest tests -q`, about 3 minutes, no network). The latest recorded full run, which includes the ticket flow and
schema version 2 work described below, is 4,314 passed, 0 failed, 0 skipped (6 to 14 minutes).

This is the one current status page. Older snapshots are in [history/](history/). Verify against the files and Git rather than
trusting any status text, including this one.

## Where it stands

| Area | State |
|---|---|
| Assistant (LLM tool calling, guards, confirmation flow) | Complete; evaluated live on `gpt-4o-mini` up to code `78f9790` |
| Database layer, backend, Streamlit UI, Docker, retrieval | Complete; the UI uses the real backend by default |
| Localization (English, Hinglish, Hindi) | 150 sentences in one catalogue (129 before the ticket work, which added 21; earlier figures of 110 in older documents were already out of date); Hinglish and Hindi are **drafts**, including the 105th (`which_order_to_return`) and the 106th to 110th (`ticket_out_of_scope`, `ticket_scope_ask`, `ticket_describe_ask`, `ticket_service_ask`, `ticket_declined`, added with the ticket-scope gate and its follow-up fix) |
| Evaluation | Evaluations A and B and two post-fix validations complete; raw evidence archived |
| Repository presentation | README, architecture, evaluation report and documentation index current |
| Docker | `api` and `app` built and verified on 2026-11-19 from `master` `6e82d86` (health checks, database initialised); the voice `worker` is **not tested**; this is not a verification of the whole stack |
| Publication | **Published.** PR #1 (`rukum/capstone-final`) and PR #2 (the corrective-v2 work, via the sanitized branch `integrate/corrective-v2-public`) are merged into `master`. `rukum/corrective-v2` was never pushed because it holds the unredacted evidence archive |

## What is validated, and how

| Category | What it covers |
|---|---|
| **Live-validated** (real model, replies read by hand) | Code `8fafd95` (Evaluations A and B); `bccbc12` (first post-fix check); `78f9790` (final check: 12 conversations passed, 29 requests) |
| **Offline-tested only** | The return-prose guard (a return request the model answers in prose is settled by code; see the evaluation report, section 13) and everything after `78f9790`: refund-status wording replacement, the localised return journey, the order-ambiguity guards (including "ask before any tool runs"), issue-management, greeting, thanks and scope localisation, Hindi and Hinglish courtesy words, `ORD-1010`-style IDs, number-word durations. **Not verified against the live model.** |
| **Human-review pending** | All 150 Hinglish and Hindi sentences; **26 are marked High priority in the review sheet and await fluent human review**; four English ticket messages are also proposals awaiting owner approval; every manual verdict in the evaluation report |

The ticket-scope gate (`guard.ticket_scope`) is vocabulary-based: it recognises store complaints from word lists, so unusual wording can be asked a clarifying question instead of getting a card, and it has **not yet passed independent held-out validation** (protocol: `tests/data/ticket_scope_heldout.md`; the 190 regression tests in `tests/test_ticket_scope_gate.py` are the tuning set).

Details: [evaluation/EVALUATION_REPORT.md](evaluation/EVALUATION_REPORT.md) and the review sheets
[localization_review_hinglish_hindi.md](localization_review_hinglish_hindi.md) / [.csv](localization_review_hinglish_hindi.csv).

## Remaining work

1. A fluent Hinglish and Hindi reviewer to sign the 150 sentences (the 26 High-priority ones first; see the review sheet).
2. A small live check (about 20 requests) of the offline-only changes. The evaluation ledger stands at 682 of its 700-request
   guard (a conservative upper bound), so the ceiling has to be agreed first.
3. Housekeeping: **done** on 2026-11-19. The remote branch `integrate/corrective-v2-public` was deleted after checking that it was fully merged into `master` (PR #2); the local copy at `aa5c8d9` is retained, as are `rukum/corrective-v2` and the private evidence backup.
4. Refresh `docs/Capstone_Presentation.pptx` (it predates the corrective-v2 results; see [deck/README.md](deck/README.md)).
5. Voice (LiveKit) has never been run live, and the Docker `worker` has not been tested. Docker `api` and `app` were verified on 2026-11-19 (see the table above).
6. **Security work still open after H3** (session and resource protection, [session_resource_protection.md](session_resource_protection.md), offline-tested only and process-local; its service tokens authenticate services, not customers):
   **H1** authentication and customer identity (the customer ID is still chosen in the UI) and **H2** voice-confirmation authorization (a model still decides a spoken yes or no).

## Support tickets: Phase 1.1 and Phase 2 (implemented, offline-tested)

Implemented on the working branch `feat/ticket-phase1-1-and-2`; merging it into `master` is a separate step. The decisions behind it are
recorded in the ticket design documents, which are kept outside this repository.

| Item | State |
|---|---|
| Fresh-description flow (Phase 1.1) | Implemented. Gate `SUPPORT_TICKET_FRESH_FLOW_ENABLED`, **default OFF** (only `true` or `false`). When ON, the customer's own current message is the description, kept exactly as typed (at most 2,000 characters); only the Confirm button creates the ticket. With the gate OFF the earlier flow is unchanged |
| Gate interlock | With the gate ON, `Backend` accepts only the real repository writer and refuses to start without it or without a migrated database. The fake writer exists only under `tests/` |
| Schema version 2 (Phase 2) | Implemented. `support_tickets` rebuilt (nullable `conversation_summary`; legacy summaries untouched), plus `ticket_events`, `ticket_erasure_log` and `ticket_write_intents`. Runtime code requires version 2 exactly, with the gate ON or OFF: a version-1 database (including `app_d2.db` and existing Docker volumes) is refused until it is migrated or re-seeded. **Nothing migrates at startup** |
| Migration | `python -m app.db.migrate` needs `--scratch`; verified backup and restore rehearsal; refuses `app/db/data/app.db` and `app_d2.db`. **Run on scratch databases only; no protected database has been migrated** |
| Creation | Request key and immutable payload stored before the insert; a retry of the same confirmation never creates a second ticket. Duplicate detection is a separate outcome from idempotency. The account-level rate limit is counted inside the creation transaction |
| Uncertain outcomes | If a commit's outcome is unknown the request is kept and no second creation is allowed. After session expiry or a restart, the next ticket request reconciles the customer's own stored requests with their original key and payload |
| Latest recorded full run | 4,314 passed, 0 failed, 0 skipped, one ChromaDB deprecation warning. Recorded earlier; not re-run after the later documentation-only edits |
| Not verified | Live-model behaviour with the new prompt and flow; several processes writing to one database file at once (SQLite single-file locking only) |
| Owner decisions outstanding | The delayed-replay policy (how long a stored confirmed request may be replayed without a fresh confirmation); approval of four proposed English messages (`ticket_duplicate_active`, `ticket_idempotency_conflict`, `ticket_recovery_unknown`, `ticket_recovered_created`); fluent review of the Hindi and Hinglish wording. Production enablement stays blocked until they are settled |
| Not started | Phase 3: adding information to an existing ticket, staff status changes and the rules they depend on; these wait for unresolved owner decisions |

## Known limitations

See section 11 of the [evaluation report](evaluation/EVALUATION_REPORT.md): free prose is steered, not enforced; ambiguity
guards act on tool calls; typed order IDs only; conservative refund and duration guards; small sets at temperature 0.2;
request counts are client-side bookkeeping.

## Reading order for a new contributor

[README](../README.md) → [architecture](architecture.md) → [PROJECT_HANDOVER](PROJECT_HANDOVER.md) →
[evaluation report](evaluation/EVALUATION_REPORT.md) → [CLAUDE.md](../CLAUDE.md) for commands and conventions.
