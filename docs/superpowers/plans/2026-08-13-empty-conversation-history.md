# Empty Conversation History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent conversations without a non-empty real user message from being persisted or displayed in session history.

**Architecture:** Add one backend predicate for valid user content and enforce it at serialization, loading, and legacy log listing boundaries. Keep newly-created empty sessions in memory until the first valid prompt. Add a frontend predicate as a defensive display filter while preserving the active empty draft.

**Tech Stack:** Python, aiohttp bridge, vanilla JavaScript, pytest, Node VM regression tests.

---

## Task 1: Add RED regression tests

**Files:**
- Modify: `tests/test_desktop_bridge.py`
- Modify: `tests/test_bots.py`
- Modify: `tests/frontend_session_sidebar.test.js`

- [ ] **Step 1: Add backend persistence tests**

Add tests asserting a newly created manager session does not create `sessions.json`; adding `system` or `assistant` content still does not persist it; adding a non-empty `user` message persists it. Add a reload test with one empty and one valid persisted record; only the valid record loads and `sessions.json` is rewritten without the empty record.

- [ ] **Step 2: Add legacy log filtering test**

Create a log containing a Prompt whose JSON user content is whitespace and a Response pair, plus a valid pair. Assert `continue_cmd.list_sessions()` returns only the file/pair set containing a valid prompt and reports one round for a file containing only the valid pair.

- [ ] **Step 3: Add frontend display filtering test**

Export the session validity predicate from the test harness. Put an empty session and a session with `{ role: 'user', content: 'hello' }` into `state.sessions`, render the list, and assert only the valid session row exists.

- [ ] **Step 4: Run focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_desktop_bridge.py -q
uv run pytest tests/test_bots.py -q
node tests/frontend_session_sidebar.test.js
```

Expected: the new assertions fail because empty sessions are currently persisted/displayed and legacy listing counts any Prompt/Response pair.

## Task 2: Implement backend session filtering

**Files:**
- Modify: `zero_agent/frontends/desktop_bridge.py`
- Test: `tests/test_desktop_bridge.py`

- [ ] **Step 1: Add valid-user-content helpers**

Implement `_message_text()` and `session_has_user_message(sess_or_messages)` near persistence helpers. Accept non-empty strings and content blocks with non-empty `text` or `input_text`; ignore all other roles and blank values.

- [ ] **Step 2: Filter persistence and defer initial write**

Change `_persist_sessions()` to serialize only sessions satisfying the predicate. Remove the immediate `_persist_sessions()` call from `create_session()`. Keep `active_session_id` in memory and write it when the first valid message causes persistence.

- [ ] **Step 3: Clean old empty records on load**

In `_load_persisted_sessions()`, load only valid sessions, track whether any records were skipped, normalize `active_session_id` against the retained sessions, and rewrite `sessions.json` when stale empty records were removed.

- [ ] **Step 4: Ensure all post-message paths persist**

Keep `add_message()` persistence behavior; because it persists after append, the first valid user message writes the session. Ensure error/status paths cannot write an empty session due to the serializer filter.

- [ ] **Step 5: Run backend tests GREEN**

Run the focused bridge tests and confirm all pass.

## Task 3: Filter legacy history logs

**Files:**
- Modify: `zero_agent/bots/shared/continue_cmd.py`
- Test: `tests/test_bots.py`

- [ ] **Step 1: Require valid user prompts in `list_sessions()`**

For each parsed pair, retain only pairs whose prompt parses as a user message with non-empty real text. Return no entry when none qualify; count only retained valid pairs; preview only retained pairs.

- [ ] **Step 2: Guard snapshot creation**

Change `_snapshot_current_log()` to require at least one valid pair before writing a snapshot or clearing the active PID log.

- [ ] **Step 3: Run legacy history tests GREEN**

Run `uv run pytest tests/test_bots.py -q`.

## Task 4: Add frontend defensive filtering

**Files:**
- Modify: `zero_agent/frontends/desktop/static/app.js`
- Modify: `tests/frontend_session_sidebar.test.js`

- [ ] **Step 1: Add `sessionHasUserMessage()`**

Implement a frontend predicate matching the backend’s observable rule for string and text-block user messages. Export it through the existing VM test harness.

- [ ] **Step 2: Filter list rendering**

In `renderSessionList()`, skip invalid sessions before grouping and bucketing. Keep the active empty draft available to the chat area; it is simply absent from history/sidebar until it receives a valid user message.

- [ ] **Step 3: Filter bootstrap restoration**

When restoring `/sessions`, ignore invalid bridge sessions before creating local sessions. Do not alter active-session selection for retained valid sessions.

- [ ] **Step 4: Run frontend tests and syntax checks GREEN**

Run:

```bash
node --check zero_agent/frontends/desktop/static/app.js
node tests/frontend_session_sidebar.test.js
node tests/frontend_message_reconciliation.test.js
```

## Task 5: Final verification and review

**Files:** No planned changes.

- [ ] **Step 1: Run targeted Python verification**

Run `uv run pytest tests/test_desktop_bridge.py tests/test_bots.py -q`.

- [ ] **Step 2: Run frontend verification**

Run the two Node regression scripts and `node --check` for the changed frontend file.

- [ ] **Step 3: Review changed files**

Inspect the diff for accidental scope expansion, verify empty sessions are filtered at every persistence/display boundary, and run `git diff --check`.
