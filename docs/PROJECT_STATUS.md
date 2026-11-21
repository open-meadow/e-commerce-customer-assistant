# Project status

**Project:** E-commerce customer-support assistant (capstone). **Branch:** `master` (PRs #1 and #2 were merged on 2026-11-19, `6e82d86`). The development line `rukum/corrective-v2` is kept locally.
**Code checkpoint:** `224f7a5`, plus the return-prose fix described in section 13 of the evaluation report (the only application change after it). **Offline tests:** 3,796 passed, 0 failed,
0 skipped (`uv run python -m pytest tests -q`, about 3 minutes, no network).

This is the one current status page. Older snapshots are in [history/](history/). Verify against the files and Git rather than
trusting any status text, including this one.

## Where it stands

| Area | State |
|---|---|
| Assistant (LLM tool calling, guards, confirmation flow) | Complete; evaluated live on `gpt-4o-mini` up to code `78f9790` |
| Database layer, backend, Streamlit UI, Docker, retrieval | Complete; the UI uses the real backend by default |
| Localization (English, Hinglish, Hindi) | 110 sentences in one catalogue; Hinglish and Hindi are **drafts**, including the 105th (`which_order_to_return`) and the 106th to 110th (`ticket_out_of_scope`, `ticket_scope_ask`, `ticket_describe_ask`, `ticket_service_ask`, `ticket_declined`, added with the ticket-scope gate and its follow-up fix) |
| Evaluation | Evaluations A and B and two post-fix validations complete; raw evidence archived |
| Repository presentation | README, architecture, evaluation report and documentation index current |
| Docker | `api` and `app` built and verified on 2026-11-19 from `master` `6e82d86` (health checks, database initialised); the voice `worker` is **not tested**; this is not a verification of the whole stack |
| Publication | **Published.** PR #1 (`rukum/capstone-final`) and PR #2 (the corrective-v2 work, via the sanitized branch `integrate/corrective-v2-public`) are merged into `master`. `rukum/corrective-v2` was never pushed because it holds the unredacted evidence archive |

## What is validated, and how

| Category | What it covers |
|---|---|
| **Live-validated** (real model, replies read by hand) | Code `8fafd95` (Evaluations A and B); `bccbc12` (first post-fix check); `78f9790` (final check: 12 conversations passed, 29 requests) |
| **Offline-tested only** | The return-prose guard (a return request the model answers in prose is settled by code; see the evaluation report, section 13) and everything after `78f9790`: refund-status wording replacement, the localised return journey, the order-ambiguity guards (including "ask before any tool runs"), issue-management, greeting, thanks and scope localisation, Hindi and Hinglish courtesy words, `ORD-1010`-style IDs, number-word durations. **Not verified against the live model.** |
| **Human-review pending** | All 110 Hinglish and Hindi sentences; **17 are High priority (13 original plus the four ticket-scope sentences) and await fluent human review**; every manual verdict in the evaluation report |

The ticket-scope gate (`guard.ticket_scope`) is vocabulary-based: it recognises store complaints from word lists, so unusual wording can be asked a clarifying question instead of getting a card, and it has **not yet passed independent held-out validation** (protocol: `tests/data/ticket_scope_heldout.md`; the 190 regression tests in `tests/test_ticket_scope_gate.py` are the tuning set).

Details: [evaluation/EVALUATION_REPORT.md](evaluation/EVALUATION_REPORT.md) and the review sheets
[localization_review_hinglish_hindi.md](localization_review_hinglish_hindi.md) / [.csv](localization_review_hinglish_hindi.csv).

## Remaining work

1. A fluent Hinglish and Hindi reviewer to sign the 110 sentences (17 High priority first; the 105th is Medium priority).
2. A small live check (about 20 requests) of the offline-only changes. The evaluation ledger stands at 682 of its 700-request
   guard (a conservative upper bound), so the ceiling has to be agreed first.
3. Housekeeping: **done** on 2026-11-19. The remote branch `integrate/corrective-v2-public` was deleted after checking that it was fully merged into `master` (PR #2); the local copy at `aa5c8d9` is retained, as are `rukum/corrective-v2` and the private evidence backup.
4. Refresh `docs/Capstone_Presentation.pptx` (it predates the corrective-v2 results; see [deck/README.md](deck/README.md)).
5. Voice (LiveKit) has never been run live, and the Docker `worker` has not been tested. Docker `api` and `app` were verified on 2026-11-19 (see the table above).
6. **Security work still open after H3** (session and resource protection, [session_resource_protection.md](session_resource_protection.md), offline-tested only and process-local; its service tokens authenticate services, not customers):
   **H1** authentication and customer identity (the customer ID is still chosen in the UI) and **H2** voice-confirmation authorization (a model still decides a spoken yes or no).

## Known limitations

See section 11 of the [evaluation report](evaluation/EVALUATION_REPORT.md): free prose is steered, not enforced; ambiguity
guards act on tool calls; typed order IDs only; conservative refund and duration guards; small sets at temperature 0.2;
request counts are client-side bookkeeping.

## Reading order for a new contributor

[README](../README.md) → [architecture](architecture.md) → [PROJECT_HANDOVER](PROJECT_HANDOVER.md) →
[evaluation report](evaluation/EVALUATION_REPORT.md) → [CLAUDE.md](../CLAUDE.md) for commands and conventions.
