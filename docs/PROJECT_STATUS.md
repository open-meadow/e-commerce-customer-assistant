# Project status

**Project:** E-commerce customer-support assistant (capstone). **Branch:** `rukum/corrective-v2` (source of truth).
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
| Publication | **Not pushed.** PR #1 (`rukum/capstone-final`) comes first; a separate PR of the corrective-v2 work follows after it merges |

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
3. Publication: wait for PR #1 to merge, then open a PR containing only the corrective-v2 work and the cleanup.
4. Refresh `docs/Capstone_Presentation.pptx` (it predates the corrective-v2 results; see [deck/README.md](deck/README.md)).
5. Live runs of voice (LiveKit) and a Docker rebuild; neither has happened since these changes.

## Known limitations

See section 11 of the [evaluation report](evaluation/EVALUATION_REPORT.md): free prose is steered, not enforced; ambiguity
guards act on tool calls; typed order IDs only; conservative refund and duration guards; small sets at temperature 0.2;
request counts are client-side bookkeeping.

## Reading order for a new contributor

[README](../README.md) → [architecture](architecture.md) → [PROJECT_HANDOVER](PROJECT_HANDOVER.md) →
[evaluation report](evaluation/EVALUATION_REPORT.md) → [CLAUDE.md](../CLAUDE.md) for commands and conventions.
