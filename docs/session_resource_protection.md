# Session and resource protection (H3)

Implements the H3 finding of the architecture audit: unbounded session creation, no effective idle cleanup, and no adequate request
or provider usage limits. **Offline-tested only**: nothing here was run against a real LiveKit server or a live model.
Authentication of customers (H1) and voice-confirmation authorization (H2) are **not** part of this work and remain open; see
"Remaining security work" at the end.

## Root causes found in the code

| Where | What was wrong |
|---|---|
| `SessionRegistry` | No maximum number of sessions. Expiry happened only when someone else created a session or when the owner returned; nothing ran in the background. A purge could remove a session whose turn was still running. Finished pending actions stayed in memory until the whole session was dropped. |
| `http_api` | A second, parallel store (`_sessions`, `_session_by_room`) outside the registry that never expired: a customer who closed the tab without calling close leaked an entry, their LiveKit agent dispatch and their backend session until the process restarted. |
| `http_api` | No request-body limit, no request-rate limit, no creation limit; the LiveKit dispatch call had no timeout and an unhandled failure returned a 500 with the backend session already allocated. |
| LiveKit | Nothing ever released a room, and the voice agent never told the API when its job ended. Closing an application session does not release LiveKit resources (and a finished agent job does not end the application session); both directions are now explicit. |
| Provider calls | Each call already had a timeout and was never retried, but nothing bounded calls per session or per time window. A request could wait on a session's turn lock without limit. |

Found in the review of the first H3 commit and fixed in the follow-up: voice users behind Streamlit shared one rate-limit bucket (F1); in-process
Streamlit sessions had no per-client limit at all (F2); `/api/agent/*` was unauthenticated (F3); shutdown released rooms one after another and
request threads waited on LiveKit (F4, F9); a voice call kept running after a hard session limit (F5); a turn still running when its session ended
could re-create a pending entry nobody could reclaim (F6); configuration values were unchecked (F7); a refused request still refreshed a session's
idle timer (F8).

## What the protection does

* **Session cap and creation rates** (`SessionRegistry.create`): the global cap, the per-client and global creation rates and the storage of the session are
  one atomic step under the registry lock. A refused attempt consumes no rate budget. The limit reached is reported (`capacity`, `rate_limited`) and mapped to
  HTTP 503 or 429 with `Retry-After`.
* **Expiry and cleanup**: a session expires when idle (`SUPPORT_IDLE_TIMEOUT_SECONDS`) and when older than an absolute age (`SUPPORT_MAX_SESSION_AGE_SECONDS`;
  activity does not extend it). A background sweeper (started by the API's lifespan and by `get_backend()`) and every create remove expired sessions together
  with their pending actions, request counters and budget counters. Listeners are told why a session was dropped (`ended`, `idle`, `age`, `shutdown`).
* **An active turn never loses its session**: a session whose turn lock is held, or that holds reserved write capacity (a claimed action), is skipped by every
  expiry purge. An explicit end by the customer, or the application shutting down, does end it; see the next point.
* **Ending a session mid-turn (F6)**: the registry marks a dropped session `closed`, and `put_pending` refuses to attach a new card to a closed session
  (`SessionClosedError`, answered as `invalid_session`), so no entry is created that nothing could ever reclaim. A confirmed action that was already
  *claimed* is unaffected: the claim is taken once, before the session could be dropped, so the write still commits exactly once and its reservation is released.
  Only the reply to that last request can be lost.
* **Idle timer (F8)**: only an *accepted* request refreshes a session's `last_active`. A lookup, a rate-limited or over-limit request, a too-long or empty
  message, a malformed request, a busy-session refusal, an unknown session, an unauthorized agent call or a client rate limit does not.
* **Pending actions**: open cards disappear with their session (a late confirmation gets `invalid_session` and writes nothing). Finished actions are kept only up to
  20 per session, so a repeated confirmation of a recent card still reports `done` instead of creating anything.
* **Request protection**: body-size limit (an ASGI middleware, 413 before parsing), identifier length limits, per-client and per-session request rates, a total
  per session (every message, confirmation and choice counts), the existing message-length limit, and a bounded wait for the session's turn lock. A refused request
  is **reported, never silently dropped**, and leaves the open card untouched so the customer can answer again.
* **Provider budget**: model calls are charged to the session and to a per-minute window (`BudgetedLLM` wraps the client given to the engine; `Backend.llm` stays the
  raw client). A message is refused up front when its session or the window is over budget; a confirmation is never refused for the provider budget because it makes
  no model call. A budget hit in the middle of a turn raises `LLMError(category="budget")`, which the engine already turns into the "unavailable" reply. Adapters make
  exactly one attempt per call (tests prove a timeout and a 429 are not retried); `LLM_TIMEOUT_SECONDS` must be between 1 and 120.
* **No duplicate business operations**: a confirmation is claimed once, so a retry after a lost response, ten concurrent confirmations, or a confirmation arriving
  after cleanup or after the session ended create at most one ticket or return.

## Per-browser versus global limits (F1, F2)

| Limit | Applies to | Scope |
|---|---|---|
| Active sessions (`SUPPORT_MAX_SESSIONS`) | every client, identified or not | **global** |
| Session creations per minute (`SUPPORT_SESSIONS_PER_MINUTE`) | every client, identified or not | **global** |
| Session creations per client per minute | one browser (or one peer address) | per client |
| HTTP requests per client per minute | one browser (or one peer address) | per client |
| Requests, rate and model calls per session | one session | per session |
| Model calls per minute (`SUPPORT_PROVIDER_CALLS_PER_MINUTE`) | all sessions | **global** |

*Who is a "client"*: the Streamlit server creates a random, opaque **browser identifier** per browser session (`secrets.token_urlsafe`, kept in Streamlit's session
state, generated on the server, never derived from the customer ID). It is used in two places:

1. **In-process** (the default text chat): passed to `Backend.start_session(customer_id, client_key)`, so each browser has its own creation bucket.
2. **Over HTTP** (voice): sent as `X-Browser-Id` on session start and close, together with `X-Service-Token: <SUPPORT_UI_TOKEN>`. The API honours the identifier **only**
   when that token matches; otherwise it is ignored and the client is the TCP peer. A forged, rotated or malformed identifier from anyone else therefore buys nothing.
   `X-Forwarded-For` and similar headers are never read, and a customer ID chosen in the UI is never a client identity.

A caller with no identifier is **not exempt**: it still counts against the global cap and the global creation rate, so the global protections hold even if an
identifier is missing. (The offline evaluation harness is such a caller and passes explicitly lifted creation rates, because it is not a client of the service.)
If `SUPPORT_UI_TOKEN` is not set, voice starts fall back to the Streamlit server's network address and **all voice users share one bucket**.

**This is anonymous abuse control, not authentication.** The identifier proves nothing about who a person is; anyone can obtain a new one by reloading the page, so it
bounds accidents and casual abuse, not a determined client. The global cap and global rates are what bound a determined one. The customer identity problem is H1.

## Service-to-service authentication (F3)

`/api/agent/turn` and `/api/agent/close` are for the voice worker only. They require `X-Service-Token: <SUPPORT_AGENT_TOKEN>`:

* The comparison is constant time (`hmac.compare_digest`). The presented value is never logged, echoed or placed in an error; a missing and a wrong credential get the
  same answer (401), and repeated failures from one peer are throttled (429 after 30 a minute).
* It **fails closed**: while `SUPPORT_AGENT_TOKEN` is not set on the API the endpoints answer 503 whatever is presented, and the API logs a warning at startup. The voice
  worker refuses to start without the same token.
* The two secrets are separate: `SUPPORT_AGENT_TOKEN` (worker to API) and `SUPPORT_UI_TOKEN` (Streamlit to API) must differ, and neither is accepted in place of the other.
  The compose file gives the Streamlit container a blank agent token and the worker a blank UI token, so a compromise of one does not hand over the other's secret.
* Room ownership and session validation are unchanged: a valid token with an unknown, ended or guessed room name still gets 404, and room names are `uuid4` values.
* Network isolation is an additional layer: the compose file exposes the API only to the compose network. The token is the control; the network is not relied on alone.
* Rotate a secret by changing it in `.env` and restarting the API and the service that uses it.

This authenticates the *voice worker* to the API. It does **not** authenticate customers (H1) and does not make a spoken confirmation trustworthy (H2).

## Session lifecycle and shutdown

```
POST /api/sessions -> client key (browser id if authenticated, else peer) -> rate check -> registry.create (cap + rates + store, atomic)
        -> token -> dispatch (timeout)
        | failure or timeout: remove maps, end session, queue room release, 502/504
        v
   active: every request -> client rate -> per-session admission -> turn lock (bounded wait) -> engine -> touch (accepted requests only)
        |
        +-- close / agent close / invalid session reply -> remove maps, end session, queue room release
        +-- sweeper or any create: idle or too old and not busy -> drop -> listener removes maps and queues the room release
        +-- application shutdown -> registry.shutdown -> every session dropped -> every room release queued -> drained (bounded)
```

**Room release is background, bounded and idempotent (F4, F9).** A room release is queued on a small pool of daemon worker threads (8): a request thread, the sweeper
and a `create` never wait for LiveKit. A room already queued is not queued twice; releasing an already-deleted room only logs a warning; one failing or hanging release never
stops another, because each is its own attempt bounded by `SUPPORT_LIVEKIT_TIMEOUT_SECONDS`.

**Shutdown** ends every session (the drop listener queues every room) and then waits for the queue to drain for at most `SUPPORT_SHUTDOWN_TIMEOUT_SECONDS` overall. Releases
that have not started by then are skipped, so shutdown takes at most that deadline plus one per-room timeout, however many rooms there are. Rooms that were skipped are
logged by count and are closed by LiveKit's own empty-room timeout. A write that was already claimed when the shutdown began still commits once (see F6 above); its reply is lost.

**Voice calls.** The worker bounds a call by length and by silence, tells the API when its job ends (`POST /api/agent/close`), and ends the job when a reply is
`terminal`: a reply is terminal when the session is gone or a *hard* limit is used up (the session's request limit, its message limit, or its model-call budget), and it is
spoken first. A *transient* refusal (a request rate, a busy session, the shared model-call window) is `rate_limited` without `terminal`; the call stays up and waiting helps.
Ending the job stops further speech-to-text, model and text-to-speech work for that call.

## Configuration

Read by `Settings.from_env` (and, for the voice limits, by the worker). **An invalid value is rejected at startup**, never silently replaced: unparsable, non-finite,
below the minimum or above the maximum, or a combination that would defeat a protection. All problems are listed together and no secret is ever printed. A blank value
means "unset". Also listed in `.env.example`.

| Variable | Default | Allowed range | Meaning |
|---|---|---|---|
| `SUPPORT_MAX_SESSIONS` | 200 | 1 to 10,000 | Active sessions, all clients (global) |
| `SUPPORT_SESSIONS_PER_CLIENT_PER_MINUTE` | 10 | 1 to 600 | Session creations per browser (or peer address) |
| `SUPPORT_SESSIONS_PER_MINUTE` | 60 | 1 to 6,000 | Session creations, all clients and unidentified callers (global) |
| `SUPPORT_IDLE_TIMEOUT_SECONDS` | 3600 | 60 to 86,400 | Idle expiry |
| `SUPPORT_MAX_SESSION_AGE_SECONDS` | 28800 | 300 to 604,800 | Absolute lifetime |
| `SUPPORT_MAX_REQUESTS_PER_SESSION` | 300 | 10 to 5,000 | Messages, confirmations and choices per session |
| `SUPPORT_REQUESTS_PER_SESSION_PER_MINUTE` | 30 | 1 to 600 | Request rate per session |
| `SUPPORT_REQUESTS_PER_CLIENT_PER_MINUTE` | 120 | 1 to 6,000 | HTTP request rate per browser (or peer address) |
| `SUPPORT_MAX_BODY_BYTES` | 16384 | 4,096 to 1,048,576 | HTTP request body limit (must fit a maximum-length message: at least 13,024) |
| `SUPPORT_TURN_LOCK_TIMEOUT_SECONDS` | 40 | 1 to 300 | Wait for the session's previous turn |
| `SUPPORT_PROVIDER_CALLS_PER_SESSION` | 300 | 10 to 10,000 | Model calls per session |
| `SUPPORT_PROVIDER_CALLS_PER_MINUTE` | 240 | 10 to 100,000 | Model calls per minute, all sessions (global) |
| `SUPPORT_LIVEKIT_TIMEOUT_SECONDS` | 10 | 1 to 60 | One LiveKit dispatch or room release |
| `SUPPORT_SWEEP_INTERVAL_SECONDS` | 60 | 1 to 3,600 | Background cleanup of expired sessions |
| `SUPPORT_SHUTDOWN_TIMEOUT_SECONDS` | 20 | 1 to 300 | All room releases at shutdown, overall |
| `SUPPORT_TICKET_RATE_MAX` | 5 | 1 to 1,000 | Account-level ticket creations allowed per rolling window |
| `SUPPORT_TICKET_RATE_WINDOW_SECONDS` | 86400 | 60 to 31,536,000 | Length of the rolling ticket-creation window |
| `SUPPORT_VOICE_MAX_SECONDS` | 1800 | 30 to 14,400 | Voice worker: longest call |
| `SUPPORT_VOICE_IDLE_SECONDS` | 300 | 10 to 3,600 | Voice worker: longest silence (not above the call limit) |
| `SUPPORT_AGENT_TOKEN` | none | 16 to 256 printable characters, no spaces | Voice worker to `/api/agent/*`. Required for voice (503 until set; the worker will not start without it) |
| `SUPPORT_UI_TOKEN` | none | 16 to 256 printable characters, no spaces | Streamlit to API: makes the API trust Streamlit's browser identifiers. Must differ from the agent token |
| `LLM_TIMEOUT_SECONDS` | 20 | 1 to 120 | Provider call timeout; outside the range is rejected; calls are never retried |

Combinations rejected: idle timeout above the absolute age; sweep interval above the idle timeout; per-client creation rate above the global one; a body limit too small
for a maximum-length message; identical agent and UI tokens. The existing `max_messages_per_session` (60) and `max_message_chars` (2000) are unchanged: the message
limit still ends a session's texts at 60; the request limit also counts confirmations and choices.

## Limitations (read before relying on this)

* **Everything is process-local.** Sessions, counters, queues and limiters live in one process's memory. Two workers or replicas each hold their own, so every limit becomes up
  to N times larger and a session created on one instance does not exist on another. The compose file runs one API process; keep it that way, and do not claim these limits
  protect a multi-worker deployment.
* **The browser identifier is not an identity.** It is regenerated by a page reload, so one person can obtain many buckets; the global cap, global creation rate and per-session
  limits are the bound that still holds. Without `SUPPORT_UI_TOKEN`, all voice users behind Streamlit share one bucket (the Streamlit server's address).
* **The peer address is only as good as the network in front of the API.** Behind a reverse proxy or load balancer every client shares the proxy's address. Forwarded headers are
  deliberately not trusted; trusting them needs a configured list of trusted proxies, which is not built. Put a rate limiter at the proxy as well.
* **The service tokens are shared secrets in `.env`.** They are not rotated automatically, are visible to anyone who can read the environment of the containers that hold them, and are
  sent over plain HTTP inside the compose network (put TLS in front of any other topology). They authenticate services, not customers.
* **LiveKit behaviour was not verified live.** Room deletion uses the documented `RoomService.DeleteRoom` call, and a deleted room disconnects its participants (the agent included),
  which ends the job; this was not exercised against a real server. The voice worker's speech-to-text, model and text-to-speech calls are made by the LiveKit agents framework, **not**
  through `BudgetedLLM`, and are not individually budgeted: the call-length and idle limits, and ending the call on a terminal reply, are what bound them.
* **A restart still loses every session and pending card** (by design: an unconfirmed action can never be replayed). Rooms left behind by a crash are closed by LiveKit's own
  empty-room timeout, not by this code.
* **Releases skipped at shutdown** (past the deadline) are not retried; they depend on that LiveKit timeout.
* The turn lock's bounded wait means a customer who sends a second request while the first is still running may be told to wait; nothing is lost and nothing runs twice.

## What a multi-instance deployment would need

A shared session store (for example Redis) with atomic create and expiry; shared counters for the creation, request and provider limits; a trusted-proxy configuration for the client
address; a single owner for each LiveKit room (or the room name derived from the shared session); distributed locking or an idempotency key for confirmations (today the single claim in one
process is the guard); per-instance service credentials with rotation; and a metrics and alerting path for refusals. None of that is implemented.

## Remaining security work (not resolved by this change)

* **H1, authentication and customer identity**: the customer ID is still selected from the UI. The browser identifier and the service tokens do **not** authenticate a customer; a signed-in
  identity is needed before this can face real customers.
* **H2, voice-confirmation authorization**: a voice model still decides `accepted` on a spoken confirmation. A confirmation must come from an explicit, authenticated, non-model channel.
  Nothing here changes who or what may confirm a ticket or return.
* The ticket-scope gate is still vocabulary-based and has not passed independent held-out validation; Hinglish and Hindi sentences are drafts.

## Tests

`tests/test_session_protection.py` (registry, backend, budgets, failures, duplicates, browser buckets, terminal replies, mid-turn ends, idle-timer refresh, configuration bounds),
`tests/test_http_protection.py` (FastAPI with LiveKit replaced by recorders: limits, browser identity, agent authentication, bounded cleanup, terminal replies) and
`tests/test_voice_lifecycle.py` (the worker's watchdog, service credential, terminal replies and strict startup), plus `tests/ui/test_browser_identity.py` (the Streamlit side). They use temporary
databases, scripted providers and a controllable clock; no network, no paid API.
