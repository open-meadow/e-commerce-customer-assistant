# Evaluation files

Everything here comes from `scripts/eval/real_eval.py`, which runs the **real backend** (orchestrator, tools,
temporary seeded SQLite database, Chroma retrieval) against a language model and scores the replies with
mechanical checks. Synthetic data only.

| File | What it is |
|---|---|
| `final_v5.json` | The final live run: every case, conversation and scenario, with replies, proposed and executed tools, and per-check outcomes. |
| `baseline_v2_run1.json` | One of the three baseline runs taken before the Phase 2 changes, for before/after comparison. |
| `results_summary.json` | Compact rates for all six live runs (baseline v2 x3, v3, v4, final v5). |

Inputs live in `app/db/data/eval/`:

| File | Content |
|---|---|
| `queries_conversational.csv` | 30 single-turn cases written to be hard: frustrated, abusive, Hinglish and Hindi, indirect wording, requests for a human, unsupported actions, cross-customer and injection attempts, spread across all six customers. |
| `conversations.json` | 12 multi-turn conversations (context: pronoun follow-ups, corrections, a change of mind, repeated frustration, a language switch, off-topic recovery) with per-turn expectations. |
| `queries.csv` | The original 70 labelled cases (customers C101 and C102). Run offline only; not run against a live model. |
| `logs/sample_conversations.jsonl` | Four illustrative demo logs from the first prototype. |

## Setup of the live runs

- Model `gpt-4o-mini` through the OpenAI Responses API, temperature 0.2, production embeddings
  (`text-embedding-3-small`) over a temporary vector store, a temporary database seeded at 2026-11-07.
- Each report records what was exercised in its `layers` block: model, whether the reply-interpretation layer was on,
  guard mode, number of `interpret_reply` calls, retrieval mode, temperature and a prompt fingerprint.
- Final run: prompt fingerprint `55fec724b7b1`, 30 cases, 12 conversations (46 turn expectations), 5 scenarios, 6
  `interpret_reply` calls, about 3 minutes.

## Results (passed / applicable)

| Check | Baseline v2 (3 runs) | Final v5 |
|---|---|---|
| No write without Confirm | 30/30 each | 30/30 |
| No false success claim | 30/30 each | 30/30 |
| No invented contact details | 30/30 each | 30/30 |
| Cross-customer isolation | 2/2 each | 2/2 |
| Return reaches the category menu | 0/2 each | 2/2 |
| Correct tool chosen | 23/25 each | 24/25 |
| Customer's own order facts correct | 8/11, 9/11, 9/11 | 10/11 |
| Reply in the customer's language | 16/30, 15/30, 15/29 | 26/27 |
| Claims supported by tool data | 26/27, 26/27, 25/26 | 26/26 |
| Needless question | 12/13 each | 12/13 |
| Technical scorecard | 156/162, 157/162, 156/161 | 157/158 |
| Conversational scorecard | 42/60, 41/60, 41/59 | 55/57 |
| Conversations fully passing | 2 of 12 each | 11 of 12 |
| Context turn expectations | 43/46, 44/46, 44/46 | 45/46 |
| Scripted scenarios | 5/5 each | 5/5 |

Three scorecards are reported separately and never blended: **technical** (code-checkable correctness and safety),
**conversational** (tool choice, language, context) and **experience**. The experience scorecard contains one
automated proxy only (`unnecessary_question`: a customer who already stated the problem or named an order is not
asked for it again); empathy, tone and helpfulness are **not scored**. Replies were read by hand after each run, but
that informal review (no rubric, one reviewer, not repeated) is not a measured result and is not in the tables.
Baseline percentages quoted elsewhere (report, deck) pool the three baseline runs by summing passed and applicable
counts; the final run is a single run.

## What these numbers do and do not show

- The safety checks (no unconfirmed write, no false success, no invented IDs or contacts, isolation) held in every
  run. They are enforced by code, so this is the most reliable part.
- Baseline v2 has three repeats and agreed almost exactly (temperature 0.2). v3, v4 and the final run are one run
  each; differences of one or two cases (for example C003, C024, C025, C027) are within run-to-run variation.
- The instrument was refined during the work. Scores are as produced by the instrument version current at each
  run. Later fixes (the `not_asking` regex, not judging backend menus as model text, extra Hinglish words) were
  re-applied to the language check only; older runs were not fully re-scored because tool data was not stored in
  the first runs.
- n is small (25 cases for the main rates), so percentages are indicative, not precise.
- Several checks are heuristics (language by script and word list, claim support by phrase list, one narrow
  "needless question" proxy). A paraphrased fault would not be caught. Hand review of replies found a case no check
  could see (a customer's own orders called "your friend's"; since fixed and verified live).
- The first live run (baseline v1, 24/25 tool selection) is **not** a baseline: a wrapper hid the model's
  capability from the engine, so the reply-interpretation layer was never exercised. That bug is fixed and every
  report now records whether the layer was on.

## Known gaps

- Fixed backend wording (category menus, confirmation cards, the "what is the issue?" ticket question) is English
  only, so Hinglish and Hindi customers see English there (C023, and the menus in C015 and C017).
- Run-to-run variation remains for some cases. In the final run C003 ("I already told you my order number, why do I
  have to repeat everything?") got "could you please provide the order ID again?" instead of a lookup of the
  customer's orders, which the model did in two earlier runs. It is a single-turn case (no ID is in the
  conversation; the customer's order list was available through the tool); the related multi-turn conversation M07
  passed. The behaviour is not yet reliable.
- The demo UI does not display the source attached to policy replies.
- Empathy, tone and helpfulness are not scored.
- One model, one temperature, one retrieval corpus of 25 chunks.

## Reproduce

Offline (no key, no network; a deterministic stand-in replaces the model, so scores describe the stand-in):

```bash
uv run --offline --frozen python scripts/eval/real_eval.py --cases app/db/data/eval/queries_conversational.csv --conversations app/db/data/eval/conversations.json
```

Live (makes billed requests; set `LLM_PROVIDER`, `LLM_MODEL`, `OPENAI_API_KEY` and optionally `LLM_TEMPERATURE` in
the environment or a git-ignored `.env`; never commit a key):

```bash
uv run python scripts/eval/real_eval.py --llm env --allow-live --retrieval production --cases app/db/data/eval/queries_conversational.csv --conversations app/db/data/eval/conversations.json --output results.json
```

The runner refuses to write a results file that contains a credential value. Cost: about 110 to 130 short model
calls per run on a small model; check your provider dashboard for rates.
