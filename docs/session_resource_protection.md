# Session and resource protection (H3)

Implements the H3 finding of the architecture audit: unbounded session creation, no effective idle cleanup, and no adequate request
or provider usage limits. **Offline-tested only**: nothing here was run against a real LiveKit server or a live model.
Authentication (H1) and voice-confirmation authorization (H2) are **not** part of this change; see the end of this page.

## Root causes found in the code

| Where | What was wrong |
|---|---|
| `SessionRegistry` | No maximum number of sessions. Expiry happened only when someone else created a session or when the owner returned; nothing ran in the background. A purge could remove a session whose turn was still running. Finished pending actions stayed in memory until the whole session was dropped. |
| `http_api` | A second, parallel store (`_sessions`, `_session_by_room`) outside the registry that never expired: a customer who closed the tab without calling close leaked an entry, their LiveKit agent dispatch and their backend session until the process restarted. |
| `http_api` | No request-body limit, no request-rate limit, no creation limit; the LiveKit dispatch call had no timeout and an unhandled failure returned a 500 with the backend session already allocated. |
| LiveKit | Nothing ever released a room, and the voice agent never told the API when its job ended. Closing an application session does not release LiveKit resources (and a finished agent job does not end the application session); both directions are now explicit. |
| Provider calls | Each call already had a timeout and was never retried, but nothing bounded calls per session or per time window. A request could wait on a session's turn lock without limit. |

## What changed

* **Session cap and creation rates** (`SessionRegistry.create`): the global cap, the per-peer and global creation rates and the storage of the session are one
  atomic step under the registry lock. A refused attempt consumes no rate budget. The creation rates apply to an attributable client (the HTTP layer passes the peer
  address); an in-process caller (the Streamlit UI, the offline evaluation harness) has no peer address and is bounded by the cap, the idle expiry and the absolute age. The limit reached is reported (`capacity`, `rate_limited`) and mapped to HTTP 503 or
  429 with `Retry-After`. The client identity is the **TCP peer address**; `X-Forwarded-For` and similar headers are not trusted, and a customer ID chosen in the UI is
  never a client identity.
* **Expiry and cleanup**: a session expires when idle (`SUPPORT_IDLE_TIMEOUT_SECONDS`) and when older than an absolute age (`SUPPORT_MAX_SESSION_AGE_SECONDS`; activity
  does not extend it). A background sweeper (started by the API's lifespan and by `get_backend()`) and every create remove expired sessions together with their
  pending actions, request counters and budget counters. Listeners are told why a session was dropped (`ended`, `idle`, `age`, `shutdown`).
* **An active turn never loses its session**: a session whose turn lock is held, or that holds reserved write capacity (a claimed action), is skipped by every purge. The turn
  also marks the session active when it ends. An explicit end by the customer still ends it.
* **Pending actions**: open cards disappear with their session (a late confirmation gets `invalid_session` and writes nothing). Finished actions are kept only up to 20 per
  session, so a repeated confirmation of a recent card still reports `done` instead of creating anything.
* **Request protection**: body-size limit (an ASGI middleware, 413 before parsing), identifier length limits, per-peer request rate (429), per-session request rate and
  total request count (every message, confirmation and choice counts), the existing message-length limit, and a bounded wait for the session's turn lock. A refused request
  is **reported, never silently dropped**, and leaves the open card untouched so the customer can answer again.
* **Provider budget**: model calls are charged to the session and to a per-minute window (`BudgetedLLM` wraps the client given to the engine; `Backend.llm` stays the raw
  client). A message is refused up front when its session or the window is over budget; a confirmation is never refused for the provider budget because it makes no model
  call. A budget hit in the middle of a turn raises `LLMError(category="budget")`, which the engine already turns into the "unavailable" reply. Adapters make exactly one
  attempt per call (tests prove a timeout and a 429 are not retried); `LLM_TIMEOUT_SECONDS` is clamped to 1..120.
* **No duplicate business operations**: unchanged and re-tested through the registry and over HTTP. A confirmation is claimed once, so a retry after a lost response,
  ten concurrent confirmations or a confirmation arriving after cleanup create at most one ticket or return.
* **LiveKit**: the dispatch is time-bounded and a failure or timeout ends the session, releases the room and answers 502 or 504 without internals. When the registry drops
  a session for any reason, the HTTP maps are cleaned and the room is deleted (best effort, time-bounded, failure logged). The voice worker has a maximum call length and
  an idle limit, ends its job when the backend says the session is gone, and on any shutdown tells the API (`POST /api/agent/close`) so it ends the session and releases the
  room. The API's own shutdown ends every session and releases every room.

## Session lifecycle

```
POST /api/sessions -> rate check (peer) -> registry.create (cap + rates + store, atomic) -> token -> dispatch (timeout)
        | failure or timeout: remove maps, end session, delete room, 502/504
        v
   active: every request -> per-peer rate -> per-session admission -> turn lock (bounded wait) -> engine -> touch
        |
        +-- close / agent close / invalid session reply -> remove maps, end session, delete room
        +-- sweeper or any create: idle or too old and not busy -> drop -> listener removes maps, deletes room
        +-- application shutdown -> registry.shutdown -> every session dropped -> every room deleted
```

## Configuration

All optional; read by `Settings.from_env`. An unset, unparsable or non-positive value keeps the default. Also listed in `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `SUPPORT_MAX_SESSIONS` | 200 | Active sessions, all clients |
| `SUPPORT_SESSIONS_PER_CLIENT_PER_MINUTE` | 10 | Session creations per peer address |
| `SUPPORT_SESSIONS_PER_MINUTE` | 60 | Session creations, all clients |
| `SUPPORT_IDLE_TIMEOUT_SECONDS` | 3600 | Idle expiry (the default is unchanged; expiry used to be lazy and is now also proactive) |
| `SUPPORT_MAX_SESSION_AGE_SECONDS` | 28800 | Absolute lifetime |
| `SUPPORT_MAX_REQUESTS_PER_SESSION` | 300 | Messages, confirmations and choices per session |
| `SUPPORT_REQUESTS_PER_SESSION_PER_MINUTE` | 30 | Request rate per session |
| `SUPPORT_REQUESTS_PER_CLIENT_PER_MINUTE` | 120 | HTTP request rate per peer address |
| `SUPPORT_MAX_BODY_BYTES` | 16384 | HTTP request body limit |
| `SUPPORT_TURN_LOCK_TIMEOUT_SECONDS` | 40 | Wait for the session's previous turn |
| `SUPPORT_PROVIDER_CALLS_PER_SESSION` | 300 | Model calls per session |
| `SUPPORT_PROVIDER_CALLS_PER_MINUTE` | 240 | Model calls per minute, all sessions |
| `SUPPORT_LIVEKIT_TIMEOUT_SECONDS` | 10 | LiveKit dispatch and room release |
| `SUPPORT_SWEEP_INTERVAL_SECONDS` | 60 | Background cleanup interval |
| `SUPPORT_VOICE_MAX_SECONDS` | 1800 | Voice worker: longest call |
| `SUPPORT_VOICE_IDLE_SECONDS` | 300 | Voice worker: longest silence |
| `LLM_TIMEOUT_SECONDS` | 20 | Provider call timeout (clamped to 1..120); calls are never retried |

The existing `max_messages_per_session` (60) and `max_message_chars` (2000) are unchanged. The number of messages a session may send is therefore still 60; the new
request limit also counts confirmations and choices.

## Limitations (read before relying on this)

* **Everything is process-local.** Sessions, counters and limiters live in one process's memory. Two workers or replicas each hold their own, so every limit becomes up to
  N times larger and a session created on one instance does not exist on another. The compose file runs one API process; keep it that way, and do not claim these
  limits protect a multi-worker deployment.
* **The peer address is only as good as the network in front of the API.** Behind a reverse proxy or load balancer every client shares the proxy's address, so the per-peer
  limits become global. Forwarded headers are deliberately not trusted (anyone can set them); trusting them needs a configured list of trusted proxies, which is not built.
  Put a rate limiter at the proxy as well.
* **The limits are not authentication.** They bound resource use; they do not prove who a caller is. The customer ID in a session request is still chosen by the caller (H1).
* **LiveKit behaviour was not verified live.** Room deletion uses the documented `RoomService.DeleteRoom` call, and a deleted room disconnects its participants (the agent
  included), which ends the job; this was not exercised against a real server, and the voice worker's STT, model and TTS provider calls are made by the LiveKit agents
  framework, not through `BudgetedLLM`. The worker's maximum call length and idle limit are what bound them.
* **Cleanup of an expired session is inline in whichever thread drops it.** Releasing a room is bounded by `SUPPORT_LIVEKIT_TIMEOUT_SECONDS`, so a hanging LiveKit can delay
  a request that happened to trigger a purge by up to that long per dropped session; the sweeper normally does the work first.
* **A restart still loses every session and pending card** (by design: an unconfirmed action can never be replayed). Rooms left behind by a crash are closed by LiveKit's own
  empty-room timeout, not by this code.
* The turn lock's bounded wait means a customer who sends a second request while the first is still running may be told to wait; nothing is lost and nothing runs twice.

## What a multi-instance deployment would need

A shared session store (for example Redis) with atomic create and expiry; shared counters for the creation, request and provider limits; a trusted-proxy configuration
for the client address; a single owner for each LiveKit room (or the room name derived from the shared session); distributed locking or an idempotency key for confirmations
(today the single claim in one process is the guard); and a metrics and alerting path for refusals. None of that is implemented.

## Remaining security work (not part of H3)

* **H1, authentication and customer identity**: the customer ID is still selected from the UI; a signed-in identity is needed before this can face real customers.
* **H2, voice-confirmation authorization**: a voice model still decides `accepted` on a spoken confirmation. A confirmation must come from an explicit, authenticated,
  non-model channel.
* The ticket-scope gate is still vocabulary-based and has not passed independent held-out validation; Hinglish and Hindi sentences are drafts.

## Tests

`tests/test_session_protection.py` (registry, backend, budgets, failures, duplicates), `tests/test_http_protection.py` (FastAPI with LiveKit replaced by recorders) and
`tests/test_voice_lifecycle.py` (the worker's watchdog, disconnect notice and shutdown). They use temporary databases, scripted providers and a controllable clock; no network.
