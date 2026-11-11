# Backend Architecture — AI E-Commerce Customer Support Assistant

**Phase:** 3 — Backend architecture and contracts
**Status:** Revision 3.2 (2026-11-10) is the **approved architecture baseline**. It was approved by Person A in the project review. The approval is recorded in this metadata by documentation baseline D1 (2026-11-11), which changed no design content. The baseline is **pending one amendment**: the later decision to add a dedicated `reason_category` column to `return_requests` is approved in principle but needs a formal design amendment in Checkpoint D2. Until D2, Sections 5.3.1 and 7.4 and open item 9 (the reason-prefix convention, which assumes the category is not stored) stay as written. No backend code described here exists yet, and Checkpoint 11B onward is not authorized.
**Revision history (all dated 2026-11-10):**
- **Revision 1:** initial draft with the four Technical Lead decisions D1–D4.
- **Revision 2:** responded to the Technical Lead's review of Revision 1. Added the server-side pending-action store, `prechecks_passed`, customer-selected return categories, session inactivity expiry, the read-only health check, the restored D4 limits, and the Conversation Scope Guard proposal.
- **Revision 3:** responded to the review of Revision 2. Removed the placeholder support address, preserved the full 1,000-character reason input, separated atomic claim, at-most-once dispatch, idempotent execution and recoverable result, added atomic state transitions, the dedicated read-only health connection and the diagnostic, the clarification context, `get_my_tickets`, the session bearer-handle note, and the verified seed timing.
- **Revision 3.1:** responded to the review of Revision 3. Corrected the ambiguous-message test wording, added the atomic write-capacity reservation, and added the WAL-mode and sidecar precondition for the health check.
- **Revision 3.2:** responded to the review of Revision 3.1. Protects in-flight reservations during session cleanup, corrects the busy-timeout wording, and updates this metadata.

**Scope of this document:** design only. No application code, tests, schema, seed behavior, database content, dependencies, LLM, RAG, or Streamlit code is created or changed by this checkpoint.
**Inputs:** `docs/00_project_discovery.md`, `docs/01_database_design.md`, `docs/02_database_implementation_plan.md`, `AGENTS.md`, the Checkpoint 10 inspection report, the four Technical Lead decisions in Section 1, and the Technical Lead's Revision 1 review.

**Relationship to the database documents.** `docs/01` and `docs/02` remain canonical for the database, the repository, and the approved write contracts. This document builds on them and does not change them. The only addition it requires to the repository layer is one new read-only operation (Section 6), which needs matching amendments to `docs/01` Section F and `docs/02` Section 4 in a separately approved change. Where this document and `docs/01` or `docs/02` disagree, report the conflict instead of choosing silently.

**Proposed values.** Numeric limits, timeouts and caps in this document are marked **[Proposed]** unless Section 1.1 lists them as approved. Proposed values and proposed features are design suggestions for Technical Lead confirmation, not approved decisions. Section 18 lists every open item.

---

## 1. Decisions and goals

### 1.1 Technical Lead decisions (approved)

| # | Decision | Where it lands |
|---|---|---|
| D1 | Return eligibility belongs in the repository layer as a read-only operation. | Section 6 |
| D2 | Demo data is reseeded relative to an explicitly supplied current UTC reference time when the demo database is initialized or reset. `seed_database(None)` keeps its fixed behavior. An existing database is never reset automatically. | Section 9 |
| D3 | Unsupported operations (cancellation, address changes, warranty, replacement, courier pickup) get honest explanations and support-ticket escalation where appropriate. The assistant never claims an unsupported action succeeded. | Section 10 |
| D4 | Free-text limits are enforced at the tool/application boundary first. Approved repository write contracts and the database schema are not changed. | Section 5.3 |

**Approved D4 limits.** These values are approved and are not proposals. Any stricter limit or additional restriction on these fields appears only in Section 5.3.2 as a separate proposal.

| Field | Approved maximum |
|---|---|
| Return reason | 1,000 characters |
| Ticket description | 4,000 characters |
| Conversation summary | 2,000 characters |
| Issue type | 100 characters |

### 1.2 Goals
- One stable backend entry point for the future Streamlit frontend, which stays independent of repository and database details.
- Customer identity comes only from trusted backend context. It never comes from a message, an LLM tool argument, or conversation history.
- Every write goes through an explicit confirmation step that the LLM cannot bypass and that cannot be replayed, including by concurrent requests that hold copies of the same state.
- Every success, rejection, duplicate and failure outcome has a defined, honest customer-facing meaning.
- The deterministic layers (scope guard, tools, prechecks, confirmation, limits) are fully testable without an LLM.
- The main LLM is used only for conversations that belong to the product, which keeps cost and behavior focused (Section 13).

### 1.3 Non-goals (this checkpoint)
No LLM client, prompts, embeddings, retrieval, Streamlit code, runtime dependency, repository change, schema change, seed change, or database change. Checkpoint 10 findings that are unrelated to the decisions above (for example the `__all__` bug in `app/db/__init__.py`, packaging, and the relative database path) are listed in Section 17 and are deliberately not fixed here.

---

## 2. Layered architecture

```text
Streamlit frontend (other developer)
        │  only: app.backend public API  (Section 4)
        ▼
Backend service ── session registry, pending-action store, limits,
        │          scope guard (Section 13), response assembly
        ▼
Orchestrator (later) ── LLM + intent routing; sees tool schemas only
        │
        ▼
Tool layer (app/tools) ── ctx-bound, validated, normalized results (Section 5)
        │                    │                      │
        ▼                    ▼                      ▼
Repository (app/db)     Product catalog         RAG retriever (later)
        │               (Products.json)         (FAQ.md, Policies.md)
        ▼
SQLite via get_connection()
```

### 2.1 Dependency rules
1. The frontend imports only `app.backend` (its functions and its public result types). It must not import `app.db`, `app.tools`, `app.orchestrator`, `app.rag`, `sqlite3`, or read `data/` files.
2. Only `app.tools` (including its non-LLM session helpers, such as the login directory in Section 18, item 15) imports `app.db.repository`. The bootstrap command in Section 9 imports only `app.db.connection`, `app.db.schema` and `app.db.seed_data`. The health check in Section 9.4 uses only a dedicated read-only connection from `app.db.connection` and, if approved, a narrow internal demo-readiness diagnostic. It never uses `get_connection()`, the tool layer, or a customer-scoped repository function. No other module runs SQL, and no module except `app.db.connection` calls `sqlite3.connect()`.
3. The orchestrator and the LLM reach data only through registered tools. The LLM never receives a connection, a cursor, SQL, a customer ID, or a repository function.
4. Dependencies point downward only. The repository knows nothing about tools, the backend, or the LLM.
5. Public backend types are plain frozen dataclasses of strings, numbers, booleans, lists and tuples. They carry no `sqlite3.Row`, no repository dict, and no internal exception.
6. The scope guard (Section 13) runs inside the backend service, before the orchestrator. It imports no repository, tool, or LLM module and performs no I/O.

### 2.2 Proposed module layout
Nothing below exists yet. Names are proposals for approval. The README already lists `tools/`, `rag/`, `orchestrator/`, and `ui/`; `backend/` and `catalog/` are additions.

```text
app/
  backend/        # public entry points, public result types, session registry,
                  # pending-action store, scope guard                (Sections 4, 8, 11, 13)
  tools/          # CustomerContext, tool registry, validation, outcomes (Section 5)
  catalog/        # read-only Products.json loader and lookup      (Section 12)
  orchestrator/   # LLM + routing (later checkpoint)
  rag/            # retrieval over FAQ/Policies (later checkpoint)
  db/             # existing repository layer; one new read (Section 6)
  ui/             # Streamlit, owned by the frontend developer
```

---

## 3. Trusted customer identity

### 3.1 `CustomerContext`
An immutable object that carries the authenticated customer into the tool layer.

```text
CustomerContext(customer_id: str, session_ref: str)    # frozen
```

- Created only by the backend from a valid server-side session record (Section 11), which `start_session` creates after the simulated login selects a customer. Nothing else constructs one.
- The opaque random value that names the server-side session record (`session_id` in the session record) is the session handle (Section 3.3). The context does **not** carry it. The context carries `session_ref`, the short non-reversible reference described in Section 14.3, which is safe to log and cannot be used to reach the session. The backend alone uses the handle, internally, for session lookup and for binding pending actions (Section 8). It takes `customer_id` from the session record, never from anything the frontend sends.
- Every tool function takes the context as an explicit first parameter and passes `ctx.customer_id` to the repository as `authenticated_customer_id`.
- Every tool checks that its `ctx` is a real `CustomerContext` with a non-blank `customer_id`. Anything else is an internal error, not a business outcome.

### 3.2 Identity rules (security requirements)
1. The context is injected by backend code. It is never an argument the LLM can supply, and it never appears in any tool schema shown to the LLM.
2. The tool layer rejects, never ignores, any unexpected argument. A call that includes `customer_id`, `user_id`, `email` or any other unknown field fails with `invalid_input` and is not executed.
3. Customer IDs in message text, tool arguments, retrieved documents or conversation history are never parsed as identity. A customer who types another customer's ID gets the same result as a customer who types a nonexistent ID.
4. Order IDs and item IDs supplied by the customer or LLM are data, always filtered by the context customer inside the repository SQL (`docs/01` Section E). The tool layer never pre-checks ownership by reading other customers' data.
5. Switching the simulated customer ends the session. The frontend calls `start_session` again and discards the old state. The backend marks the old session ended and invalidates its pending actions (Section 11.2).
6. The simulated login is a labeled limitation, not authentication (`docs/00` Section 9). Anyone who can reach the login selector can choose any listed customer. The in-memory design in this document is valid for a single-process demo only (Section 8.7). Exposing the backend over a network, or running several workers, requires the durable design in Section 8.8, which this document does not implement.

### 3.3 The session handle is a bearer credential
`ConversationState` is an opaque handle to a server-side session record (Section 11). It is a **bearer credential** for the simulated session. Whoever holds a valid handle can act as that session's customer, within the demo's capabilities, until the session ends or expires. Copying or replaying a valid handle does not change the identity behind it, but it also does not stop another holder from using it. The handle is **not** non-transferable, and this document does not claim that it is.

Requirements for the handle (proposed):
1. It is generated with a cryptographically secure random source, with at least 128 bits of entropy.
2. It contains no customer ID, name, timestamp or any other sensitive or derivable data.
3. It is never shown in customer-facing output, never placed in a URL or query string, and never logged in full (Section 14.3).
4. It is not persisted in an insecure or public location. The frontend keeps it only in server-side Streamlit session memory and does not write it to a file, cookie, browser storage or shared store.
5. The backend checks the session's existence, status and expiry on every call (Section 4.3, step 2). An unknown, ended or expired handle is rejected with `invalid_session`.
6. Expiration (Section 11.2) and logout or customer switch revoke the handle. A revoked handle is never revived.
7. Neither the LLM nor any tool receives the handle.

This protects against guessing and against stale reuse. It does not protect against theft of a valid handle, which is a limitation of the simulated login. Real authentication is outside the MVP (`AGENTS.md`).

---

## 4. The frontend-facing backend interface

The frontend needs three calls. The conversation call, `handle_message`, is the one stable entry point. The other two exist because Streamlit needs a login list and a way to begin a session.

### 4.1 Entry points

```text
list_login_options() -> list[LoginOption]
start_session(customer_id: str) -> StartSessionResult
handle_message(
    state: ConversationState,
    message: str | None = None,
    *,
    decision: ConfirmationDecision | None = None,
    choice: ChoiceSelection | None = None,
) -> ChatReply
```

`list_login_options` and `start_session` exist only because login is simulated. They are the one place a customer ID is chosen by the frontend rather than by backend authentication, and they are not callable by the LLM.

### 4.2 Public types

| Type | Fields | Notes |
|---|---|---|
| `LoginOption` | `customer_id`, `display_name` | Simulated login selector only. Needs the customer directory read in Section 18, item 15. |
| `StartSessionResult` | `ok: bool`, `state: ConversationState \| None`, `greeting: str`, `error_code: str \| None` | `error_code` is `unknown_customer` or `unavailable`. On failure `state` is `None`. |
| `ConversationState` | opaque | A handle created by the backend and stored unmodified by the frontend (for example in `st.session_state`). It identifies a server-side session record (Section 11). It is not a source of authority: copying it grants nothing the original did not have, and it holds no pending action. The frontend never reads or edits it. |
| `ConfirmationDecision` | `action_id: str`, `accepted: bool` | Produced by a confirm or cancel button. Typed replies work too (Section 8.3). |
| `ChoiceSelection` | `prompt_id: str`, `option_id: str` | Produced by a button when the customer picks a return reason category (Section 7). Typed replies work too. |
| `ChatReply` | `text: str`, `state: ConversationState`, `status: str`, `error_code: str \| None`, `pending: PendingView \| None`, `choice: ChoiceView \| None`, `citations: tuple[Citation, ...]`, `request_id: str` | See below. |
| `PendingView` | `action_id: str`, `kind: str`, `summary: str`, `expires_at: str` | What the frontend shows next to confirm and cancel buttons. `action_id` is the random identifier of the server-side pending action (Section 8.1). |
| `ChoiceView` | `prompt_id: str`, `question: str`, `options: tuple[ChoiceOption, ...]` | A fixed set of options. `ChoiceOption` is `option_id` and `label`. |
| `Citation` | `source: str`, `section: str` | Source document and section for policy answers (filled once RAG exists). |

`ChatReply.status` is one of `ok`, `needs_choice`, `needs_confirmation`, `error`. `ChatReply.error_code` is empty unless the status is `error`. At most one of `pending` and `choice` is set.

### 4.3 `handle_message` contract
- Exactly one of `message`, `decision` and `choice` is supplied. Supplying none or more than one raises `ValueError`. A non-string `message` or a wrong-typed `state`, `decision` or `choice` raises `TypeError`. These are frontend programming errors.
- It returns a new `ChatReply` and never mutates the `state` it was given. The frontend replaces its stored state with `reply.state`.
- It never raises for database failures, tool failures, LLM failures, or business rejections. Those become `status="error"` or an ordinary `status="ok"` reply (Sections 7 and 15). Internal causes are logged, never returned.
- `reply.text` is untrusted text from the frontend's point of view. The frontend renders it as plain text or escapes it; it must not execute or interpret it as HTML or script.

Processing order for a call. Each step either finishes the call or passes control to the next.

1. **Type checks** (above).
2. **Session check.** Look the session up. If it is missing, ended, or past its expiry, return `status="error"`, `error_code="invalid_session"` without refreshing it, and without running anything else. Otherwise refresh `last_activity_at` (Section 11.2).
3. **`decision`.** Run the claim and execution steps in Section 8.4.
4. **`choice`.** Apply the selection to the session's open choice prompt (Section 7.2).
5. **`message`.** In this order:
   1. A blank or whitespace-only message is normal user behavior and not an exception. The reply asks the customer to type a question, with `status="ok"`. No LLM call is made.
   2. A message over the length limit (Section 5.3) gets a polite reply asking for a shorter message, `status="error"`, `error_code="message_too_long"`, and no LLM call. It is not added to history and does not count toward the per-session message cap.
   3. The per-session message cap (Section 5.3.3). When it is reached, the reply is `status="error"`, `error_code="rate_limited"`.
   4. If a choice prompt or a pending action is open and the message is an exact typed answer to it (Sections 7.2 and 8.3), handle it as that answer.
   5. The **Conversation Scope Guard** (Section 13). Greetings and acknowledgements, clearly out-of-scope messages, and ambiguous messages without support context are answered with fixed text and do not reach the orchestrator.
   6. Otherwise the message goes to the orchestrator (the main LLM), with the tool-call cap and other cost controls in Section 13.5.

### 4.4 Optional health check

```text
check_backend() -> BackendHealth      # status: "ready" | "not_initialized"
                                      #         | "stale_demo_data" | "unavailable"
```

Lets the frontend show a banner. It takes no customer or session argument and has no customer identity. It is strictly read-only: it never creates, seeds, resets, repairs or otherwise changes a database file, a directory, or any other part of the filesystem. It checks that the file exists before opening anything, opens an existing file only through a dedicated read-only connection (never `get_connection()`), and keeps its demo-readiness diagnostic separate from the customer-facing tools. See Section 9.4.

### 4.5 Error codes returned to the frontend

| `error_code` | Meaning | Customer-visible behavior |
|---|---|---|
| `message_too_long` | Message over the limit | Ask for a shorter message |
| `rate_limited` | Per-session cap reached (Section 5.3.3) | Explain the limit; offer a ticket where useful |
| `invalid_session` | Session is unknown, ended or idle-expired (Section 11) | Ask the frontend to start a new session |
| `unavailable` | Database or backend service failure | Generic apology; nothing was claimed or written unless stated |
| `internal` | Unexpected backend error | Generic apology; request ID shown for support |

**Contact details.** `FAQ.md` Q6.2 and `Policies.md` Section 7 contain the address `support@example.com`. It is a placeholder, not an operational support channel. Therefore:
- No customer-facing template, tool result or prompt in this design contains that address or any other contact detail. Where a template needs to send the customer elsewhere, it says "Please contact customer support."
- The assistant never invents an email address, telephone number, URL, opening hours or other contact channel, and it never presents a placeholder as a real channel.
- If the policy retriever returns text that contains the placeholder, the orchestrator and the response checks must not repeat it to the customer as an operational channel (the response-grounding checks belong to the orchestrator checkpoint).
- A real contact channel can be added only by a Technical Lead decision, as configuration owned by the project and not as text invented by the assistant (Section 18, item 23).

---

## 5. Tool layer

### 5.1 Principles
- Every LLM-visible tool is a thin, deterministic function over the repository, the catalog, or the retriever. It takes `ctx` first, then validated arguments.
- Tool schemas are strict. Unknown or extra fields are rejected (Section 3.2). Types are exact, with no coercion of numbers to strings.
- Tools return a `ToolResult`, never an exception and never a repository dict.
- Only fields the answer needs are returned. Stored free text written by customers or the LLM (a return `reason`, a ticket `description`, a `conversation_summary`) is not returned to the LLM, which closes the stored-text prompt-injection path.
- No LLM-visible tool performs a write, and no LLM-visible tool supplies a `reason_category`. Writes happen only after the customer confirms a server-side pending action (Section 8).

### 5.2 `ToolResult`

```text
ToolResult(
    status: "ok" | "not_found" | "rejected" | "invalid_input"
            | "needs_choice" | "needs_confirmation" | "unsupported" | "unavailable",
    code: str,              # stable machine code, see Section 7
    data: dict,             # small, display-ready fields; no raw repository rows
    note: str | None,       # short factual note for the LLM; never database text
)
```

### 5.3 Input normalization, limits and counters (D4)
These limits are enforced in the tool and backend layer before any repository call. The repository write contracts and the schema are unchanged. Lengths are counted in characters after sanitization.

#### 5.3.1 Approved limits (D4)

| Input | Rule | Approved limit |
|---|---|---|
| `reason` (return), customer-supplied text | Required, stripped. The limit applies to the customer's own text only. | 1–1,000 characters |
| `description` (ticket) | Required, stripped | 1–4,000 characters |
| `conversation_summary` | Built by the backend, not the LLM (Section 8.5). Omitted when empty. | 1–2,000 characters, or omitted |
| `issue_type` (ticket) | Required, stripped. Baseline is validated free text. | 1–100 characters |

**Customer text versus backend metadata.** The approved 1,000-character limit belongs to the text the customer supplies. The backend never reduces it for internal metadata. If the category is encoded into the existing `reason` column (Section 7.4), the prefix `[damaged] `, `[wrong_product] ` or `[standard] ` is backend-generated metadata. It does not count against the customer's 1,000-character allowance. The longest prefix, `[wrong_product] `, is 16 characters, so the longest stored `reason` string is **1,016 characters** (a 1,000-character customer reason plus the prefix). Neither the schema nor the repository write contract limits the length of `reason` (the repository checks only that it is a non-blank string), so the longer stored string needs no change to either. This checkpoint changes neither.

**Approved enum.** `reason_category` takes exactly the values `damaged`, `wrong_product` and `standard`. These values, and the windows they select, already belong to the approved Checkpoint 7A repository contract (`docs/01` Section F) and are not proposals here. What is proposed is only the customer-facing selection workflow in Section 7 (who sets the value and how), not the existence or meaning of the values.

#### 5.3.2 Separate proposals (not approved)
Each of the following is an additional or stricter rule that requires Technical Lead approval. None of them overrides a limit in 5.3.1.

| Input | Proposal | Status |
|---|---|---|
| `issue_type` | Restrict to the enum in Section 10.2, instead of validated free text of up to 100 characters. If not approved, the baseline in 5.3.1 applies. | **[Proposed]** |
| Customer chat message | Required text, at most 2,000 characters | **[Proposed]** |
| `order_id`, `item_id` | Strip whitespace, uppercase, must match `[A-Z0-9_-]+`, 1–20 characters. Otherwise `invalid_input`. | **[Proposed]** |
| Product search query | Required, stripped, 1–100 characters | **[Proposed]** |

#### 5.3.3 Per-turn limits and per-session limits
These are separate controls and are tracked separately.

| Scope | Limit **[Proposed]** | Counter increments when | Resets |
|---|---|---|---|
| Per turn | 6 tool calls | Each tool call the backend dispatches, whether it succeeds or not | At the start of each customer turn |
| Per session | 60 customer messages | A message passes the blank check and the length check (Section 4.3), including messages the scope guard answers | Never; ends with the session |
| Per session | 5 return requests | Only after `create_return_request` returns `created` (committed) | Never; ends with the session |
| Per session | 3 support tickets | Only after `create_support_ticket` returns `created` (committed) | Never; ends with the session |

Rules for the write counters:
- A counter for a write increments only after the repository confirms a committed write. A rejected, duplicate, failed or unconfirmed attempt does not consume capacity.
- **Capacity is reserved, not just counted.** One pending action per session does not prevent an overrun. A claimed action becomes `consumed` before its database write starts, and the shared lock is released before the repository call, so a second action can be staged and claimed while the first write is still running. Two distinct writes could then both pass a check against the committed counter alone. The server-side session record therefore holds, for each write kind, a **committed count** and a **reserved count**. The claim in Section 8.4 atomically reserves one slot, and it succeeds only if `committed + reserved < cap` for that kind. The write then runs without the lock, and the reservation is committed or released afterward (Sections 8.4 and 8.4.1). Committed writes plus reserved writes therefore never exceed the cap.
- The staging-time check (no staging when `committed + reserved` has already reached the cap) is only a courtesy, so the customer is not shown a confirmation that cannot succeed. The claim-time reservation is the authority.
- Reaching a cap returns `rate_limited`. For tickets, the reply says "Please contact customer support." and names no address (Section 4.5).
- Counters live in the server-side session record (Section 11), not in the frontend's copy of the state, so a copied state cannot reset them.

Text sanitization applies to every free-text input: Unicode NFC normalization, removal of NUL and other control characters except newline and tab, trimming, then the length check. A value that is empty after sanitization is `invalid_input`.

Order and item IDs are normalized only for convenience ("ord1001" becomes "ORD1001"). Normalization never widens access, because the repository still filters by the context customer.

### 5.4 LLM-visible tools

| Tool | Arguments (LLM-supplied) | Repository / source | Notes |
|---|---|---|---|
| `list_my_orders` | none | `list_customer_orders` | Summaries only: order ID, status, order date, total. |
| `get_order_status` | `order_id` | `get_order`, `get_order_items`, catalog | Status, delivery date, payment status, refund status and amount, items with product names. |
| `get_my_returns` | none | `get_customer_returns` | Return ID, order and item, status, date. No `reason` text. |
| `get_my_tickets` **[Proposed]** | none | `get_customer_tickets` | See Section 5.5. No `description` and no `conversation_summary`. |
| `start_return_request` | `order_id`, `item_id`, `reason` | Stores a return draft in the session record | No `reason_category` argument. Returns `needs_choice` so the customer selects the category (Section 7). Never writes. |
| `start_support_ticket` | `issue_type`, `description`, `order_id` (optional) | Stages a pending action | Never writes. Returns `needs_confirmation` (Section 8). |
| `get_refund_policy` | `order_id` (optional) | policy source, plus `get_order` when `order_id` is given | Policy facts with citations, and that order's refund status and amount if supplied. |
| `lookup_product` | `query` or `product_id` | catalog | Section 12. |
| `search_policies` | `query` | RAG retriever | Reserved. Contract defined in the RAG checkpoint. |

### 5.5 `get_my_tickets` (proposed)
The repository already has `get_customer_tickets(authenticated_customer_id)`, which returns the authenticated customer's tickets, most recent first, with the keys `ticket_id`, `customer_id`, `order_id`, `issue_type`, `description`, `conversation_summary`, `status`, `priority` and `created_at`. A customer who created a ticket needs to be able to ask about it later, and `get_my_returns` has no ticket counterpart. This proposal adds one tool. It is an additive use of an existing repository operation and does not change that operation or its contract.

Requirements:
- **No arguments.** There is no customer ID, ticket ID or filter argument. Identity comes only from the injected `CustomerContext`, and the tool calls `get_customer_tickets(ctx.customer_id)`. A call that carries any argument is rejected with `invalid_input` (Section 3.2).
- **Minimum fields returned to the LLM:** `ticket_id`, `issue_type`, `order_id` (the linked order reference, or none), `status`, `priority` and `created_at`. Nothing else.
- **Never returned:** the stored `description` and `conversation_summary`, and the `customer_id`. These are customer- and LLM-written text that could carry injected instructions (Section 5.1), so the tool drops them in the tool layer before building the result.
- **Size bound.** Only the most recent tickets are returned, up to a limit **[Proposed]** of 10, with the total count, so a long list cannot inflate the LLM context.
- **Behavior.** The customer can ask whether a ticket they created is open, in progress, resolved or closed, using the stored `status`. The assistant reports only what the record says. It does not predict resolution times, promise follow-up or describe actions taken by support staff that the record does not show.
- **Failures.** `sqlite3.Error` or `ConnectionError_` is caught in the tool layer and returned as `unavailable`, with no database text (Section 15).
- **Isolation.** Another customer's tickets are never returned, because the repository filters by `customer_id` in SQL. A customer who asks about another customer's ticket ID gets the same reply as for a nonexistent one, because the tool offers no way to look up a ticket by ID.

The ticket-history tool also narrows the lost-reply gap in Section 8.5 for customers: after a crash a customer can list their tickets to see whether one was created, which lets them avoid a duplicate. It is not a substitute for idempotent execution (Section 8.8).

### 5.6 What the LLM-visible registry excludes
The prechecks in Section 6 are not an LLM-visible tool. The backend calls them itself, after the customer has selected a category. The LLM-visible registry contains no `create_return_request`, no `create_support_ticket`, and no way to supply or change a `reason_category`. Those repository writes are reachable only from the confirmation handler (Section 8.4), which takes its arguments from the claimed server-side pending action and not from the LLM or the customer's message.

---

## 6. Return eligibility prechecks: new repository read (D1)

### 6.1 Contract

```text
check_return_eligibility(
    authenticated_customer_id,
    order_id,
    item_id,
    *,
    reason_category,
    reference_time=None,
) -> dict
```

Read-only. It takes the same inputs, applies the same validation, and uses the same time-window rules as `create_return_request` (`docs/01` Section F), but never writes. The function name follows D1. Its successful outcome is deliberately named `prechecks_passed`, not `eligible`, because it does not establish full eligibility (Section 6.3). An alternative function name, `check_return_prechecks`, is listed in Section 18, item 13. It follows the same check order, stopping at the first rejection:

1. Validate inputs (before any connection).
2. Order ownership. Nonexistent and unowned both give `not_found`.
3. Item belongs to the order, otherwise `item_mismatch`.
4. An active return for the item (`Requested` or `Approved`) gives `duplicate`.
5. Time window from `delivered_at` and `reason_category`, with inclusive boundaries and the same handling of NULL, future and malformed timestamps as the write.

Validation and reference-time rules are exactly those in `docs/01` Section F: wrong types raise `TypeError`, invalid values raise `ValueError` (including an invalid `reason_category`), `reference_time=None` reads the real UTC clock exactly once, a supplied time must be timezone-aware and is normalized to UTC and truncated to whole seconds.

### 6.2 Proposed result contract

```text
{
  "outcome": "prechecks_passed" | "not_found" | "item_mismatch"
             | "outside_window" | "duplicate" | "failed",
  "reason_category": "damaged" | "wrong_product" | "standard",
  "window_hours": 48 | 168 | None,
  "delivered_at": "YYYY-MM-DDTHH:MM:SSZ" | None,
  "window_closes_at": "YYYY-MM-DDTHH:MM:SSZ" | None,
  "evaluated_at": "YYYY-MM-DDTHH:MM:SSZ" | None,
  "window_reason": "expired" | "delivery_not_recorded"
                   | "delivery_after_reference" | None,
  "existing_return_status": "Requested" | "Approved" | None,
}
```

The field `window_closes_at` replaces the Revision 1 name `eligible_until`, because the time is the end of the time window and not a promise of eligibility.

**`reason_category` in every outcome.** `reason_category` is the validated input, echoed back in every returned result, including `not_found`, `item_mismatch`, `duplicate` and `failed`. It is validated before any database access, so it is always one of the three enum values in a returned result, and it discloses nothing about stored data or other customers. When validation fails, nothing is returned, because the function raises `TypeError` or `ValueError` as the write does. The echo lets the caller keep the category with the result without re-deriving it, and it never replaces or changes the category.

| Outcome | `window_hours` | `delivered_at` | `window_closes_at` | `evaluated_at` | `window_reason` | `existing_return_status` |
|---|---|---|---|---|---|---|
| `prechecks_passed` | set | set | set | set | `None` | `None` |
| `outside_window`, expired | set | set | set | set | `expired` | `None` |
| `outside_window`, delivery in the future | set | set | set | set | `delivery_after_reference` | `None` |
| `outside_window`, `delivered_at` NULL or malformed | set | `None` | `None` | set | `delivery_not_recorded` | `None` |
| `duplicate` | `None` | `None` | `None` | `None` | `None` | set |
| `not_found`, `item_mismatch`, `failed` | `None` | `None` | `None` | `None` | `None` | `None` |

`window_hours` is set only when the window was actually evaluated. `window_reason` exists only on this read; the write's contract and result are unchanged and still return only `outside_window`. Only the owner's own data is ever returned, so no field discloses anything about another customer. A database failure gives the generic `failed` result with no database text, the same convention as `create_return_request` (Section 18, item 14 lists the alternative).

### 6.3 What `prechecks_passed` means, and what it does not
`prechecks_passed` establishes exactly four facts at the moment of the check:

1. The order belongs to the authenticated customer.
2. The item belongs to that order.
3. There is no active duplicate return (`Requested` or `Approved`) for the item.
4. The applicable delivery-time window, 48 hours for `damaged` and `wrong_product` or 168 hours for `standard`, has not closed.

It does **not** establish that the item is returnable under every policy condition. The product catalog and the repository do not represent non-returnable product categories, and nothing checks the product's condition, packaging, or the other conditions in `Policies.md` Section 2 (`docs/01` Section L, item 1). The final review of a return is outside this system.

Requirements that follow:
- Customer-facing text for `prechecks_passed` says that the **preliminary checks** or **time-window checks** passed, names what they covered, and states that policy conditions still apply. It never says or implies that the item is eligible, approved, accepted, guaranteed, or definitely returnable. The templates in Section 7.5 are the only permitted wording.
- The tool result sent to the LLM carries the code `prechecks_passed` and a fixed note that forbids "eligible" or "approved" wording.

### 6.4 Relationship to `create_return_request`
- The check is **advisory**. Time passes and other sessions act between a check and a create. The final `create_return_request` write remains authoritative. It re-validates every rule inside its own `BEGIN IMMEDIATE` transaction. A check that returned `prechecks_passed` can still be followed by `outside_window` or `duplicate` at create time, and the backend must handle that (Section 8.4).
- The time-window rules must have a single source of truth. The implementation checkpoint extracts the window table and window evaluation so the check and the write share them. Any change to `create_return_request` code in that checkpoint must leave all 290 existing return and ticket tests (188 in `tests/test_returns.py`, 102 in `tests/test_tickets.py`) unchanged and passing.
- This is an additive operation. It does not change the approved write contracts, outcomes, result shapes, ID formats or transaction behavior.

### 6.5 Required later amendments (not made now)
These are required before implementation and must be approved and committed separately. `docs/01` and `docs/02` are not modified by this checkpoint.
- **`docs/01` Section F:** add `check_return_eligibility` with the contract in 6.1, the result in 6.2, the meaning and limits of `prechecks_passed` in 6.3, and the statement that `create_return_request` remains authoritative.
- **`docs/02` Section 4:** add the function to the repository interface and the implementation sequencing, the shared window helper, and the parity and read-only tests listed in Section 16.
- Both documents also need the `list_customers_for_login` text if Section 18, item 15 is approved.

### 6.6 Known limitation (unchanged)
Category-based non-returnable enforcement is still an MVP limitation (`docs/01` Section L, item 1). Where the customer asks about non-returnable categories, the assistant quotes `Policies.md` Section 2 and offers a support ticket. It does not state a returnability verdict for a specific product.

---

## 7. Return reason category: customer selection and outcome mapping

### 7.1 Why the LLM cannot decide
The category decides the time window. The repository trusts `reason_category`, and the 48-hour and 168-hour windows differ. The category therefore must not come from an LLM guess, a keyword guess, or a silent default. It is set only by an explicit customer selection.

### 7.2 Selection flow
1. The customer asks to return an item. The LLM may call `start_return_request(order_id, item_id, reason)`. That stores a return draft in the session record and returns `needs_choice`. The draft has no category.
2. The backend replies with a fixed choice prompt. The category options are:
   - **Damaged or defective item** (`damaged`)
   - **Wrong product received** (`wrong_product`)
   - **Other / standard return reason** (`standard`)
3. The customer selects an option, using a button (`ChoiceSelection`) or a typed reply. A typed reply counts only when it exactly matches an option's label, its option number, or its category word, matched deterministically, case-insensitively and over the whole message, while the prompt is open. The exact accepted words are an implementation detail to be approved with the tests.
4. Any other reply to the prompt, including a message that merely describes the problem in free text, is treated as **ambiguous**. The backend asks the customer again to select one of the three options. The LLM may help phrase the clarification, but it never selects the category, it never changes it, and the tool layer never accepts a category from it. Nothing is inferred from a customer's wording even when it looks clear: the customer still selects.
5. Only after the selection does the backend call the prechecks in Section 6 with that category. The category is recorded in the draft and is never changed afterward without a new selection.

### 7.3 Changing the category
- The category is never changed silently after a check. If the selection is `damaged` and the 48-hour window has passed but the 7-day window has not, the reply explains that the damaged-item window has passed (Section 7.5) and offers the choice prompt again. A new selection restarts the prechecks.
- The standard return policy requires the item to be unused (`Policies.md` Section 2), so the assistant states that a damaged item may not meet that condition. It does not decide this for the customer, and it requires a fresh selection and a fresh confirmation under the new category.

### 7.4 Stored reason and the confirmation preview
- The selected category is shown in the confirmation preview (Section 8.2) in words, for example "Reason category: Damaged or defective item (48-hour window)". The customer sees it before approving.
- The category is not a repository field. The tool boundary prefixes the stored reason with it, for example `[damaged] <customer reason>`, so support staff can see it. The prefix is backend-generated metadata, not customer input. It does not consume the customer's approved 1,000-character allowance, and the longest stored string is 1,016 characters (Section 5.3.1). Customer text is always validated before the prefix is added, and the prefix is never taken from customer or LLM text. This is a tool-layer convention only; the repository stores the reason as given and its contract and the schema are unchanged. The prefix convention is a proposal for approval (Section 18, item 9).
- Photo evidence (`FAQ.md` Q3.4) cannot be uploaded here. The assistant says so and suggests a ticket where the customer wants support to follow up.

### 7.5 Outcome-to-message mapping: return prechecks and return creation
These apply to `check_return_eligibility` and to the result of `create_return_request` after confirmation. Messages are templates. The LLM may paraphrase tone but not facts, and never for outcomes that were not returned.

| Outcome | Meaning | Customer-facing meaning | Writes? |
|---|---|---|---|
| `prechecks_passed` (check) | The four facts in Section 6.3 hold now | "The preliminary checks passed: this item is in an order on your account, there is no open return for it, and it is inside the `<48-hour / 7-day>` window, which closes `<time> UTC`. Other return conditions still apply." Then show the confirmation preview. Never "eligible" or "approved" | no |
| `created` | Row committed | "Your return request `<RET-…>` has been recorded with status Requested." States what the system did not do (Section 10.4). | yes |
| `not_found` | Order not on this account, or does not exist | "I couldn't find that order on your account." Identical for nonexistent and unowned orders | no |
| `item_mismatch` | Item not in that (owned) order | "That item isn't part of order `<ORDER>`." Offers the order's actual items | no |
| `outside_window`, `expired` | The window closed | "The `<48-hour / 7-day>` window for this category closed on `<time> UTC`." Where the category is `damaged` or `wrong_product` and the 7-day window is still open, offer the choice prompt again (Section 7.3). Offer a ticket | no |
| `outside_window`, `delivery_after_reference` | The recorded delivery time is later than now | "The delivery time recorded for this order is later than the current time, so I can't check a return window." Offer a ticket | no |
| `outside_window`, `delivery_not_recorded` | `delivered_at` is NULL or malformed | "I can't find a valid delivery time recorded for this order, so I can't check the time window." May state the order's recorded status. Offer a ticket. Never says the order "has not been delivered" or "is not delivered" | no |
| `duplicate` | Active return already exists for the item | "You already have a return request for this item" with its status. Offer to look at returns. No second request | no |
| `failed` | Database or connection failure | "Something went wrong and I couldn't complete that. Nothing was submitted." Offer a ticket or a retry. Never claims success | no (or rolled back) |

Notes:
- A missing or malformed `delivered_at` means only that a valid delivery timestamp is not recorded. It can occur for an order that is in transit, for a delivered order whose timestamp is missing, or for corrupted data. The assistant does not infer which.
- In the write path (`create_return_request`), the repository returns the single outcome `outside_window` and no detail. The backend maps it to a neutral message: "I can't confirm that this item is still inside its return window, so nothing was submitted." It does not reuse a detail from an earlier check, which may be stale, and it does not guess a specific cause. It may offer to run the check again.
- The repository returns `created` only after `COMMIT`, so a `failed` result means no return was recorded. The reply says so.
- `reason_category` accompanies every outcome in the check (Section 6.2). In customer text the category appears only where it helps, for example in the window message.

---

## 8. Server-side pending actions and the confirmation workflow (all writes)

Every write the assistant performs on the customer's behalf, return requests and support tickets alike, uses one mechanism. The LLM can propose a write. It cannot execute one.

### 8.1 Why `ConversationState` cannot be the authority
A pending action held only in the frontend's `ConversationState` is not sufficient. The frontend may hold several copies of the same state, a user may double-click, two browser requests may be in flight, and an old copy can be replayed. If each copy contained its own pending action, every copy could execute it. A random identifier alone, in a copied state, would not prevent this either, because each copy would carry a valid identifier and nothing would record that it was already used.

The authority for a pending action is therefore a **server-side pending-action store**. `ConversationState` carries only a handle to the session (Section 11). The state never contains the action's parameters, and a copy of it can never be used to execute or revive an action.

### 8.2 Pending action record
Each record is created by the backend, never by the LLM or the frontend.

```text
PendingAction(
    action_id: str,         # random, unguessable; at least 128 bits from a CSPRNG
    kind: "return_request" | "support_ticket",
    customer_id: str,       # from the session record; checked again at claim
    session_id: str,        # the creating session; checked again at claim
    params: <frozen, validated, normalized arguments>,
    summary: str,           # exactly the text shown to the customer for approval
    created_at: <UTC>,
    expires_at: <UTC>,      # [Proposed] created_at + 10 minutes
    status: "pending" | "consumed" | "expired" | "cancelled",
    consumed_at: <UTC> | None,
    reservation: "held" | "committed" | "released" | None,   # write-capacity slot, Section 8.4
    outcome: str | None,    # repository outcome code, set after the write; None if unknown
    result_id: str | None,  # RET-… or TKT-… after `created`
)
```

Requirements:
- `action_id` is generated with a cryptographically secure random source. It is not derived from the customer, the session, a counter or the time.
- The record is bound to the authenticated customer and to the session that created it.
- `params` are validated and normalized at staging time and then frozen. Nothing edits them afterward.
- `expires_at` is fixed when the action is staged. It is not extended by later activity. The session idle timeout (Section 11) is separate.
- At most one pending action is open per session. Staging a new one marks the previous one `cancelled`, and the two changes happen as one atomic transition (Section 8.4.1).

The `PendingView` shown to the customer is built from the record's `summary`. The preview must match what will actually be written:

**Return preview** shows: product name and order, item, the selected **reason category in words** (Section 7.4), the customer's reason text, the window and when it closes (stated as preliminary), and a statement that the request will be recorded with status Requested.

**Ticket preview** shows all of the following, exactly as they will be stored:
1. the issue type;
2. the description text;
3. the order reference the customer supplied, if any, together with a note that whether it is linked to the customer's account is checked only when the ticket is created;
4. the full conversation summary that will be recorded with the ticket, or a statement that none will be recorded.

### 8.3 Return flow and decisions

```text
 customer asks to return an item
        │
 resolve order and item   (ask when the order has several items or the order is unclear)
        │
 start_return_request ──► draft stored ──► reply status "needs_choice"
        │
 customer selects reason category   (Section 7.2; not the LLM)
        │
 backend calls check_return_eligibility with that category
        │
        ├─ not prechecks_passed ──► explain (Section 7.5); no pending action
        │
        ▼ prechecks_passed
 stage PendingAction in the store ──► reply status "needs_confirmation"
        │      (preview includes the selected category)
        ▼
 customer decision
   ├─ accepted ──► claim, then execution handler (8.4)
   ├─ declined ──► mark cancelled; "I've cancelled that. Nothing was submitted."
   └─ expired / other ──► see below
```

Decisions:
- **Buttons.** The frontend sends `decision=ConfirmationDecision(action_id, accepted)`.
- **Typed replies.** While a pending action is open, a message that is exactly one of a small fixed set of affirmative words (for example "yes", "confirm") confirms, and one of a fixed set of negative words ("no", "cancel") declines. Matching is deterministic, case-insensitive and whole-message only. The LLM never interprets a confirmation. The `action_id` for a typed reply comes from the session record's current pending action, never from the message text. The exact word lists are an implementation detail to be approved with the tests.
- **Other messages while an action is pending.** A message that is not a typed answer is first classified by the scope guard (Section 13):
  - greetings, acknowledgements, clearly out-of-scope messages, and ambiguous messages answered with fixed text do **not** cancel the pending action. The fixed reply includes the pending preview again, so the customer sees that a request is waiting;
  - a message that continues to the orchestrator marks the pending action `cancelled` with a one-line notice ("I cancelled the pending request") and is then handled normally. This prevents a stale confirmation from firing later.
- **Expiry.** An action past `expires_at` is marked `expired` and can never be claimed. A decision for it gets a "that request expired" reply. Nothing is written.
- **Replay, tampering, wrong customer.** A decision for an `action_id` that is unknown, belongs to another customer or session, is cancelled, or is already consumed does not execute anything. Unknown, wrong-customer and wrong-session cases give the same plain reply, "that request is no longer active", so nothing is revealed about other customers' actions. A decision for an action the same customer already consumed gets "that request was already processed" and, when known, its result ID.

### 8.4 Claim and execution handler
Runs only on a valid accepted decision. The order of steps is fixed. In particular the action is consumed **before** the write is attempted, so a request can lose a write but can never duplicate one.

1. **Claim and reserve capacity, atomically.** Call `claim(action_id, customer_id, session_id, now)` on the store. In **one** indivisible critical section it succeeds only if all of these hold: the action exists, `status == "pending"`, its `customer_id` and `session_id` equal those of the current session, `now < expires_at`, and the session has write capacity for the action's kind, that is `committed + reserved < cap` (Section 5.3.3). On success it sets `status = "consumed"`, `consumed_at = now` and `reservation = "held"`, and increments the session's `reserved` count for that kind by one. On failure it changes nothing, except in the capacity case below, and returns the reason (`unknown`, `wrong_owner`, `expired`, `consumed`, `cancelled`, `rate_limited`). Exactly one of any number of concurrent requests for the same action can succeed. Every other request, whether it arrives at the same instant or later, fails the claim.
2. If the claim failed, reply as in Section 8.3 and **do not write**. Stop. If the only reason was capacity (`rate_limited`), the action is set to `cancelled` in the same critical section, so it is never left `consumed` without a write, and the reply is `rate_limited`.
3. No lock is held from here until step 5. No repository, SQLite or LLM call is ever made while a lock is held.
4. Call the repository write with arguments taken **only** from the claimed record's frozen `params`. The confirmation message, the decision's other fields, and the LLM contribute nothing to the arguments. `reference_time` is supplied explicitly from the injected clock (Section 9.5) and read once.
5. **Finalize, atomically, in a `finally` that always runs** once the repository call returns or raises. In one critical section:
   - the repository outcome is `created`: record the outcome and result ID, increment the session's **committed** count for the kind, decrement its **reserved** count, and set `reservation = "committed"`;
   - any other outcome (`not_found`, `item_mismatch`, `outside_window`, `duplicate`, `failed`): record the outcome, decrement the **reserved** count, set `reservation = "released"`, and do **not** increment the committed count;
   - an unexpected exception from the call: the approved repository contract rolls back before re-raising, so nothing was committed. Treat it as a non-`created` outcome (outcome unset, reservation released), then let the backend boundary handle the exception (Section 15).

   Because the finalize step is in a `finally`, no exception path leaves a reservation held while the process is alive. The outcome record is a separate step from the SQLite write in step 4 and is **not** atomic with it (Section 8.5).
6. Map the repository outcome to the reply using Section 7.5 or Section 8.6.
7. Write the audit log entry (Section 14.3). The write counters change only in step 5.

**Why this prevents the concurrent-write overrun.** Suppose a ticket cap of 3 with 2 tickets committed. Action A is claimed: `committed (2) + reserved (0) < 3`, so the reservation succeeds and `reserved` becomes 1. While A's write is still running (no lock held), action B is staged and its confirmation arrives. B's claim sees `committed (2) + reserved (1) = 3`, which is not below the cap, so B is cancelled with `rate_limited` and never writes. If A returns `created`, `committed` becomes 3 and `reserved` 0, and the cap holds. If A fails or is rejected, `reserved` returns to 0, `committed` stays 2, and a later action can use the slot. At no instant do committed plus reserved writes exceed the cap.

**Timeout and hung calls.** SQLite's configured connection timeout (the busy timeout) bounds only how long the call waits for a database **lock**. It is not a deadline for the whole repository operation, and this design does not claim that every repository call is bounded by it. The actual timeout must be verified against the existing connection factory when the reservation is implemented. At the time of this review, the factory's `sqlite3.connect(db_path)` call in `app/db/connection.py` passes no `timeout` argument, so the standard library default would apply. That is an observation to re-verify, not a guarantee. A call that hangs for any other reason keeps its reservation until the call completes or the process stops. That fails safe, because the cap is never exceeded. The design deliberately does **not** reclaim a reservation by lease timeout, because a write that is merely slow could still commit afterward and exceed the cap. A process restart loses the in-memory sessions and reservations, which is the existing MVP boundary (Section 8.7).

**Session end or expiry while a write is in flight.** Ending or expiring a session does not discard a reservation that is still held. The session record stays on the server as a non-active record until its outstanding reservations are finalized (Section 11.2), and the consumed action stays available so that the finalize step in step 5 can record the outcome and commit or release the reservation. The session handle is invalid from the moment the session ends or expires, so the record accepts no new customer request. Pending, unclaimed actions of that session are invalidated at once. Only the already claimed, in-flight write is allowed to finish.

For returns, the repository re-checks every rule inside its transaction. If the window closed or another session created a return since the check, the customer receives the `outside_window` or `duplicate` message and nothing is written. The reply never claims a return exists unless the outcome was `created`.

### 8.4.1 Atomicity of all state transitions
Atomic `claim` is not the only transition that must be consistent under concurrency. Every transition below changes the pending-action store (and, where stated, the session record), and each must be one indivisible step. Two concurrent requests must never observe or produce a state that no sequential order of the same requests could produce.

| Transition | What must be atomic | In-memory design (MVP) | Durable design (later) |
|---|---|---|---|
| Stage a new action | Cancel the session's previous pending action, insert the new one and set `pending_action_id` on the session record, as one step. The session never has two pending actions, and never has a pointer to a missing one | One critical section under the shared lock | One transaction: a conditional update of the old row to `cancelled`, an insert of the new row, and the session pointer update |
| Claim and reserve capacity | `pending` to `consumed` only if owner, session, status and expiry match **and** `committed + reserved < cap` for the kind, together with incrementing `reserved` and setting `reservation = "held"`. A capacity failure moves the action to `cancelled` in the same step (Section 8.4) | One critical section under the shared lock | One transaction: the conditional `UPDATE ... WHERE ... AND status = 'pending' AND expires_at > ?` plus a conditional update of the session's reserved count that succeeds only if `committed + reserved < cap`. Both must change exactly one row, or the transaction is rolled back |
| Finalize the write (commit or release the reservation) | For a consumed action whose `reservation` is `held`: on `created`, increment `committed`, decrement `reserved` and set `committed`; otherwise decrement `reserved` and set `released`. Only once per action. A second finalize for the same action changes nothing | One critical section under the shared lock, run in a `finally` after the repository call, never while the call is in progress | A transaction or conditional updates, with the same once-only condition. See Section 8.8 for why this step is still separate from the business write in the MVP |
| Decline or cancel | `pending` to `cancelled` only if the action is still `pending` and owned by the session. A decline racing a claim has exactly one winner: if the claim wins the action is `consumed` and the decline is told it was already processed | One critical section under the shared lock | One conditional update, with the same single-row success rule |
| Expire | `pending` to `expired` only if `now >= expires_at` and still `pending`. A lazy sweep racing a claim must not expire a consumed action or let an expired one be claimed | One critical section under the shared lock; the claim checks expiry itself, so a missed sweep cannot allow an expired claim | A conditional update, with the claim's own `expires_at > ?` condition as the authority |
| End or expire a session | Mark the session `ended` or `expired` and mark all its `pending` actions `expired` or `cancelled`, as one step. No action of a dead session stays claimable. Actions already `consumed` with `reservation = "held"` are **not** touched, and the session record is **retained** as non-active until they finalize | One critical section covering the session record and its actions, under the same lock | One transaction over the session row and its pending actions, leaving consumed actions with a held reservation unchanged |
| Purge a finished session | Remove a non-active session and its terminal actions only when no action of the session has `reservation = "held"`. A purge pass may run between a claim and its finalize, but it must retain this session and its held-reservation action. | One critical section under the shared lock | A conditional delete with the same "no held reservation" condition |
| Record the outcome | Part of the finalize step above: `outcome` and `result_id` are set on an already `consumed` action, only once. A second outcome write for the same action is ignored | One critical section under the shared lock | A conditional update, and see Section 8.8 for why this step alone is not sufficient |

Rules for the in-memory design:
- Related operations are done under **one** shared lock, and a multi-step transition holds that lock for its whole duration. A check performed under one lock acquisition followed by a change under another is a defect.
- The lock covers the session registry and the pending-action store together. This avoids a lock-order problem between them and makes "end session and invalidate its actions" one step.
- No repository or SQLite call, and no LLM call, is made while the lock is held. The lock protects only in-memory state. The write in Section 8.4, step 4, happens after the claim has released the lock. Because the lock is released during the write, the per-session write cap is enforced by the capacity reservation in the claim and finalize steps, **not** by the lock and **not** by the one-pending-action rule.
- The reserved and committed counts for a session are changed only inside these critical sections, and `committed + reserved` never exceeds the cap for any kind.
- Terminal states (`consumed`, `expired`, `cancelled`) never move back to `pending`.
- **Cleanup must not destroy in-flight accounting.** A `consumed` action whose `reservation` is `held` is never purged, and the session record that holds its reserved count is never purged while any of its actions has `reservation = "held"`. The session record is kept as a non-active record (status `ended` or `expired`). Its handle stays invalid and accepts no new request. The cleanup rule is checked under the same lock as the other transitions.

Rules for a durable design: each transition is a transaction or a conditional update whose success is decided by the number of rows changed, never by a read followed by a separate write.

### 8.5 Failure between consumption and the write
Consuming first is deliberate: the design prefers at-most-once dispatch. The consequences are defined here.

| Point of failure | State afterward | Customer experience |
|---|---|---|
| Repository returns `failed` (database or connection error; the write was rolled back) | Action `consumed`, outcome `failed`, no row written | "I couldn't complete that. Nothing was recorded." The customer must start the request again, which creates a new action. The backend never retries automatically |
| A non-database exception is raised in the write (repository rolled back and re-raised) | Action `consumed`, outcome unset, nothing committed | The backend boundary catches it, logs it with the request ID, and replies `error` / `internal`: "I couldn't complete that, and I can't confirm whether it was recorded. Please check your returns (or your tickets, if `get_my_tickets` is approved) before trying again." The action is not reusable |
| The process stops after the claim and before the write | The in-memory store is lost with the process. No row was written | After restart the session is gone (`invalid_session`, Section 11). Nothing was written and nothing can be replayed |
| The process stops after the write committed and before the reply | A row exists. The record that said so is lost | The customer never saw the ID. For returns, asking again gives `duplicate` with the existing return's status, because of the repository's active-return guard (a property of the return write, not of the pending-action store). For tickets there is no duplicate guard in the repository, so a retry can create a second ticket. The customer can check with `get_my_tickets` if it is approved, but the system does not prevent the duplicate |

A consumed action is never reused or retried by the backend. A claim that succeeded and then lost its write is a lost write, not a duplicate write. In every row that is not a process stop, the finalize step in Section 8.4 releases the action's write-capacity reservation, so a failed or rejected attempt does not consume the customer's session cap.

**Explicit MVP limitation.** The single-process MVP does **not** have crash-safe idempotency. It has an atomic claim and at-most-once dispatch within one process lifetime. It has no idempotent business execution and no recoverable result. The record in step 5 of Section 8.4 and the SQLite write in step 4 are two separate durable-or-not operations, so a crash between them loses the link between the action and the business record. A lost reply, and for tickets a duplicate ticket on retry, are known and accepted limitations of the demo.

### 8.6 Support tickets
- `start_support_ticket` validates the issue type, description and optional order ID (Section 5.3), builds the `conversation_summary`, stages a pending action, and returns `needs_confirmation` with the preview in Section 8.2.
- The summary is built by the backend from structured state: the issue type, order IDs the customer mentioned that tool lookups confirmed, and a truncated tail of the customer's own recent in-scope messages. It never includes raw tool output or LLM text, and is capped at 2,000 characters (Section 5.3.1). The customer sees the full summary before approving.
- An order ID the customer supplies is checked for ownership only inside `create_support_ticket`, as `docs/01` Section G requires. An unowned or nonexistent order produces a general ticket with no order link, and the reply is the same in both cases: "I've logged your ticket. I wasn't able to link it to a specific order, so a support agent will follow up for the order details." It never says the order belongs to someone else.

Ticket outcome mapping:

| Outcome | Customer-facing meaning |
|---|---|
| `created`, order linked | "Your ticket `<TKT-…>` has been created and linked to order `<ORDER>`." |
| `created`, not linked (no order, or unowned or nonexistent order) | "Your ticket `<TKT-…>` has been created." Add the linking sentence above only when the customer supplied an order ID that was not linked |
| `failed` | "I couldn't create the ticket. Nothing was recorded. Please contact customer support." No address or other contact detail is given (Section 4.5). |

Ticket creation has no duplicate, rejection, or eligibility outcome in the repository. The session cap (Section 5.3.3) is the only rejection, and it is a tool-layer `rate_limited` result.

### 8.7 In-memory store: requirements and limitations
This checkpoint adds no database table and no schema change. The first implementation may therefore keep the pending-action store (and the session registry, Section 11) in process memory. That is acceptable for the single-process demo only, and only if all of these hold:

- It is a **process-global** object guarded by a lock. It must not live in Streamlit's per-session `st.session_state`, because the claim must see every request in the process.
- `claim` is one critical section: look up, check, and mark `consumed` happen under the same lock acquisition. A check-then-set across two lock acquisitions is a defect.
- Expired, cancelled and finalized records are purged on access and periodically, and the store has a size bound, so it cannot grow without limit. A `consumed` action with `reservation = "held"` is never purged, and a non-active session that still has such an action is retained until it finalizes (Sections 8.4.1 and 11.2). Purging happens under the shared lock and never while a repository call is made.

Limitations that must be stated plainly:
- **Process restart:** all sessions and pending actions are lost. This fails safe: nothing is written, and the customer must start a new session.
- **Several workers or processes:** each worker has its own store. A decision routed to a different worker finds no such action (`unknown`) and writes nothing. That fails safe too, but it makes the feature unreliable, and nothing prevents a state handle from reaching a worker that has no session for it. The in-memory design provides **no** cross-process replay or concurrency protection and must not be deployed with more than one worker.
- **No audit trail** survives a restart, apart from the application logs (Section 14.3).
- **No crash-safe idempotency or recoverable result** (Sections 8.5 and 8.8). The store provides an atomic claim and at-most-once dispatch only, and only inside one process.

### 8.8 Durable store requirement (not designed or implemented here)
A deployment with more than one process, restart tolerance, or an audit requirement needs more than the in-memory store. It would be designed and implemented in a later checkpoint and needs approved schema and repository changes, which this checkpoint does not make. Sections 8.8.1 to 8.8.3 state the requirements and the reasoning. Nothing here changes an existing repository write signature or the approved write contracts.

#### 8.8.1 Four separate properties
These are four different properties. None of them guarantees any of the others, and the design must not claim that one provides another.

| Property | Meaning | MVP in-memory store | Durable pending-action store alone |
|---|---|---|---|
| **Atomic claim** | Only one request can claim a given action | Yes, within one process (Section 8.4) | Yes, with a conditional update that succeeds only when exactly one row changed |
| **At-most-once dispatch** | The backend does not execute the same claimed action again | Yes, within one process lifetime | Yes, because a claimed action is never claimed again |
| **Idempotent business execution** | Repeating the same action identifier never creates another business record | **No** | **No** |
| **Recoverable result** | After a crash, the customer can retrieve the original outcome and result ID | **No** | **No** |

A durable pending-action store makes the first two survive restarts and multiple workers. It does not provide the last two, because the pending-action store and the SQLite business write are two separate transactions.

#### 8.8.2 The remaining failure, even with a durable store
1. The pending action is atomically claimed and recorded as `consumed` in the durable store.
2. `create_support_ticket()` commits a ticket row.
3. The process crashes before the pending-action record stores the ticket ID and the outcome.
4. The ticket exists, but the action record says only `consumed`. The customer never received the ticket ID.
5. The backend does not re-dispatch the consumed action (at-most-once holds). The customer, seeing no reply, retries by starting a new request, which stages a new action with a new identifier. The new action's write has no way to know that the first ticket exists, so it creates a second ticket.

The same crash window exists for returns, where the repository's active-return guard happens to turn the retry into a `duplicate`. That is a property of the return write and its unique index, not of the confirmation mechanism.

#### 8.8.3 Future durable design requirement (not designed or approved)
A production-grade design must make the idempotency record and the business write **recoverable as one logical operation**. One approach to evaluate, with the exact schema and repository contract left to a separately designed and approved change:

1. The action's identifier is stored as a unique idempotency key together with the business record, or in a table whose row is written in the same SQLite transaction as the business write.
2. Uniqueness is enforced by the database on that key.
3. In **one** SQLite transaction, the backend checks the key, performs the business write if the key is new, and records the action's outcome and result ID.
4. On a retry with the same key, the backend returns the original committed outcome and result ID and executes no second write.

This approach needs a new repository write contract or a new optional parameter, and a schema addition for the key. It therefore requires its own approved change to `docs/01` and `docs/02`. This checkpoint does not implement it and does not change `create_return_request` or `create_support_ticket` signatures. Even then, the customer still needs a way to learn the key's outcome (for example a pending-action status lookup, or the ticket-history tool in Section 5.5) to meet the recoverable-result property.

Open items 1 and 2 in Section 18 ask the Technical Lead to choose between the in-memory MVP and a durable design, and to decide the idempotency strategy.

---

## 9. Demo data and database initialization (D2)

### 9.1 Problem being solved
The seed is anchored at `2026-11-07T00:00:00Z`, while `create_return_request(reference_time=None)` uses the real clock. As real time passes, the seeded Delivered orders leave their return windows (ORD1001, delivered 2026-11-04, leaves the 7-day window at 2026-11-11T00:00:00Z). The demo needs seed dates relative to the real current time.

### 9.2 Initialization contract (proposed)
A bootstrap operation, run explicitly and never at application startup:

```text
initialize_demo_database(db_path, reference_time)       # reference_time is REQUIRED, no default
```

The existing seed function, verified from `app/db/seed_data.py`, has this exact signature:

```text
seed_database(
    connection: sqlite3.Connection,
    reference_time: Union[datetime, str, None] = None,
) -> None
```

Verified behavior that this design relies on:
- `reference_time=None` uses the fixed baseline `2026-11-07T00:00:00Z`. `datetime.now()` is never called inside the function.
- A `datetime` must be timezone-aware, and a `str` must be ISO-8601 with an explicit UTC offset. Timezone-naive values are rejected with `ValueError`.
- It requires a connection with foreign keys on (`ConnectionError_` otherwise) and not already inside a transaction (`RuntimeError`).
- It is deliberately not idempotent: it raises `RuntimeError` before writing anything if any of `customers`, `orders` or `order_items` already has rows. It does not look at `return_requests` or `support_tickets`.
- It runs its own atomic transaction.

Therefore:
- Initialization opens the connection with `get_connection(db_path)`, calls `create_schema` and then `seed_database(connection, reference_time=...)` with the supplied aware datetime. It does not change `seed_database`.
- `reference_time` is mandatory and has no default. It must be a timezone-aware UTC instant supplied by the caller. The command-line wrapper obtains "now" once from the real clock, prints it, and passes it in. Passing it explicitly keeps the behavior reproducible and auditable.
- `seed_database(None)` is untouched and keeps its fixed `2026-11-07T00:00:00Z` baseline. Existing tests keep using it.
- The seed's required scenario is preserved: C101 owns ORD1001 (Delivered, inside the 7-day window relative to `reference_time`) and ORD1002, C102 owns ORD1003. Section 9.2.1 records the verified result: every seeded order timestamp is relative to `reference_time`. This document does not change the seed.
- If the target database already contains rows, initialization refuses and reports why. `seed_database` already raises on non-empty tables, and this operation relies on that guard.

#### 9.2.1 Seed timing, verified from `app/db/seed_data.py` (read-only inspection)
Inspected: `_DEFAULT_REFERENCE_TIME_STR` (line 58), `_parse_reference_time` (line 99), `_fmt` (line 155), `_offset` (line 165), `_build_seed_data` (line 251, including the `customers` list at 268, the `orders` list at 296 with its comment block at lines 276–295, and `order_items` at 479), the three INSERT statements `_INSERT_CUSTOMER`, `_INSERT_ORDER` and `_INSERT_ORDER_ITEM` (lines 534–549), and `seed_database` (line 557 onward). The file was not modified.

Verified facts:
1. **Every `order_date` is relative.** All 15 orders set `order_date` with `_offset(ref, days=-N)`. The offsets are: ORD1001 −5d, ORD1002 −2d, ORD1014 −1d, ORD1003 −14d, ORD1004 −1d, ORD1015 −2d, ORD1005 −4d, ORD1006 −3d, ORD1007 −2d, ORD1008 −1d, ORD1009 −2d, ORD1010 −16d, ORD1011 −1d, ORD1012 −20d, ORD1013 −5d.
2. **Every non-null `delivered_at` is relative.** Six orders set it with `_offset(ref, hours=−H)`: ORD1001 −72h, ORD1003 −240h, ORD1005 −36h, ORD1010 −288h, ORD1012 −360h, ORD1013 −60h. The other nine orders have `delivered_at = None`.
3. **No other seeded timestamp exists.** The INSERT statements touch only `customers`, `orders` and `order_items`. `customers` and `order_items` have no timestamp columns, and `return_requests` and `support_tickets` are not seeded. Tickets and returns later written by the application take `created_at` from the database default or the write's `reference_time`, not from the seed.
4. **The clock is never read by the seed.** `datetime.now()` appears only in docstrings and comments. `reference_time=None` resolves to the constant `_DEFAULT_REFERENCE_TIME_STR = "2026-11-07T00:00:00Z"`, so the fixed `seed_database(None)` behavior is unchanged by this design. A supplied `reference_time` replaces that constant as `ref`, after normalization to UTC. Output strings are truncated to whole seconds by `_fmt`.
5. **Every return-window scenario is therefore relative to the supplied reference time T0.**

| Order | Customer | `delivered_at` | 48-hour window (`damaged`, `wrong_product`) | 7-day window (`standard`) |
|---|---|---|---|---|
| ORD1001 | C101 | T0 − 72h | already closed | open until T0 + 96h |
| ORD1005 | C103 | T0 − 36h | open until T0 + 12h | open until T0 + 132h |
| ORD1013 | C106 | T0 − 60h | already closed | open until T0 + 108h |
| ORD1003 | C102 | T0 − 240h | closed | closed |
| ORD1010 | C105 | T0 − 288h (status Returned) | closed | closed |
| ORD1012 | C106 | T0 − 360h | closed | closed |

Consequences for the demo:
- A database initialized at T0 keeps its main `standard` scenarios for about 4 to 5.5 days. The 48-hour `damaged` scenario (ORD1005) lasts only **12 hours** after T0. A health check that watches only ORD1001 would still report fresh after that scenario has lapsed. The diagnostic in Section 9.4 therefore reports the two scenario families separately.
- The required seed scenario (C101 owns ORD1001 Delivered inside the 7-day window, and ORD1002; C102 owns ORD1003) holds for any timezone-aware `reference_time`, because the offsets do not depend on its value. The customer and item assignments are fixed and do not depend on it.
- This verification covers the current source. A later change to `seed_data.py` could break these properties, so the implementation checkpoint adds a test that seeds with an arbitrary reference time and asserts these offsets (Section 16).

### 9.3 Reset (separate, explicit, manual)
Reset is a different command from initialization, and it is never run automatically by the backend, by startup, or by a health check.

- It requires an explicit flag and an interactive typed confirmation that echoes the exact database path.
- It operates only on the configured demo database path.
- It first makes a timestamped backup copy unless a flag disables that.
- It then recreates the file and calls initialization with a freshly supplied reference time.
- It never touches any other path.

### 9.4 Startup health check: read-only, no side effects
`check_backend()` is read-only. It never creates, seeds, resets, repairs or otherwise changes a database file, a directory, or anything else on the filesystem.

#### 9.4.1 Why the ordinary connection factory is not acceptable
`get_connection()` can create the database's parent directory (`os.makedirs`) and SQLite creates an empty file when none exists (`app/db/connection.py`). A missing-file check before it avoids that. But opening an **existing** database through the read-write factory still does not prove that the health check has no filesystem side effects. It opens the file read-write, it sets a connection pragma, and a read-write connection that meets a leftover journal can try to recover it. The health check therefore never uses `get_connection()`, in any branch.

#### 9.4.2 Order of operations
1. **Missing-file check first.** Test whether the configured database path exists as a regular file, using only a filesystem stat (for example `os.path.isfile`). If it does not, return `not_initialized` immediately. No connection of any kind is opened, and no file or directory is created.
2. **Pre-open file safety checks (no SQLite).** Before SQLite opens anything, apply the journal-mode precondition in 9.4.3: read the first 100 bytes of the file in binary mode to check the SQLite header and the journal-mode version bytes, and stat the `-wal`, `-shm` and `-journal` sidecar paths. If the file is not a rollback-journal database, or any sidecar exists, return `unavailable` without opening it. These are plain read-only filesystem operations.
3. **Dedicated read-only connection.** Only if the pre-open checks pass, open it with the read-only connection described in 9.4.3. It is not the ordinary factory.
4. If the required tables are missing, return `not_initialized`.
5. If the tables exist, run the demo-readiness diagnostic (9.4.4).
6. Any unexpected error, including failure to open the file read-only, returns `unavailable` and is logged. Nothing is repaired, deleted, recovered or recreated.

The stat and the open are not one atomic step. If the file is removed between them, the read-only open fails and the result is `unavailable` or `not_initialized`. It never creates a file. If a file appears between them, the check only reads it. Likewise a file could be converted to WAL mode by another process after the pre-open checks. The project never does this (9.4.3), so this window is a documented residual risk and not a supported scenario.

#### 9.4.3 Read-only connection design (future connection-layer change, not implemented)
The architecture needs one additional function in the existing connection module, `app/db/connection.py`, named here `get_readonly_connection(db_path)`. It is a separate, approved, later change. It is not made by this checkpoint, and it changes neither `get_connection()` nor its callers. Requirements:

- It opens the database in **SQLite read-only URI mode**: `sqlite3.connect(<file URI>, uri=True)` where the URI is the resolved path followed by `?mode=ro`. For example, a `pathlib` file URI with the `mode=ro` query. The path must be properly percent-encoded for a URI, and the implementation must be tested on Windows, which is the project's development platform.
- `mode=ro` makes SQLite refuse to create the file when it does not exist, and refuse every write.
- **Connection-level query-only protection:** immediately after opening, run `PRAGMA query_only = ON` and read it back to confirm it is `1`. If it is not, close the connection and raise `ConnectionError_`, like the existing factory does for its own pragma. This is defense in depth on top of `mode=ro`.
- It never calls `os.makedirs` or creates any directory. It never runs `PRAGMA journal_mode = ...` or any other pragma that changes the file or its sidecars.
- It does not use `immutable=1`, and `immutable=1` is not accepted as a workaround for any problem below. That mode skips locking and can return inconsistent results if another process writes to the file while the health check reads.
- `mode=ro` is the actual read-only open mode. `PRAGMA query_only` is defense in depth. Neither is assumed to prevent sidecar creation on its own, as the next requirement explains.

**Journal-mode and sidecar precondition.**
- **Requirement.** The health check must not create, delete or modify any `-wal`, `-shm` or rollback-journal file, in any journal mode. It must also not rely on `mode=ro` alone to guarantee this. SQLite's read-only URI mode does **not** unconditionally prevent sidecar creation for a WAL-mode database: a reader of a WAL database may need a `-shm` wal-index, and a read-only open can still try to create one.
- **Project expectation.** `get_connection()` sets no `journal_mode`, so the project's database uses SQLite's default rollback-journal mode, not WAL. It is expected to stay that way unless a separate approved decision changes it. A reader of a rollback-journal database that only reads creates no journal file.
- **How the health check enforces it.** Before SQLite opens the file (Section 9.4.2, step 2), the check reads the first 100 bytes of the file with an ordinary binary read and verifies the SQLite header string and the file-format version bytes at offsets 18 and 19 (both `1` for a rollback-journal database, `2` for WAL). It also stats the `-wal`, `-shm` and `-journal` paths next to the file.
  - Header unreadable, not a SQLite header, or version bytes not both `1` (for example a WAL-mode database): return `unavailable`. The check opens nothing, so it cannot create a sidecar.
  - A `-wal`, `-shm` or `-journal` file exists: return `unavailable`. The state may be mid-write or hot, and the check does not open, recover, checkpoint, delete or alter it.
  - Otherwise open with `mode=ro` and `query_only` as above.
- **Safe rejection.** `unavailable` is the correct result when safe read-only inspection cannot be guaranteed. The operator is told to run the application or the reset command from a clean state. The check never converts the journal mode, never checkpoints, and never removes a sidecar.
- A leftover hot journal from a crashed writer is covered by the same rule, because the `-journal` file is detected before any open.
- It is used only for health checks and other explicitly read-only operator diagnostics. Customer-facing repository functions keep using `get_connection()`.
- It lives in the connection module, so the existing rule that `sqlite3.connect()` is called only in that module still holds. `AGENTS.md` and `docs/02` currently say that every connection goes through `get_connection()`. The wording needs a separately approved amendment to allow this one additional factory (Section 18, item 4).

#### 9.4.4 Demo-readiness diagnostic and customer identity
`check_backend()` takes no customer or session argument, so it has no authenticated customer. It therefore must not call customer-scoped repository functions such as `get_order`. Faking a `CustomerContext` for `C101` to satisfy their authorization would be an identity bypass, and it is forbidden. The design separates the two concerns:

- A **backend or operator health diagnostic** is not a customer-facing order lookup. It is a separate internal operation with no connection to the tool layer.
- It may inspect only **fixed synthetic demo-readiness markers**. It runs one narrowly scoped read-only query for the delivery timestamps of the fixed demo order IDs (`ORD1001` for the 7-day scenario and `ORD1005` for the 48-hour scenario, Section 9.2.1), using the read-only connection from 9.4.3.
- It **accepts no customer ID**, no order ID and no other input from the frontend, the LLM or the customer. The marker IDs are constants in code.
- It returns only a small status structure, for example whether each scenario's window is still open relative to the injected clock. It never returns rows, customer data, names, or timestamps to the frontend.
- It is not registered as a tool, is not importable by `app.tools` or the orchestrator, and its result is never shown to a customer as order information. The frontend sees only the `BackendHealth` status and an operator-facing message.
- It does not construct a `CustomerContext` and does not call, wrap or relax the customer-scoped repository functions or their authorization (`docs/01` Section E).

This is one repository-layer read that is not scoped to an authenticated customer, like the login directory in Section 18, item 15. It requires an explicit decision and amendments before implementation. The alternative is to drop the `stale_demo_data` status, so `check_backend()` reports only `ready`, `not_initialized` and `unavailable`, and demo freshness is handled by the operator running the reset command before a demo. Section 18, item 5 asks the Technical Lead to choose.

#### 9.4.5 Statuses
- `not_initialized`: the file does not exist, or the required tables are missing. The backend tells the operator which command to run. It does not create or seed anything.
- `stale_demo_data` (only if the diagnostic in 9.4.4 is approved): the database is initialized, but one of the two fixed demo scenarios has lapsed relative to the current time. The status names which one, for the operator. It is advisory only. The app keeps working. The operator is told to run the reset command before a demo.
- `ready`.
- `unavailable`: the check itself could not complete, for example because the read-only open failed.

The existing `main.py` calls `get_connection()` and can create an empty database file (Section 17, item 4). The health check must not follow that pattern.

### 9.5 Clock injection
The tool layer receives a `Clock` (a callable returning an aware UTC datetime). In production it is the real clock. Tests inject a fixed clock. The tool layer passes `reference_time=clock()` explicitly to the repository, once per operation. This does not change the repository's `reference_time=None` behavior, which stays real-clock by default as approved. The session registry and pending-action store (Sections 8 and 11) use the same clock for expiry.

---

## 10. Unsupported operations (D3)

### 10.1 Rule
The assistant never says or implies that an unsupported action was done, started or scheduled. It states plainly what policy says, states what it cannot do itself, and offers a support ticket where a human can help. It does not offer a ticket when the answer is simply "not possible", for example a cancellation after shipment, where the policy says to return the item after delivery if eligible.

### 10.2 Ticket issue types **[Proposed]**
`order_status`, `delivery_delay`, `payment_issue`, `refund_issue`, `return_issue`, `cancellation_request`, `address_change`, `warranty_claim`, `replacement_request`, `courier_pickup`, `product_question`, `other`. This is the optional enum restriction in Section 5.3.2. Without approval, `issue_type` stays validated free text of 1–100 characters (Section 5.3.1). The repository stores `issue_type` as free text and is unchanged either way.

### 10.3 What the assistant may and may not say

| Request | Source of truth | The assistant may say | The assistant must not say | Ticket? |
|---|---|---|---|---|
| Cancel an order | `Policies.md` §3, `FAQ.md` Q5.1 | Cancellation is allowed until shipment; after shipment it is not possible and an eligible item can be returned after delivery. States the order's actual status. | "I've cancelled your order", or that cancellation is in progress | Yes while the order is before shipment (`Processing` or `Packed`); no once shipped or delivered |
| Change a shipping address | `FAQ.md` Q1.3 | Address changes are possible only before shipment, through support or the account dashboard. | "I've updated your address" | Yes while `Processing`; no once shipped |
| Warranty claim | `Policies.md` §4, `FAQ.md` Q6.1 | Most electronics have a 6–24 month manufacturer warranty; claims may need the invoice and serial or IMEI. | That a claim is filed, approved or covered, or the warranty period for a specific product | Yes (`warranty_claim`) |
| Replacement | `FAQ.md` Q3.2 | A replacement can be requested in the store's own flow; this assistant can only record a return request. | "I've arranged a replacement" | Yes (`replacement_request`) |
| Courier pickup | `FAQ.md` Q3.2 | The store's courier partner schedules pickups for eligible returns. | "A pickup is scheduled", a date, or a courier name | Yes (`courier_pickup`) |
| Refund processing | `Policies.md` §2, `FAQ.md` Q3.3 | Refunds are processed after quality check, typically 3–7 business days. States the order's `refund_status` and amount from the database. | "Your refund has been issued", or a date | Yes (`refund_issue`) when the customer disputes timing |
| Payment reversal | `Policies.md` §5, `FAQ.md` Q4.3 | Failed payments typically reverse within 3–7 business days; share the transaction reference with support if not credited. | That the money has been or will be returned | Yes (`payment_issue`) |
| Photo evidence upload | `FAQ.md` Q3.4 | Photos cannot be uploaded in this assistant; support may ask for them. | "Photos received" | Optional |
| Non-returnable item question | `Policies.md` §2 | Quotes the named non-returnable categories. | A returnability verdict for a specific product beyond what the prechecks report | Yes when unclear |

### 10.4 What a successful return request means
After `created`, the assistant says the request has been recorded with status Requested. It also says, once, that it cannot schedule a pickup or process a refund and that the request awaits review. It does not state a refund date, an approval, or a pickup.

### 10.5 Refund and payment facts come from the database
The assistant reports `payment_status`, `refund_status` and `refund_amount` exactly as `get_order_status` returns them, with amounts formatted by the backend (Section 12.3). It does not infer or predict them.

---

## 11. Sessions and conversation state

### 11.1 Server-side session record and the opaque state
The authoritative session lives on the server, in a process-global registry with the same locking and limitations as the pending-action store (Sections 8.7 and 8.8). `ConversationState` is only a handle that names a session record.

```text
SessionRecord(
    session_id: str,            # random, unguessable
    customer_id: str,           # set by start_session; never changed
    created_at: <UTC>,
    last_activity_at: <UTC>,    # used for inactivity expiry
    expires_at: <UTC>,          # last_activity_at + idle timeout
    status: "active" | "expired" | "ended",
    history: <LLM history, Section 13.4>,
    clarification_context: <bounded customer-only ambiguous messages, Section 13.4.1>,
    return_draft: <draft or None>,         # Section 7
    choice_prompt_id: <str or None>,       # open category prompt, Section 7
    pending_action_id: <str or None>,      # pointer only; authority is the store (Section 8)
    counters: <per-turn and per-session counters, and per-kind committed and reserved write counts, Sections 5.3.3 and 8.4>,
)
```

`created_at` is informational (for logs and an optional absolute lifetime). It is **not** used to decide inactivity expiry.

### 11.2 Inactivity expiry
Proposed durations are marked **[Proposed]**.

- **Initialized.** `start_session` sets `last_activity_at = now` and `expires_at = now + idle timeout`. The idle timeout is **[Proposed]** 60 minutes.
- **Refreshed.** Each call to `handle_message` that passes the session check (Section 4.3, step 2) sets `last_activity_at = now` and `expires_at = now + idle timeout`. This includes message, decision and choice calls, and messages that the scope guard answers with fixed text. It does not happen for calls that fail the session check, for malformed calls, or for `check_backend` or `list_login_options`.
- **Checked.** At the start of each `handle_message` call, before any other work: if `now >= expires_at`, or the status is not `active`, the session is invalid. The check reads the server-side record, using the injected clock. It never trusts a timestamp supplied by the frontend, so a copied or edited state cannot extend a session.
- **Invalidated.** When the check finds an expired session, under the same lock it sets `status = "expired"`, marks every pending action of the session `expired` (Section 8.2), clears the draft, the open prompt and the clarification context, and returns `invalid_session`. The frontend then calls `start_session`. An expired session is never revived. Ending a session (switching customers, Section 3.2 rule 5) sets `status = "ended"` and invalidates its pending actions in the same way. Only **pending, unclaimed** actions are invalidated. A `consumed` action with `reservation = "held"` is an in-flight write: it is left alone and may finish, and its finalize step (Section 8.4, step 5) can still record the outcome and commit or release its reservation. The handle is invalid from this moment, so the record accepts no new customer request, and no lock is held across the in-flight repository call.
- **Pending actions have their own expiry.** A pending action expires at its own fixed `expires_at` (Section 8.2), which is not extended by activity. Session activity never keeps an old pending action alive.
- **Optional absolute lifetime** **[Proposed]**: a maximum session age of 8 hours measured from `created_at`, so that continuous activity cannot keep a session alive forever. This is the only use of `created_at` in expiry, and it is listed separately for approval (Section 18, item 19).
- **Cleanup.** Expired and ended records are purged lazily on access and periodically, so the registry cannot grow without limit. A non-active session is **retained** while any of its actions is `consumed` with `reservation = "held"`, because its reserved count and the action must remain available to the finalize step. After every outstanding reservation is finalized (committed or released), the session record and its terminal actions become eligible for normal cleanup. A retained non-active record is never reactivated and never accepts a new request.

### 11.3 What the record holds
- State is not persisted beyond the process (Section 8.7). A new session starts empty.
- Raw tool results are not stored in history verbatim, only the assistant's customer-facing text.
- The record never contains secrets, SQL, connections, or other customers' data.
- `history` holds the last N turns sent to the LLM, each capped at 2,000 characters. **[Proposed]** N = 10. Section 13.4 defines which turns enter it.

---

## 12. Product lookup, money and presentation

### 12.1 Product catalog (`app/catalog`, proposed)
- Loads `data/source/Products.json` once, validates it (unique `id`, required fields, positive integer `price_inr`), and exposes it read-only. It never edits the file.
- `get_product(product_id) -> ProductView | None`.
- `search_products(query, limit) -> list[ProductView]`: deterministic matching over name, category and description tokens. No LLM, no embeddings.
- `ProductView`: `product_id`, `name`, `category`, `price_inr`, `description`, `specs`, `stock_status`.
- A product ID that is missing from the catalog is shown as "an item no longer in the catalog" and is logged. It cannot occur with the validated seed.
- The assistant answers spec questions only from the returned `specs`. If an attribute is not listed, it says it is not listed. It does not guess.
- The catalog is not a database table and not a foreign key (`docs/01` Section C).
- Product questions need no login state and no purchase: product discovery and pre-purchase questions are in scope (Section 13.1).

### 12.2 Order and product joins
`get_order_status` enriches each order item with the product name from the catalog. Item `price` is the stored line price in paise and may differ from the catalog's current price. The assistant labels the order price as the price on the order.

### 12.3 Money and dates
- The database stores money as integer paise. `Products.json` stores whole rupees in `price_inr`. The tool layer converts rupees to paise (×100) before comparing and formats all amounts for display. The LLM never does currency arithmetic.
- Display format **[Proposed]**: `₹2,499` for whole rupees and `₹2,499.50` otherwise, with comma grouping.
- Stored timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ`. Return window deadlines are shown in UTC with the zone stated, because the windows are defined to the second. Other dates show as a date. Whether to also show IST is an open item (Section 18, item 22).

---

## 13. Conversation Scope Guard and cost controls (proposal)

**Status: proposal for Technical Lead approval. It is not an implementation authorization.** No provider, model, classifier or dependency is selected here.

### 13.1 Purpose
Prevent the main LLM from being used for unrelated general-purpose conversations. This reduces unnecessary token consumption and keeps the product focused on e-commerce customer support. The guard is a **cost and focus control**, not a security control. Authorization, identity and write safety never depend on it (Sections 3, 5 and 8).

### 13.2 What is in scope and out of scope
**Supported conversations.** The assistant handles:
- Order status, tracking and delivery.
- Purchases and payment issues.
- Refunds and failed payment reversals.
- Returns, damaged items and wrong products.
- Product discovery, product details and specifications, including pre-purchase questions.
- Shipping, return, cancellation and warranty policies.
- Complaints, customer-support questions and ticket escalation.
- Unsupported e-commerce actions, where the assistant explains its limitations honestly (Section 10).

The assistant is not restricted to customers who have already bought something. A logged-in customer with no orders, and any customer asking a pre-purchase product question, is in scope.

**Out of scope.** Examples:
- General coding help.
- General knowledge questions unrelated to shopping.
- Requests for jokes, stories or entertainment unrelated to support.
- General-purpose writing or brainstorming unrelated to the store.

The assistant politely redirects the customer to supported topics.

### 13.3 Proposed processing flow
The guard runs after the checks in Section 4.3, steps 5.1 to 5.4, and before the orchestrator. It is deterministic and cheap, performs no I/O, and makes **no additional LLM call** for any message.

1. **Validate** the message and apply the message-length limit (Section 4.3).
2. **Greetings and simple acknowledgements.** A message whose whole normalized text is on a fixed list of greetings ("hi", "hello", "good morning"), thanks ("thanks", "thank you"), acknowledgements ("ok", "okay"), or farewells ("bye") gets a fixed response. No main LLM call, no tool, no database access.
3. **Deterministic checks.** The message is normalized (Unicode NFC, case folded, punctuation and extra whitespace removed). The guard then looks for two kinds of signals using fixed data reviewed in code:
   - **Support signals:** support vocabulary (order, delivery, track, shipping, payment, paid, refund, return, replace, damaged, wrong, cancel, warranty, product, price, specs, stock, ticket, complaint, support, and similar), ID patterns (`ORD`, `RET-`, `TKT-` and product or item IDs), product names and categories loaded from the catalog, and currency amounts.
   - **Unrelated signals:** requests that name a general task with no store connection, for example code fences or programming-task phrases, jokes, stories, poems, essays, trivia or general-knowledge questions, and homework-style math.
4. **Decision table:**

| Support signal | Unrelated signal | Class | Action |
|---|---|---|---|
| yes | no | `in_scope` | Continue to the orchestrator (step 6) |
| yes | yes | `mixed` | Continue to the orchestrator with the **whole message** (see below) |
| no | yes | `out_of_scope` | Fixed redirection reply (step 5) |
| no | no | `ambiguous` | Step 7 |

5. **Clearly out-of-scope messages** get a fixed scope-redirection reply, for example: "I can help with orders, deliveries, payments, refunds, returns, products and store policies, and I can raise a support ticket. What would you like help with?" They never reach the main LLM, invoke tools, query the database, or trigger writes. They count toward the per-session message cap, so they cannot be used for unlimited traffic.
6. **Clearly in-scope messages** continue to the orchestrator.
7. **Ambiguous messages** are never rejected automatically. **Support context** exists when the session's LLM history (Section 13.4) is not empty, that is, when earlier turns were handled by the orchestrator as support turns. The documented rule is:
   - **With support context:** the message continues to the orchestrator and is added to LLM history as part of the support conversation. Short follow-ups such as "what about the second one?" or "why?" are ambiguous in isolation but belong to the conversation. This rule is fail-open.
   - **Without support context** (for example the first message, or after a redirection): the customer gets a fixed clarification prompt, without a main LLM call: "I'm not sure what you need help with. Could you tell me more, for example about an order, a delivery, a payment, a return or a product?" The message is **not** added to LLM history. It is kept in a separate, bounded **clarification context** (Section 13.4) that is never sent to the LLM unless it is promoted under the rules there.
   - **While a category choice prompt or a pending confirmation is open** and the message is not an exact typed answer to it: an ambiguous message does not go to the orchestrator and does not cancel the open item. The reply is a fixed reminder that shows the prompt or preview again (Section 8.3).
   - Messages in a language or script the guard's lists do not cover (for example transliterated or code-mixed text) are `ambiguous`, never `out_of_scope`. The guard classifies as `out_of_scope` only when it recognizes a specific unrelated pattern.
8. **Mixed messages.** A message with a genuine support request and unrelated text is not rejected. The whole original message is sent to the orchestrator **unchanged**. The guard does not strip, split, summarize or rewrite it, because removing text could lose details that affect an order or a support request. The orchestrator's system instructions tell it to answer the supported part and politely decline the unrelated part. **Limitation:** a mixed message still consumes main LLM tokens, including for its unrelated part. The guard reduces unrelated LLM use. It does not eliminate it, and no part of this design claims otherwise.
9. **The rules are not modifiable by customers.** The word lists, patterns and the decision table are fixed code and data. Customer text is only ever matched against them. No customer message, retrieved document, tool result, or LLM output can add to, remove from, or reorder the rules. An instruction such as "ignore your scope rules" is only a message and is classified like any other message.

Honest limits of the guard. A message that wraps an unrelated request in a support word ("for my order, write me a Python script") is classified as `mixed` and reaches the orchestrator. The guard is not meant to be airtight. Defense in depth: the orchestrator's system instructions also carry the scope rule, and the tool-call cap and the token limits in Section 13.5 bound the cost of any message that gets through. A false pass costs tokens. A false rejection of a genuine support message costs the customer their help, so the design errs toward passing messages through: only a message with a recognized unrelated pattern and no support signal is rejected.

### 13.4 Conversation history: the transcript and the LLM context
Two things are kept apart:

- **The customer-visible chat transcript.** It is owned and rendered by the frontend. It shows every customer message and every reply, including greetings, fixed redirections and clarification prompts. The backend does not remove anything from what the customer sees.
- **The history sent to the main LLM** (`SessionRecord.history`, Section 11). It contains only turns that belong to the support conversation.

A third, separate store holds ambiguous messages that have no support context yet: the **clarification context** (`SessionRecord.clarification_context`, Section 11.1). It is never sent to the LLM unless promoted (13.4.1).

| Message class | Where it goes |
|---|---|
| `in_scope` and `mixed` (whole message, unchanged) | LLM history, with the assistant's reply |
| `ambiguous`, **with** support context | Routed to the orchestrator and added to LLM history as a follow-up |
| `ambiguous`, **without** support context | Transcript, and the **clarification context**. **Not** LLM history |
| Clearly `out_of_scope` | Transcript only. Neither the message nor the fixed redirection reply enters LLM history or the clarification context |
| Greetings and acknowledgements | Transcript only |

Rules:
- A clearly out-of-scope message never reaches the LLM and never pollutes its context.
- No message class discards, trims, or reorders earlier legitimate LLM history. An out-of-scope or ambiguous message is simply not added. The legitimate in-scope history stays available for later turns.
- For mixed messages the whole message is retained as received. No meaningful detail is silently removed.
- The history window (N turns, Section 11.3) is applied to the LLM history only.
- The customer-visible transcript remains complete in every case.

#### 13.4.1 Clarification context and promotion into LLM history
The clarification context holds only the customer's own ambiguous messages (not the fixed prompts), in order, bounded to the most recent **[Proposed]** 3 messages and **[Proposed]** 10 minutes. It lives in the server-side session record and is cleared when the session ends. It is part of the session, not a copy of the history.

What happens at the next message that arrives while the clarification context is not empty:

| The next message is | Effect on the clarification context | Effect on LLM history |
|---|---|---|
| `in_scope` or `mixed` (the follow-up establishes a genuine support request) | **Promoted:** the buffered customer messages, in order, are added to LLM history immediately before the follow-up, and the buffer is cleared | Buffered messages (customer text only), then the follow-up, then the reply |
| Clearly `out_of_scope` (the follow-up establishes an unrelated request) | **Discarded.** It was never sent to the LLM | Nothing is added. Earlier legitimate history is unchanged |
| `ambiguous` again | Appended, up to the bound, oldest dropped. The fixed clarification prompt is repeated | Nothing is added |
| A greeting or acknowledgement | Unchanged | Nothing is added |

Rules for promotion:
1. Promotion happens only when a later message in the same session is itself classified `in_scope` or `mixed` by the deterministic checks. Nothing is promoted on the strength of the ambiguous message itself, on the LLM's opinion, or on the customer's claim.
2. Only the customer's own buffered text is promoted, never fixed replies, and it is promoted unchanged and in order. Nothing is rewritten or summarized.
3. A buffered message is never promoted after an `out_of_scope` follow-up, after the bound or time limit has expired, or after the session ends. The customer's earlier text stays in the visible transcript, so no details are lost to the customer.
4. Promotion is bounded by the same history window and per-message length limit as other history entries.
5. Promotion can carry an unrelated message into LLM history only in this narrow case: the customer sent an unrecognized message and then a clearly supported one. That costs at most 3 short messages. It is accepted so that genuine details given before the support request is clear, which the guard could not recognize, are not lost. The alternative of never promoting is listed in Section 18, item 6.

An ambiguous message that arrives **with** support context (13.3 step 7) does not use the clarification context. It goes to the orchestrator directly, and a later out-of-scope message never removes it or any earlier support history.

### 13.5 Token and cost controls
Scope control is one part of a broader cost strategy. The following controls are separate and are not interchangeable. Every number is **[Proposed]** and open for approval. No model, provider, tokenizer, price or percentage saving is claimed or measured here.

| Control | What it bounds | Value |
|---|---|---|
| Maximum incoming message length | One customer message | **[Proposed]** 2,000 characters (Section 5.3.2) |
| Maximum conversation-history size | Turns of past conversation sent to the LLM | **[Proposed]** 10 turns, each at most 2,000 characters (Section 11.3) |
| Maximum model input context / token budget | Total tokens sent in one LLM request (system text, history, tool schemas, tool results, message) | **[Proposed]** a cap defined as a token count once a provider and tokenizer are chosen. No number is proposed now |
| Maximum model output tokens | Tokens the model may generate per reply | **[Proposed]** a cap, again to be set when a provider is chosen. No number is proposed now |
| Maximum tool calls per turn | Tool calls in one customer turn | **[Proposed]** 6 (Section 5.3.3) |
| Avoiding unnecessary main LLM calls | Messages that never need the main LLM: greetings, acknowledgements, clearly out-of-scope messages and ambiguous messages without support context | By the guard in Section 13.3. Effect to be measured later; no saving is claimed here |

A per-session message cap (**[Proposed]** 60, Section 5.3.3) also bounds total use.

### 13.6 Optional lightweight classifier (later, not now)
If measurement on a labeled evaluation set (Section 13.7) shows that deterministic checks produce too many false rejections or too many unnecessary LLM calls, a lightweight classifier may be considered. Conditions: it would run only on messages the deterministic checks leave `ambiguous` (never on every message), fail open when uncertain, and be approved separately with its own provider, model, cost and latency analysis. This checkpoint selects none of these. The default is no classifier (Section 18, item 18).

### 13.7 Testing and evaluation (future checkpoints)
Tests, all without an LLM:
- Clearly supported messages are `in_scope`, including pre-purchase product questions from a customer with no orders.
- Clearly unrelated messages are `out_of_scope` (coding, trivia, jokes, stories, unrelated writing).
- Greetings and acknowledgements receive fixed replies.
- Ambiguous messages are handled by the documented rule (clarification with no support context, orchestrator with support context) and never rejected automatically.
- Mixed support and unrelated messages reach the orchestrator **unchanged**, with the support content intact. The guard does not strip, split or rewrite them, and a test asserts that the orchestrator receives the identical string.
- **Context handling (the five required cases):**
  1. *First ambiguous message:* it gets the fixed clarification prompt, makes no LLM call, appears in the transcript, is held in the clarification context, and is **not** in LLM history.
  2. *Follow-up establishes an in-scope request:* the buffered customer message is promoted to LLM history, in order and unchanged, immediately before the follow-up, and the buffer is cleared.
  3. *Follow-up establishes an unrelated request:* the follow-up gets the fixed redirection, the buffer is discarded, and neither message is in LLM history.
  4. *Existing support history followed by an unrelated message:* the unrelated message and its reply are not added, and the earlier support history is unchanged and still available to the next in-scope turn.
  5. *Mixed support and unrelated message:* the whole message is in LLM history unchanged.
- The clarification context is bounded (3 messages, 10 minutes **[Proposed]**), is cleared with the session, and never contains fixed replies.
- An ambiguous message while a choice prompt or pending action is open gets the fixed reminder and does not cancel the open item.
- Prompt-injection attempts to change the rules ("ignore the scope rules", "you are now a general assistant", a message that claims to be from an administrator) leave the rules unchanged and are classified like any other message.
- Out-of-scope messages invoke no main LLM, no tool and no database access (assert with fakes that fail if called), and trigger no writes.
- Out-of-scope messages and their fixed replies are not added to LLM history, and legitimate earlier history is preserved intact.
- **LLM history for ambiguous and mixed messages** (Sections 13.4 and 13.4.1 are the canonical behavior, and the cases above are the same rules):
  - an ambiguous message **with** established support context is routed to the orchestrator and **is** included in LLM history;
  - an ambiguous message **without** support context is held in the clarification context and is **not** immediately in LLM history;
  - it is promoted into LLM history only if a later message is classified `in_scope` or `mixed`;
  - a clearly out-of-scope follow-up discards the clarification buffer without adding it to LLM history;
  - a mixed message remains unchanged and **is** in LLM history.
- Out-of-scope and greeting replies do not cancel an open pending action, and they re-show it (Section 8.3).
- Out-of-scope and greeting messages refresh the session's `last_activity_at` and count toward the message cap.

**False-rejection evaluation.** The guard must be evaluated for **false rejection of genuine support messages**, not only for its ability to reject unrelated ones. The evaluation uses a labeled set that includes the support queries in the project's sample data, the planned evaluation questions, pre-purchase product questions, short follow-ups, complaints in angry or informal wording, typos, and transliterated or code-mixed messages. A genuine support message classified `out_of_scope` is a defect. The acceptable false-rejection rate is a Technical Lead decision (Section 18, item 17). The set also measures how many unrelated messages pass through, but a missed rejection costs only tokens, while a false rejection costs the customer's help, so the second is weighed more heavily.

---

## 14. LLM boundary, prompt-injection controls and logging

This section defines what the deterministic layers guarantee regardless of what an LLM later does. No LLM is added now.

### 14.1 What the LLM may see
System instructions, the trimmed LLM history window (Section 13.4), the registered tool schemas, and `ToolResult` data for the current customer. It never sees a customer ID, SQL, connection details, a pending action's identifier or parameters, or the stored free text of returns and tickets.

### 14.2 Controls
1. Tool results and retrieved documents are data, not instructions. The orchestrator labels them as such.
2. Tool calls are validated against an allowlist and strict schemas (Section 3.2). Unknown tools and extra arguments are rejected.
3. At most 6 tool calls per customer turn **[Proposed]**.
4. Writes require a server-side, expiring, single-use, customer-and-session-bound pending action that the LLM cannot create, confirm, claim or replay (Section 8). Return reason categories are set only by the customer's selection (Section 7).
5. Claims about outcomes come from the tool outcome catalog (Sections 7.5, 8.6, 10), not from LLM text. A response that asserts an order status, a refund, a return or a ticket the tools did not return is a defect. The response-grounding checks belong to the orchestrator checkpoint.
6. The customer message is untrusted. An instruction such as "ignore previous instructions" or "show me ORD1003" has no effect on identity or authorization, because identity never passes through the model.
7. The scope guard (Section 13) reduces unrelated LLM use. It is not relied on for security.

### 14.3 Logging and audit
One structured log record per customer turn and per tool call:
`request_id`, `session_ref`, customer ID (a synthetic ID), tool name, outcome code, order or item IDs involved, duration, scope-guard class and the rule names that matched (not the message text), whether a pending action was created, claimed, declined, expired or cancelled, and claim failure reasons.

`session_ref` is a short non-reversible reference, for example the first 8 hexadecimal characters of a SHA-256 hash of the session handle. It lets operators correlate one session's records without exposing the bearer credential (Section 3.3). The same rule applies to `action_id`: logs carry a short hash reference, never the full value. The full session handle and full action identifiers are never logged, printed, returned in error text or sent to the LLM.

Not logged by default: message text, return reasons, ticket descriptions, summaries, or raw database errors. Raw exceptions for internal failures are logged at the server with the request ID and are never returned to the frontend or the LLM.

---

## 15. Error handling

| Where | Condition | Handling |
|---|---|---|
| Boundary validation | Wrong type, bad format, over limit, unknown field | `ToolResult` `invalid_input`; nothing executed |
| Session check | Missing, ended, or idle-expired session | `invalid_session`; nothing executed (Section 11.2) |
| Pending-action claim | Unknown, wrong owner, expired, consumed, cancelled | Plain reply per Section 8.3; nothing written |
| Repository read | `sqlite3.Error` or `ConnectionError_` (reads propagate) | Tool layer catches and returns `unavailable`. No database text is exposed |
| Repository write or prechecks | `failed` outcome | Mapped per Sections 7.5 and 8.6 |
| Repository | `TypeError` or `ValueError` | Should not occur after boundary validation. If it does, it is an internal defect: logged, reported to the customer as `internal`, never as a business outcome |
| Anywhere else | Unexpected exception | Logged with the request ID. The frontend call returns `status="error"`, `error_code="internal"`. If the failure happens after a successful claim, the action stays consumed (Section 8.5). Repository-level behavior (rollback then re-raise) is unchanged, and this top-level guard exists only at the backend boundary |
| Health check | Any unexpected failure | `unavailable`; nothing created or changed |

The reads' raising behavior is deliberately not changed here. The tool layer is where reads are converted into `unavailable`.

---

## 16. Testing strategy for later implementation checkpoints

All tests use temporary databases, never `data/app.db`, with the same path-guard fixture pattern as `tests/test_returns.py`. No test calls an LLM. The scope guard tests are in Section 13.7.

- **Prechecks parity:** for a matrix of inputs, `check_return_eligibility` returns `prechecks_passed` exactly when `create_return_request` on a fresh database returns `created`, and otherwise reports the same outcome, with matching boundaries. `reason_category` is echoed in every outcome, including `not_found` and `failed`.
- **Prechecks read-only:** the check never changes any table, including on failure paths.
- **Prechecks wording:** no customer-facing template for `prechecks_passed` contains "eligible", "approved", "guaranteed" or "definitely"; templates state that preliminary or time-window checks passed.
- **Delivery timestamp wording:** a NULL or malformed `delivered_at` never produces "not delivered" wording, and future-dated deliveries produce the dedicated message.
- **Category selection:** the LLM-visible tools accept no `reason_category`; a free-text description does not set it; a free-text reply while the prompt is open re-asks; the selected category appears in the confirmation preview; a changed category requires a new selection and a new confirmation.
- **Context and identity:** tool schemas contain no customer identifier fields. Extra arguments are rejected. A cross-customer order ID gives the same result as a nonexistent one.
- **Boundary limits:** length, character-set, enum and sanitization cases for every input in Section 5.3, with the approved limits tested at the boundary (reason 1,000, description 4,000, summary 2,000, issue type 100). A customer reason of exactly 1,000 characters is accepted with each of the three categories, a 1,001-character reason is rejected, the stored string is the prefix plus the customer text, and the longest stored `reason` is 1,016 characters. The prefix never comes from customer or LLM text.
- **Contact details:** no customer-facing template, tool note or fixed reply contains an email address, telephone number or URL, and no template contains `example.com`. Outcome templates that refer the customer elsewhere say "Please contact customer support."
- **Ticket history (`get_my_tickets`):** the tool takes no arguments and rejects any argument; it calls the repository only with the context customer; it returns only `ticket_id`, `issue_type`, `order_id`, `status`, `priority` and `created_at`, never `description`, `conversation_summary` or `customer_id`; a customer sees only their own tickets and cannot reach another customer's ticket by ID; a customer with no tickets gets an empty result, not an error; more than the return bound is truncated with the total count; `sqlite3.Error` and `ConnectionError_` become `unavailable`; a ticket created in the same session appears afterward with its stored status.
- **Session handle:** handles are random, distinct, at least 128 bits, and contain no customer ID; an unknown, expired or ended handle is rejected on every call with `invalid_session`; logout and expiry revoke it; logs and error text contain only `session_ref`, never the handle or a full action identifier.
- **Pending-action store:**
  - random, distinct identifiers;
  - claim succeeds once; a second claim of the same action fails;
  - many threads claiming one action at once produce exactly one success and exactly one repository write (assert the write count);
  - a copied state object cannot execute or revive an action;
  - wrong customer, wrong session, unknown, expired, cancelled and already-consumed actions never write, and the first three give the same reply;
  - action parameters come only from the stored record: a confirmation message or decision carrying different values changes nothing;
  - consumption happens before the write (fault injection: the write raises; the action is still consumed; a repeat decision does not write);
  - `failed` and unknown-outcome paths follow Section 8.5;
  - a simulated restart leaves no executable action;
  - the state transitions in Section 8.4.1 are atomic: concurrent staging leaves exactly one pending action; a decline racing a claim has exactly one winner; an expiry sweep racing a claim never lets an expired action run or expires a consumed one; ending a session makes every action of that session unclaimable; the outcome is recorded at most once;
  - the in-memory lock is never held across a repository call;
  - a test that crashes the write after the claim documents the MVP limitation: the action stays consumed, the outcome is unset, its write-capacity reservation is released, and no automatic retry happens. It does not assert idempotency, because the MVP has none.
- **Counters:** committed write counters increment only after `created`, not after `failed`, `duplicate`, `outside_window`, or an unconfirmed attempt; per-turn counters reset each turn; per-session counters do not.
- **Concurrent write-cap enforcement (write-capacity reservation).** A test with a controllable fake for the repository write (one that can be paused and then released or failed) and an injected cap:
  1. Bring the session to one slot below the cap (for example 2 committed tickets with a cap of 3).
  2. Start the first action's write and pause it after the claim has succeeded and before the repository call completes. Assert that `reserved` is 1 and `committed` is still 2.
  3. While the first write is paused, stage a second, distinct action and confirm it.
  4. Assert that the second claim fails with `rate_limited`, that the second action is `cancelled` and not `consumed`, that its repository write was never called, and that `committed + reserved` never exceeded 3 at any point.
  5. Release the first write with `created`. Assert `committed` is 3, `reserved` is 0, the first action's reservation is `committed`, and the second action still did not write.
  6. Repeat steps 1 to 4, but let the first write fail (`failed`, then separately `duplicate`, then separately an unexpected exception). Assert that `reserved` returns to 0, `committed` stays 2, the first action's reservation is `released`, and a **new** action can then reserve the slot and write.
  - **Session end or expiry during an in-flight write:**
    1. Start a write and pause it after a successful claim (reservation `held`).
    2. Expire the session (idle expiry with the injected clock) in one run, and end it by logout or customer switch in another.
    3. Assert that the session handle is rejected with `invalid_session`, that any other pending, unclaimed action of the session cannot be claimed (it is `expired` or `cancelled`), and that no new customer request is accepted.
    4. Assert that the consumed action (reservation still `held`) and the session's accounting record (the reserved count) remain available, and that a purge pass run now removes neither.
    5. Release the paused repository operation (once with `created`, once with a non-`created` outcome in a second run).
    6. Assert that the reservation is finalized exactly once: `created` commits and releases the reservation, any other outcome releases it without incrementing the committed count, and a second finalize changes nothing.
    7. Assert that a purge pass now removes the non-active session and the terminal action.
    - Assert throughout that no lock is held while the operation is paused.
  - Also assert with many threads that N concurrent confirmations of distinct actions never produce more than `cap - committed` repository calls, that the two kinds (returns and tickets) do not use each other's capacity, and that the finalize step runs exactly once per claimed action. The same test must show that no lock is held while the fake write is paused.
- **Sessions:** `last_activity_at` initialized at start and refreshed on each valid call with an injected clock; idle expiry by `expires_at`, not `created_at`; an expired session invalidates its pending actions; a frontend-edited timestamp has no effect; expired sessions are not refreshed by the failed call.
- **Health check (read-only):**
  - with the database file absent, `check_backend()` returns `not_initialized`, `get_connection` is never called (patched to fail), no connection of any kind is opened, and the file and its parent directory remain absent (the directory listing is identical before and after);
  - with an existing empty file it returns `not_initialized`, and the file's size, modification time and content hash are unchanged;
  - with an existing seeded database it returns a status without writing: the database file's content hash, size and modification time, and the directory's set of entries (no `-journal`, `-wal` or `-shm` file appears), are identical before and after, over repeated calls (access times are not compared, because they are not controlled by the application);
  - **WAL-mode database, sidecars absent:** create a temporary database, switch it to WAL mode, write a row, and close every connection cleanly so that `-wal` and `-shm` are absent while the file header still says WAL. Snapshot the directory entries and the database file's hash, size and modification time. Run the health check repeatedly. Assert that no `-wal`, `-shm` or `-journal` file was created, nothing was modified, and the result is `unavailable` (the design allows `unavailable` whenever safe read-only inspection cannot be guaranteed). `immutable=1` must not be used to make this test pass;
  - **sidecars present:** with a leftover `-journal`, `-wal` or `-shm` file next to an otherwise valid database, the check returns `unavailable`, and every sidecar's content hash, size and modification time are unchanged, and none is deleted;
  - **rollback-journal database, no sidecars:** the normal case still returns a real status, with no sidecar created;
  - the pre-open header check performs no SQLite call (assert with `sqlite3.connect` patched to fail for a WAL file);
  - the read-only connection refuses any write attempt (`INSERT`, `UPDATE`, `CREATE TABLE`, `PRAGMA journal_mode` change) with an error, and `PRAGMA query_only` reads back as 1;
  - a database path that does not exist cannot be created by the read-only connection, and no parent directory is created;
  - an unreadable or corrupt file yields `unavailable` and is left untouched;
  - `get_connection` is not called in any branch (patched to fail), and the check takes no customer, session or order argument;
  - the demo-readiness diagnostic (if approved) uses only fixed marker IDs, returns no rows or customer data, is not importable from the tool layer, and reports the 7-day and 48-hour scenarios separately.
- **Outcome catalogue:** every outcome in Sections 7.5, 8.6 and 10 maps to its template, and failure never produces success wording.
- **Ticket preview:** shows the issue type, description, supplied order reference, and the full summary that will be recorded, exactly matching the stored parameters.
- **Frontend independence:** an automated import check that `app.backend` public types and the frontend-facing module import no `sqlite3` or `app.db`.
- **Bootstrap:** initialization requires an explicit reference time, refuses a non-empty database, seeds windows relative to the supplied time, and never runs automatically. Existing `seed_database(None)` tests stay unchanged.
- **Seed timing:** seeding with an arbitrary timezone-aware reference time T0 produces every `order_date` and `delivered_at` at the offsets in Section 9.2.1 (for example ORD1001 delivered at T0 − 72h, ORD1005 at T0 − 36h), only those six orders have a `delivered_at`, and `seed_database(None)` still produces the fixed `2026-11-07T00:00:00Z` baseline values. The test reads the seeded rows and does not modify `seed_data.py`.
- **Stored-text leakage:** returns and ticket tools never return `reason`, `description` or `conversation_summary`.

---

## 17. Known issues carried from Checkpoint 10 (not addressed here)

Listed so they are not lost. None is fixed by this document.

1. `app/db/__init__.py` has `__all__ = [get_connection]`, which holds the function and not its name, so `from app.db import *` fails.
2. `app/` is not an installable package, and `pyproject.toml` builds the unused `src/ecommerce_support_assistant` stub. Imports work only from the repository root.
3. `get_connection()` defaults to the relative path `data/app.db`, creates the parent directory, and creates the file if missing. Repository functions use that default, so a configurable database path would need a separately approved repository change. This document assumes the working directory is the project root.
4. `main.py` prints a greeting and calls `get_connection()`, which can create an empty database.
5. `PROJECT_STATUS.md` is stale.
6. The local, gitignored `data/app.db` contains one stray test ticket from an earlier manual check.
7. No LLM provider, embedding model or vector store is chosen, and there is no configuration or `.env.example`. These belong to later checkpoints.
8. The FAQ mentions replacement and courier pickup, and the policies mention cancellation and warranty, but the system supports none of those actions (Section 10).

---

## 18. Open items for Technical Lead decision

Nothing in this list is approved. Each item is a proposal or a question. Items 1 to 11 respond to the Revision 2 review. Items 12 to 26 are the items carried from earlier revisions.

1. **In-memory MVP versus durable pending-action storage** (Sections 8.7 and 8.8). Proposed: a process-global in-memory store and session registry with an atomic claim, valid for the single-process demo only, with no cross-process or restart protection. Decide: accept that for the capstone demo, and state a single worker as a deployment constraint. A durable shared store needs an approved schema change in a later checkpoint.
2. **Durable idempotency and crash recovery strategy** (Section 8.8.3). Decide whether the capstone accepts the documented lost-reply and duplicate-ticket limitation, or whether a later checkpoint designs an idempotency key written in the same SQLite transaction as the business write. That needs a new repository contract and schema addition, and it is not designed here.
3. **Atomicity of all pending-action state transitions and the write-capacity reservation** (Sections 8.4 and 8.4.1): staging with cancellation of the previous action, claim with capacity reservation, finalize, decline, expiry, and session end, each under one shared lock in memory and as a conditional update or transaction in a durable design. The reservation, not the lock or the one-pending-action rule, enforces the per-session write caps, and the cap values remain proposed (item 19).
4. **Read-only SQLite health-check connection design** (Section 9.4.3): a separate `get_readonly_connection(db_path)` in the connection module using URI `mode=ro` and `PRAGMA query_only`, no directory creation, and a pre-open journal-mode and sidecar check that rejects WAL-mode databases and any existing `-wal`, `-shm` or `-journal` file as `unavailable` (no `immutable=1`). Decide whether to approve it, and approve the matching wording change to `AGENTS.md` and `docs/02`, which now say that every connection goes through `get_connection()`.
5. **Internal demo-readiness diagnostic authorization** (Section 9.4.4). Proposed: a narrow internal read of the fixed demo markers ORD1001 and ORD1005, with no customer ID input, no fake customer context, and no exposure to customers. Alternative: drop the `stale_demo_data` status and rely on the reset command.
6. **Ambiguous-message context promotion into LLM history** (Section 13.4.1): the separate clarification context, the 3-message and 10-minute bounds, and promotion only after a later in-scope or mixed message. Alternative: never promote, and send only the later message.
7. **`get_my_tickets` tool approval** (Section 5.5), including the 10-ticket return bound.
8. **Session bearer-handle protections** (Section 3.3): the proposed generation, storage, logging and revocation requirements. The handle is a bearer credential, and the simulated login remains a labeled limitation.
9. **Return-reason metadata prefix** (Section 7.4). Proposed: backend-generated prefix `[damaged] `, `[wrong_product] ` or `[standard] ` in the existing `reason` column. It does not consume the customer's approved 1,000-character allowance, and the longest stored string is 1,016 characters. No schema or repository contract change.
10. **Existing approved category enum versus the proposed customer-selection interface** (Sections 5.3.1 and 7). The values `damaged`, `wrong_product` and `standard` and their windows are approved in the Checkpoint 7A contract. What is proposed is only that the customer selects the category explicitly through a choice prompt. Also decide whether a deterministic keyword suggestion may pre-highlight an option while still requiring the customer's selection.
11. **Seed timestamp verification result** (Section 9.2.1). Verified from the source: every `order_date` and every non-null `delivered_at` is relative to the supplied reference time, no other timestamp is seeded, and `seed_database(None)` keeps its fixed baseline. Decide whether the 12-hour lifetime of the 48-hour `damaged` demo scenario after initialization is acceptable for demo planning.
12. **Return prechecks outcome naming and meaning** (Section 6). Proposed: `prechecks_passed`, meaning only the four facts in Section 6.3, with `window_closes_at` and `window_reason` in the result.
13. **Function name.** Keep `check_return_eligibility` (follows D1) or rename it `check_return_prechecks` to avoid overstating.
14. **Prechecks failure convention.** The check returns the generic `failed` result like the write (Section 6.2), rather than raising like the other reads.
15. **Login directory read.** The frontend needs a list of selectable customers, but the repository has no customer read. Proposed: a minimal read-only `list_customers_for_login()` returning `customer_id` and `display_name`, used only by `list_login_options`, never exposed as a tool, and labeled as simulated-login support. It is a repository read that is not scoped to an authenticated customer, so it needs explicit approval and an amendment to `docs/01`.
16. **Conversation Scope Guard behavior and classification strategy** (Section 13): the decision table, fail-open handling of ambiguous messages, unchanged routing of mixed messages, and the fixed replies. The stated limit that mixed messages still consume main LLM tokens is part of the proposal.
17. **Scope-guard evaluation target.** The acceptable false-rejection rate on genuine support messages (Section 13.7), and who owns the labeled set.
18. **Whether an additional lightweight classifier is justified later** (Section 13.6). Proposed default: no classifier. Revisit only with evaluation evidence, and select no provider or model until then.
19. **Numeric message, history, token, tool-call and session limits** (Sections 5.3.2, 5.3.3, 11 and 13.5): the 2,000-character message limit, 10-turn history, the 6-calls-per-turn, 3-tickets, 5-returns and 60-messages caps, the 10-minute pending-action expiry, the 60-minute idle timeout, the optional 8-hour absolute session lifetime, the 3-message and 10-minute clarification bounds, and the input and output token budgets, which have no proposed numbers until a provider is chosen. The four approved D4 limits (Section 1.1) are not part of this item.
20. **Issue-type enum restriction** (Sections 5.3.2 and 10.2). Without approval, `issue_type` stays validated free text of 1–100 characters.
21. **Ticket confirmation requirement and typed confirmation behavior.** Proposed: all writes, including tickets, require confirmation (Section 8). Typed replies use strict whole-message matching against a small fixed word list. Messages that reach the orchestrator cancel the pending action, while greetings, redirections and clarification prompts do not (Section 8.3).
22. **UTC versus IST in customer-facing deadlines.** Proposed: UTC with the zone stated. The alternative is to show IST as well.
23. **Real support contact channel.** The source documents contain only a placeholder address, which this design never presents to customers (Section 4.5). Decide whether and when a real channel is supplied as project configuration. Until then, customers see "Please contact customer support."
24. **`docs/01` and `docs/02` amendments** for the prechecks function (Section 6.5), for item 15, and for any of items 2, 4 and 5 that are approved, to be approved and committed separately.
25. **Module names** `app/backend` and `app/catalog` (Section 2.2).
26. **Next checkpoints (proposed, not authorized).** 11B: the prechecks read, the matching `docs/01` and `docs/02` amendments, and tests. 11C: session registry, pending-action store, context, validation, limits, catalog, tools, return-category selection and the confirmation state machine, with tests and no LLM. 11D: the bootstrap, reset, read-only connection and health-check commands. 11E: the scope guard with its labeled evaluation set. Later: RAG, the LLM adapter and orchestrator, and the frontend integration.
