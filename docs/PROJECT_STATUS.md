# Project status

**Project:** E-commerce customer-support assistant (capstone). **Branch:** `master` (PRs #1 and #2 were merged on 2026-11-19, `6e82d86`). The development line `rukum/corrective-v2` is kept locally.
**Code checkpoint:** `224f7a5` (frozen: no application-code or prompt changes after it). **Offline tests:** 3,079 passed, 0 failed,
0 skipped (`uv run python -m pytest tests -q`, about 3 minutes, no network).

This is the one current status page. Older snapshots are in [history/](history/). Verify against the files and Git rather than
trusting any status text, including this one.

## Where it stands

| Area | State |
|---|---|
| Assistant (LLM tool calling, guards, confirmation flow) | Complete; evaluated live on `gpt-4o-mini` up to code `78f9790` |
| Database layer, backend, Streamlit UI, Docker, retrieval | Complete; the UI uses the real backend by default |
| Localization (English, Hinglish, Hindi) | 104 sentences in one catalogue; Hinglish and Hindi are **drafts** |
| Evaluation | Evaluations A and B and two post-fix validations complete; raw evidence archived |
| Repository presentation | README, architecture, evaluation report and documentation index current |
| Docker | `api` and `app` built and verified on 2026-11-19 from `master` `6e82d86` (health checks, database initialised); the voice `worker` is **not tested**; this is not a verification of the whole stack |
| Publication | **Published.** PR #1 (`rukum/capstone-final`) and PR #2 (the corrective-v2 work, via the sanitized branch `integrate/corrective-v2-public`) are merged into `master`. `rukum/corrective-v2` was never pushed because it holds the unredacted evidence archive |

## What is validated, and how

| Category | What it covers |
|---|---|
| **Live-validated** (real model, replies read by hand) | Code `8fafd95` (Evaluations A and B); `bccbc12` (first post-fix check); `78f9790` (final check: 12 conversations passed, 29 requests) |
| **Offline-tested only** | Everything after `78f9790`: refund-status wording replacement, the localised return journey, the order-ambiguity guards (including "ask before any tool runs"), issue-management, greeting, thanks and scope localisation, Hindi and Hinglish courtesy words, `ORD-1010`-style IDs, number-word durations. **Not verified against the live model.** |
| **Human-review pending** | All 104 Hinglish and Hindi sentences; **13 are High priority and await fluent human review**; every manual verdict in the evaluation report |

Details: [evaluation/EVALUATION_REPORT.md](evaluation/EVALUATION_REPORT.md) and the review sheets
[localization_review_hinglish_hindi.md](localization_review_hinglish_hindi.md) / [.csv](localization_review_hinglish_hindi.csv).

## Remaining work

1. A fluent Hinglish and Hindi reviewer to sign the 104 sentences (13 High priority first).
2. A small live check (about 20 requests) of the offline-only changes. The evaluation ledger stands at 682 of its 700-request
   guard (a conservative upper bound), so the ceiling has to be agreed first.
3. Housekeeping: **done** on 2026-11-19. The remote branch `integrate/corrective-v2-public` was deleted after checking that it was fully merged into `master` (PR #2); the local copy at `aa5c8d9` is retained, as are `rukum/corrective-v2` and the private evidence backup.
4. Refresh `docs/Capstone_Presentation.pptx` (it predates the corrective-v2 results; see [deck/README.md](deck/README.md)).
5. Voice (LiveKit) has never been run live, and the Docker `worker` has not been tested. Docker `api` and `app` were verified on 2026-11-19 (see the table above).

## Known limitations

See section 11 of the [evaluation report](evaluation/EVALUATION_REPORT.md): free prose is steered, not enforced; ambiguity
guards act on tool calls; typed order IDs only; conservative refund and duration guards; small sets at temperature 0.2;
request counts are client-side bookkeeping.

## Reading order for a new contributor

[README](../README.md) → [architecture](architecture.md) → [PROJECT_HANDOVER](PROJECT_HANDOVER.md) →
[evaluation report](evaluation/EVALUATION_REPORT.md) → [CLAUDE.md](../CLAUDE.md) for commands and conventions.
