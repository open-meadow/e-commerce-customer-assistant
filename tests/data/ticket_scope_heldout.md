# Held-out evaluation protocol for the ticket scope gate

**Status: PENDING. No held-out cases have been authored or run. No claim of independent validation may be made until the steps below are done and the result is recorded here.**

The gate is `guard.ticket_scope` plus `Engine._ticket_scope`. It is vocabulary-based. The phrases in `tests/test_ticket_scope_gate.py` are the tuning set: the gate's author wrote them while building and fixing the gate, so they are regression tests, not validation.

## What to author

- 40 store messages (complaints or service requests about an order, product, delivery, payment, refund or return) and 40 personal or unrelated messages (relationship, coaching, general knowledge, coding, venting).
- Spread across English, Hinglish (Latin script) and Hindi (Devanagari), with typos, slang, half sentences and mixed messages. Include near-misses: store words used in a personal sense, and real complaints that use only everyday words.

## Rules

1. Written by someone other than the author of the gate, without reading its word lists in `app/orchestrator/guard.py`.
2. Frozen before the first run: commit the table below first, then run it once.
3. Never used to tune the gate. If a case fails, record it. Any later change to the gate makes these cases a tuning set, and a fresh set is needed for a new claim.
4. Run offline: call `guard.ticket_scope(text)` for each message and record `store`, `personal` or `unclear`. An expected `store` that returns `unclear` is a safe miss (a clarifying question); a `personal` message that returns `store` is a failure.
5. Report the counts per language and class, with every failure listed. Do not call the gate validated unless the author of the cases and a reviewer have signed the result.

## Cases (none yet)

| # | Message | Language | Expected (`store` / `personal`) | Author | Result |
|---|---|---|---|---|---|

## Result

Not run.
