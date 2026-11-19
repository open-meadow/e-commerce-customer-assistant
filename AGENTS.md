# Agent guide

Working conventions for AI coding agents and human contributors. Product overview: [README.md](README.md). Architecture:
[docs/architecture.md](docs/architecture.md). Current status: [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md). Commands and
repository specifics: [CLAUDE.md](CLAUDE.md). This file holds only the rules and the workflow; it does not repeat project
documentation.

## Hard rules

1. **Protected databases.** Never open, read, seed, reset, migrate or delete `app/db/data/app.db` (legacy, protected in code; no
   authorization changes this). Treat `app/db/data/app_d2.db` (git-ignored default database) as protected unless the technical lead
   authorizes that specific access. Use temporary databases in tests and a seeded `demo/demo.db` for the UI.
2. **No live calls without explicit authorization**: LLM providers, OpenAI embeddings, LiveKit, speech or text-to-speech. Offline
   (`RuleBasedLLM`, local test embedding) is the default for tests, evaluations and demos. Never log or store credentials, and do not
   put real keys in a `.env` used for an offline run (Docker Compose passes `.env` to the containers).
3. **Model proposes, code decides.** Do not weaken validation, ownership filters, confirmation or the guards in
   `app/orchestrator`. Tool schemas must never contain a customer ID.
4. **Never claim a write succeeded** without the backend confirming it.
5. **Git.** Do not create or switch branches, stage, commit, push, amend, rebase or merge without the user's explicit approval for
   that operation; approval for one does not extend to the next.
6. **Do not change a frozen checkpoint.** After a development freeze, application code and prompts change only with the owner's approval.
7. **Do not edit evaluation datasets or archived results.** `docs/evaluation/corrective_v2/raw/` is archived evidence (SHA-256 in
   its manifest): byte-exact except 18 files whose local Windows user path was redacted, listed with both hashes in its
   `REDACTIONS.md`; the held-out set is frozen and pinned by a test.
8. Localization: Hinglish and Hindi sentences are drafts. Keep `REVIEW_STATUS` as `draft` until a fluent reviewer signs them.

## Engineering workflow

1. Read the relevant documentation and source before proposing changes; identify the task's acceptance criteria.
2. Keep each task narrowly scoped; do not change approved architecture or requirements without approval.
3. Do not modify tests merely to make an incorrect implementation pass; add a regression test for every defect.
4. Prefer minimal, maintainable changes. Run the relevant tests and report actual output. Review the Git diff and list every modified file.
5. Distinguish verified facts from assumptions, and **distinguish live-validated, offline-tested and human-review-pending behaviour** in
   every report. Never present an offline-only change as verified against the live model.
6. For important changes one model implements and a different one reviews independently; resolve findings against the canonical
   documents, run the tests locally and obtain technical-lead approval before the next checkpoint.
7. Read only what the task needs; do not inspect unrelated modules.

Suggested model roles (from the original project workflow): a fast model for routine edits, tests and documentation; a stronger
thinking model for database, business logic, orchestration and multi-file debugging; the strongest model reserved for security
architecture, authorization and final security review; an independent model for second-opinion reviews.

## Authority hierarchy

1. User-approved decisions.
2. [docs/01_database_design.md](docs/01_database_design.md) for database design.
3. [docs/02_database_implementation_plan.md](docs/02_database_implementation_plan.md) for sequencing.
4. [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) and [docs/PROJECT_HANDOVER.md](docs/PROJECT_HANDOVER.md).
5. This file and [CLAUDE.md](CLAUDE.md).
6. Current source code. If code conflicts with an approved document, report the conflict instead of choosing.

The previous, longer version of this file is kept in [docs/history/](docs/history/AGENTS_before_corrective_v2.md).
