# Duplicate Final Response Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a late bridge assistant message from being appended after its streaming draft has already been finalized, while preserving distinct assistant messages.

**Architecture:** The desktop renderer keeps the bridge message id in `runtime.seenBridgeMessageIds`. When a streaming draft is promoted to a local finalized assistant message, its bridge id is recorded in that set before the draft is cleared. Any later poll containing the same formal message is ignored by the existing id guard; messages with other ids continue through the normal append path.

**Tech Stack:** Browser JavaScript, Node VM regression harness, Python pytest wrapper, existing desktop bridge polling contract.

---

### Task 1: Add a failing frontend reconciliation test

**Files:**
- Create: `tests/frontend_message_reconciliation.test.js`
- Modify: `tests/test_desktop_bridge.py`

- [ ] **Step 1: Build a VM harness around the real renderer code**

Load `zero_agent/frontends/desktop/static/app.js` through Node's `vm` module only through the message-polling section, provide the browser globals needed during state initialization, and expose `state`, `getSessionRuntime`, `upsertPolledMessage`, and `finalizeAssistantReply` for the test. Stub only DOM rendering functions that are unrelated to the message-list invariant.

- [ ] **Step 2: Reproduce the late-final-message race**

Create an active session, deliver a partial assistant message with id `2`, finalize that draft through the real `finalizeAssistantReply`, then deliver the formal assistant message with id `2`. Assert that the session has exactly one assistant message and that its content is the finalized draft. Deliver a second formal assistant message with id `3` and assert it is appended as a separate message.

- [ ] **Step 3: Add the pytest entry point**

Add a Python test that runs the Node regression script with `subprocess.run(..., check=False)` and fails with the captured stdout/stderr when Node exits non-zero. This keeps the regression in the repository's normal `tests/` suite without adding a frontend package dependency.

- [ ] **Step 4: Run the regression and verify RED**

Run:

```bash
node tests/frontend_message_reconciliation.test.js
```

Expected: FAIL because `finalizeAssistantReply()` currently does not mark the finalized bridge id as seen, so the late id `2` message is appended a second time.

### Task 2: Record finalized bridge ids in the renderer

**Files:**
- Modify: `zero_agent/frontends/desktop/static/app.js:1607-1617`

- [ ] **Step 1: Mark the canonical finalized message id before clearing the draft**

Inside `finalizeAssistantReply()`, after constructing the canonical assistant message and before `runtime.assistantDraft = null`, initialize `runtime.seenBridgeMessageIds` if needed and add `msg.id` when it is a positive numeric bridge id. Keep the existing local message append and rendering behavior unchanged.

- [ ] **Step 2: Run the focused regression and verify GREEN**

Run:

```bash
node tests/frontend_message_reconciliation.test.js
```

Expected: PASS. The late id `2` message is rejected by the existing `seenBridgeMessageIds` guard, while id `3` remains visible as a distinct response.

### Task 3: Verify integration and cleanup

**Files:**
- Inspect only: `tests/test_desktop_bridge.py`, `zero_agent/frontends/desktop/static/app.js`

- [ ] **Step 1: Run focused Python tests**

Run:

```bash
uv run pytest tests/test_desktop_bridge.py tests/test_acp_bridge.py -q
```

Expected: all selected tests pass. If the environment lacks pytest dependencies, report the exact dependency failure and run the Node regression plus available checks directly.

- [ ] **Step 2: Run the frontend smoke scenario**

Run the Node regression again after the focused Python tests and confirm the final session state has one rendered canonical assistant message for id `2` and two messages after id `3`.

- [ ] **Step 3: Review the diff scope**

Confirm only the new regression test, its Python entry point, and the targeted `app.js` id bookkeeping changed; do not alter the user's unrelated pending changes.
