# Browser-Session Chat List Implementation

## Scope

Add a lightweight list of chats in the Streamlit sidebar, scoped to the current
simulated customer and current browser session. Keep the existing login
dropdown unchanged. Do not add real authentication, passwords, durable
history, database/backend integration, or Docker changes.

Each chat must retain its transcript and associated opaque backend state in
Streamlit session state. Store that backend state unmodified; never inspect,
display, serialize, log, or persist it. Switching simulated customers clears
the list and starts a fresh chat, so chat state cannot cross demo identities.

## Simplicity rule

Make the code as simple as possible. Prefer the smallest clear change that
fits existing patterns; avoid unnecessary abstractions, dependencies,
configuration, and unrelated cleanup. Add only focused tests for behavior
introduced or changed by this work. For remaining code steps, add short
docstrings to each function added or changed.

## Step-by-step plan

### [DONE] Step 1 — Add chat-list state helpers and focused tests

- Extend `app/ui/session.py` with typed state helpers for creating a chat,
  selecting a chat, updating its title after the first user message, and
  clearing all chats when the selected customer changes.
- Keep each chat’s messages, conversation state, pending confirmation, and
  choice together. Generate stable internal chat IDs; do not use display
  titles as identifiers.
- Add tests in `tests/ui/test_session.py` covering initialization, chat
  creation/selection, per-chat state isolation, title behavior, opaque-state
  preservation, and customer-switch clearing.
- Do not change the existing login dropdown or fake-backend contract.

### [DONE] Step 2 — Push the new branch, then delete completed branches

- Confirmed the worktree was clean and `rohan/ui-sidebar-chats` was at
  `55e54d19ce81e353ca5bf195f41ab2086591a0a6`.
- Verified the pushed remote branch had the same tip and all six completed
  branch tips below were ancestors of it.
- Deleted the six completed local branches and their remote branch refs.
  Verified that no matching local or remote-tracking branches remain.
- Confirmed all six branch-tip commits remain reachable from
  `rohan/ui-sidebar-chats`. The new branch was not deleted.

### [DONE] Step 3 — Wire the sidebar and chat UI

- Updated `app/ui/streamlit_app.py` with a “New chat” action and selectable
  current-customer chat list, using the state helpers from Step 1.
- Starting a chat calls the existing `start_session`; selecting a chat
  restores its transcript and pending/choice UI without starting another
  backend session.
- Chat selection clears transient errors/notices; customer switching clears
  the list and begins a fresh chat. Existing loading, error, confirmation, and
  choice behavior is preserved.
- Added concise docstrings to functions changed or added in this step.
- Verified with `uv run python -m pytest tests/ui -q` (22 passed), Pylance
  diagnostics, and Streamlit AppTest interactions for chat creation,
  switching, title updates, choice/confirmation, and customer switching.

### [DONE] Step 4 — Document the behavior

- Updated `README.md` to explain creating and returning to chats, and that
  the list is temporary, cleared on simulated-customer switch, and lost on
  reload.
- Kept the existing description of simulated login and demo limitations.
- `git diff --check` passes.

### [DONE] Step 5 — Verify and review

- `uv run python -m pytest tests/ui -q`: 22 passed.
- `uv run python -m pytest -q`: 548 passed.
- `git diff --check` passed; the worktree is clean and the recent UI/README
  commits were reviewed.

## Branch and workspace notes

- At Step 2 cleanup, `rohan/ui-sidebar-chats` and
  `origin/rohan/ui-sidebar-chats` both pointed to
  `55e54d19ce81e353ca5bf195f41ab2086591a0a6`.
- Deleting a branch reference does not remove commits still reachable from
  another branch or tag. Push the new branch and verify it before deleting
  remote completed branches. GitHub does not archive a single branch; keep
  its ref or tag its tip if a named archive pointer is desired.
- Step 2 began with a clean worktree. No Dockerfile changes were made as part
  of branch cleanup.

## Deleted branches

Step 2 complete. The local and remote refs were deleted after verifying the
new branch was pushed. Each tip remains reachable from
`rohan/ui-sidebar-chats`:

| Branch | Tip commit |
|---|---|
| `rohan/frontend` | `3eb029be6223f1136214e8e030018a2f6f13f6e7` |
| `rohan/evaluation` | `eab38c631919129df8ca759e6df4befcccdcf5cd` |
| `rohan/demo-data-script` | `514056558dce91cf88978c0da882af6325105216` |
| `rohan/docker` | `5c127abbc13cd66f96cb1cd2783caa571d97eff9` |
| `rohan/complete-ui` | `d6a5732da450d9d8bb89967bbd6133549deab98f` |
| `rohan/documents` | `3217ab0aaa2be7361797c86e85eeb203cefbb422` |

## Todos

1. [DONE] Implement chat-list state helpers and focused tests.
2. [DONE] Push and verify the new branch, then delete completed local and remote refs.
3. [DONE] Wire sidebar navigation and chat switching.
4. [DONE] Document temporary chat history.
5. [DONE] Run verification and review the diff.
