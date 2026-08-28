# SQLite Session Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the desktop bridge's full-file `sessions.json` persistence with transactional SQLite storage while preserving the bridge API, runtime behavior, and raw `model_responses_*.txt` logs.

**Architecture:** Add a focused `SessionStore` around Python's standard-library `sqlite3`. Store session metadata, one message per row, groups, active-session state, and schema version in `workspace/sessions/sessions.sqlite3`. Keep `AgentManager` and `Session` as the business-facing API; the manager translates dataclasses to store DTOs and continues to own runtime agents, threads, status, and partial streaming state. Existing `sessions.json` is intentionally neither read nor migrated.

**Tech Stack:** Python 3, `sqlite3`, `threading`, `pytest`, existing `aiohttp` desktop bridge, existing raw response-log format.

---

## File map

- Create: `zero_agent/frontends/session_store.py` — SQLite schema, connection pragmas, transactions, serialization of durable session/message/group state, and load/save operations. It must not import `desktop_bridge.Session` to avoid a circular dependency.
- Modify: `zero_agent/frontends/desktop_bridge.py:391-527,612-635,686-836,858-919,1134-1234` — construct the store, load SQLite state, replace JSON/group persistence, and keep existing lifecycle/API behavior.
- Create: `tests/test_session_store.py` — isolated unit tests for schema, message ordering, transactions, cascading deletes, active-session state, and malformed JSON metadata.
- Modify: `tests/test_desktop_bridge.py:21-56,125-180` and relevant existing persistence tests — assert SQLite paths and restart behavior; retain raw-log deletion and no-empty-session contracts.
- Modify: `docs/superpowers/specs/2026-08-27-session-sqlite-design.md` only if implementation discovers a contract mismatch; otherwise leave the approved spec unchanged.

The existing `.gitignore` rule `workspace/` already excludes the database and its WAL/SHM sidecars; no ignore-rule change is needed.

---

### Task 1: Add failing SQLite store tests

**Files:**
- Create: `tests/test_session_store.py`

- [ ] **Step 1: Define test fixtures and durable sample records**

Create an isolated store fixture using `tmp_path / "sessions.sqlite3"`. Use plain dictionaries rather than importing `desktop_bridge.Session`:

```python
import json
import sqlite3

import pytest

from zero_agent.frontends.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    value = SessionStore(tmp_path / "sessions.sqlite3")
    value.initialize()
    yield value
    value.close()


def session_record(session_id="sess-test"):
    return {
        "id": session_id,
        "title": "Test chat",
        "cwd": "/tmp/project",
        "created_at": 10.0,
        "updated_at": 20.0,
        "msg_seq": 0,
        "last_error": "",
        "terminal_status": "",
        "terminal_reason": "",
        "model_override": None,
        "token_usage": {"input": 3},
        "group_id": None,
        "sub_agents": [],
        "plan_path": None,
        "plan_status": "inactive",
        "plan_task": "",
    }


def message_record(message_id=1, role="user", content="hello"):
    return {
        "id": message_id,
        "role": role,
        "content": content,
        "ts": float(message_id),
        "image_ids": ["img-1"] if role == "user" else [],
    }
```

- [ ] **Step 2: Write tests for initialization and empty-state behavior**

Add tests that `initialize()` creates `sessions`, `messages`, `groups`, `app_state`, and `schema_meta`, writes schema version `1`, is idempotent, and does not create a session row until explicitly persisted. The tests must inspect the SQLite schema through `sqlite3.connect()` and fail before the store exists.

- [ ] **Step 3: Write tests for session/message round-trip and ordering**

Add tests that:

```python
def test_session_and_messages_round_trip(store):
    session = session_record()
    store.append_message(session, message_record(1))
    session["msg_seq"] = 2
    session["updated_at"] = 30.0
    store.append_message(session, message_record(2, "assistant", "world"))

    loaded = store.load_state()

    assert loaded["sessions"]["sess-test"]["title"] == "Test chat"
    assert loaded["sessions"]["sess-test"]["messages"] == [
        {"id": 1, "role": "user", "content": "hello", "ts": 1.0, "image_ids": ["img-1"]},
        {"id": 2, "role": "assistant", "content": "world", "ts": 2.0, "image_ids": []},
    ]
```

Also cover two sessions to prove messages are partitioned by `session_id`, and message IDs are ordered numerically rather than by insertion order.

- [ ] **Step 4: Write tests for groups, active state, cascade delete, and no migration**

Cover:

- group create/load/delete metadata;
- `set_session_group()`-equivalent persistence through the store;
- `set_active_session()` round-trip;
- deleting a session removes its messages but leaves another session intact;
- an existing `sessions.json` file is never read or modified because the store only opens `sessions.sqlite3`.

- [ ] **Step 5: Run the new tests and verify RED**

Run:

```bash
uv run pytest tests/test_session_store.py -q
```

Expected: collection/import failures because `SessionStore` does not yet exist. Do not implement before recording this failure.

- [ ] **Step 6: Commit the failing tests**

```bash
git add tests/test_session_store.py
git commit -m "test: define SQLite session store contract"
```

---

### Task 2: Implement the focused `SessionStore`

**Files:**
- Create: `zero_agent/frontends/session_store.py`

- [ ] **Step 1: Add connection and schema initialization**

Implement a path-normalizing `SessionStore` with these public methods:

```python
class SessionStore:
    def __init__(self, path: str | os.PathLike[str]): ...
    def initialize(self) -> None: ...
    def load_state(self) -> dict: ...
    def append_message(self, session: dict, message: dict) -> None: ...
    def upsert_session(self, session: dict) -> None: ...
    def delete_session(self, session_id: str) -> None: ...
    def load_groups(self) -> list[dict]: ...
    def save_group(self, group: dict) -> None: ...
    def delete_group_and_unassign_sessions(self, group_id: str) -> None: ...
    def set_active_session(self, session_id: str | None) -> None: ...
    def close(self) -> None: ...
```

Open a fresh connection per operation so calls from bridge worker threads never share a connection incorrectly. Configure every connection with:

```python
conn = sqlite3.connect(str(self.path), timeout=5.0)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = ON")
conn.execute("PRAGMA busy_timeout = 5000")
conn.execute("PRAGMA journal_mode = WAL")
```

`initialize()` creates the directory, creates `groups`, `sessions`, `messages`, `app_state`, and `schema_meta` in dependency order, and inserts schema version `1` with `INSERT OR IGNORE`. Guard store operations with a process-local `RLock` and use explicit transactions for writes.

- [ ] **Step 2: Implement durable session and message serialization**

Use parameterized SQL. Store JSON-valued fields using `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`; parse them with a helper that returns the declared default for missing or malformed values. Reconstruct a message as:

```python
{
    "id": int(row["message_id"]),
    "role": row["role"],
    "content": row["content"],
    "ts": float(row["timestamp"]),
    **metadata,
}
```

`append_message()` must perform one transaction that upserts session metadata and inserts exactly one message row. Use a plain `INSERT` for the message primary key so duplicate `(session_id, message_id)` writes fail rather than silently hide a logic error. The manager must call `upsert_session()` only for sessions that already satisfy `session_has_user_message()`; the store serializes the complete durable record it receives.

- [ ] **Step 3: Implement group, active-state, and delete operations**

`save_group()` uses an upsert on `groups.id`. `set_active_session()` writes or deletes `app_state.active_session_id`. `delete_group_and_unassign_sessions()` must clear `group_id` for all matching sessions and delete the group in one transaction. `delete_session()` deletes only the session row; foreign keys with `ON DELETE CASCADE` remove messages. Keep deleting the raw response log outside the store transaction because the store has no filesystem ownership.
- [ ] **Step 4: Implement state loading**

`load_state()` reads sessions ordered by `updated_at DESC, id`, then reads all messages ordered by `session_id, message_id`. Return a structure that `AgentManager` can consume without importing bridge dataclasses:

```python
{
    "sessions": {
        "sess-id": {**session_fields, "messages": [message_dict, ...]},
    },
    "groups": [group_dict, ...],
    "active_session_id": "sess-id" | None,
}
```

If `active_session_id` is absent or points to a missing session, return the newest loaded session ID as the fallback. A malformed JSON metadata value must not crash the entire load; use its field default and preserve other session data.

- [ ] **Step 5: Run the store tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_session_store.py -q
```

Expected: all store tests pass.

- [ ] **Step 6: Commit the store implementation**

```bash
git add zero_agent/frontends/session_store.py tests/test_session_store.py
git commit -m "feat: add SQLite session store"
```

---

### Task 3: Replace `AgentManager` JSON persistence with SQLite

**Files:**
- Modify: `zero_agent/frontends/desktop_bridge.py:41-65,391-527,546-574,612-622,686-763`

- [ ] **Step 1: Add the store dependency and initialize it**

Import `SessionStore`, replace `group_store` with:

```python
self.session_store = SessionStore(
    os.path.join(self.sessions_dir, "sessions.sqlite3")
)
self.session_store.initialize()
self._load_persisted_sessions()
```

Keep `self.groups` and `self.sessions` as the existing in-memory ordered dictionaries. Do not delete or rename the existing `sessions.json` on startup.

- [ ] **Step 2: Rewrite `_persist_groups()` and `_load_persisted_groups()`**

Use the store for group CRUD. Preserve the public `list_groups()`, `create_group()`, `delete_group()`, and `set_session_group()` behavior. For delete-group, first record affected sessions' previous `group_id` and timestamps, update their in-memory fields, then call `delete_group_and_unassign_sessions()` so the session updates and group deletion share one database transaction. On failure, restore the previous in-memory group IDs and timestamps before re-raising.

`_load_persisted_groups()` must load the group list from `session_store.load_state()` or a dedicated `load_groups()` call and rebuild `SessionGroup` objects with the existing field names and ordering.

- [ ] **Step 3: Rewrite `_persist_sessions()` as metadata synchronization**

Replace JSON construction and `os.replace()` with a loop over valid in-memory sessions:

```python
for sess in self.sessions.values():
    if session_has_user_message(sess):
        self.session_store.upsert_session(_session_to_store_dict(sess))
self.session_store.set_active_session(
    self.active_session_id if self.active_session_id in persisted_ids else fallback_id
)
```

The helper must omit runtime-only fields and convert `token_usage`, `sub_agents`, and any other JSON-valued fields into Python objects for `SessionStore` to encode. It must not write empty sessions. When there are no valid sessions, delete all persisted session rows or leave the database with no sessions and clear `active_session_id`; do not delete the SQLite file.

- [ ] **Step 4: Rewrite `_load_persisted_sessions()`**

Load the store state once, rebuild each `Session` with the existing `_session_from_persisted()` conversion, and attach messages from the store state. Set `restore_history = bool(sess.messages)` as before. Normalize runtime status to `idle`, ignore malformed individual rows, and set `active_session_id` only to an existing session, otherwise to the newest session.

Do not call or inspect the old JSON loader. Existing `sessions.json` remains untouched and old sessions are intentionally absent from the new in-memory list.

- [ ] **Step 5: Update `add_message()` for incremental message writes**

After appending the message to `sess.messages`, call:

```python
if session_has_user_message(sess):
    self.session_store.append_message(
        _session_to_store_dict(sess),
        _message_to_store_dict(msg),
    )
```

Remove the call that rewrites all sessions through JSON. On a store error, remove the just-appended in-memory message and restore `msg_seq`, title, and `updated_at` to their previous values before re-raising. Preserve the existing empty-session rule.

- [ ] **Step 6: Update lifecycle callers to persist metadata only**

Keep existing `_persist_sessions()` calls after terminal state, plan-state, group, delete, replace, and error transitions. Their implementation now updates only SQLite session metadata and active-session state; message rows are already inserted by `add_message()`.

For the first plan prompt, ensure the user message is persisted through `add_message()` before the subsequent metadata-only persistence. For assistant completion and error messages, use the same existing `add_message()` path.

- [ ] **Step 7: Run existing bridge persistence tests and fix integration failures**

Run:

```bash
uv run pytest tests/test_desktop_bridge.py -q
```

Expected initially: failures in tests that assert `sessions.json` exists or monkeypatch `group_store`. Update only those assertions to target `sessions.sqlite3` and the new store boundary; preserve all observable API and lifecycle assertions.

- [ ] **Step 8: Commit the manager integration**

```bash
git add zero_agent/frontends/desktop_bridge.py tests/test_desktop_bridge.py
git commit -m "feat: persist desktop sessions in SQLite"
```

---

### Task 4: Preserve raw logs, deletion semantics, and restart behavior

**Files:**
- Modify: `zero_agent/frontends/desktop_bridge.py:644-667,765-836,858-919`
- Modify: `tests/test_desktop_bridge.py` relevant session/restart tests
- Modify: `tests/test_sessions.py` only if a new response-log assertion is required

- [ ] **Step 1: Add a test proving each desktop session keeps a separate raw log**

Create two sessions, assert their `log_path` values differ, write distinct content to both files, delete the first session, and assert:

```python
assert not first_log.exists()
assert second_log.read_text(encoding="utf-8") == "second"
```

The existing behavior must remain unchanged; only the session metadata source changes to SQLite.

- [ ] **Step 2: Add a restart test for the new SQLite store**

Create two sessions, add user and assistant messages, set one active, close the first manager, instantiate a second manager against the same temporary config, and assert:

- both session IDs and message order are present;
- titles, `cwd`, `updated_at`, plan metadata, and token usage survive;
- `active_session_id` survives when valid;
- `status == "idle"`, `agent is None`, `thread is None`, and `partial is None` after restart;
- an old `sessions.json` fixture with a distinct legacy session is ignored and remains byte-for-byte unchanged.

- [ ] **Step 3: Verify delete and replace behavior**

Assert that deleting a session removes its SQLite row and messages, removes only its owned raw log, and selects the newest remaining session when deleting the active one. Assert `session/replace` creates a new ID, deletes the old raw log, and leaves the database with the replacement session only after it has a valid user message.

- [ ] **Step 4: Verify `/continue` remains log-backed**

Keep or add a test that creates a legacy/PID `model_responses_*.txt`, calls the existing `list_resume_sessions()` path, and confirms it remains discoverable. Also assert that owned desktop logs are excluded from the external resume list. Do not change `continue_cmd.py`'s storage format.

- [ ] **Step 5: Run focused integration tests**

Run:

```bash
uv run pytest tests/test_session_store.py tests/test_desktop_bridge.py tests/test_sessions.py -q
```

Expected: all focused tests pass.

- [ ] **Step 6: Commit compatibility changes**

```bash
git add zero_agent/frontends/desktop_bridge.py tests/test_desktop_bridge.py tests/test_sessions.py
 git commit -m "test: preserve session restart and log compatibility"
```

---

### Task 5: Add database failure and concurrency coverage

**Files:**
- Modify: `tests/test_session_store.py`
- Modify: `zero_agent/frontends/session_store.py` only where tests expose a concrete issue
- Modify: `tests/test_desktop_bridge.py` for manager rollback behavior

- [ ] **Step 1: Test duplicate message rejection**

Insert the same `(session_id, message_id)` twice and assert `sqlite3.IntegrityError`; verify the original row remains unchanged.

- [ ] **Step 2: Test transaction rollback**

Force a failing metadata JSON serialization or SQL write inside `append_message()` and assert no partial session/message row remains. The store must not leave a half-created session.

- [ ] **Step 3: Test manager in-memory rollback**

Monkeypatch the store append method to raise, call `manager.add_message()`, and assert the in-memory message list, sequence, title, and timestamp match their pre-call values. Verify the exception is visible to the caller.

- [ ] **Step 4: Test concurrent append safety**

Use two threads appending distinct message IDs to the same initialized store and assert both rows exist in order afterward. Keep the test deterministic by using distinct IDs and joining both threads before loading state.

- [ ] **Step 5: Run the failure/concurrency tests**

Run:

```bash
uv run pytest tests/test_session_store.py tests/test_desktop_bridge.py -q
```

Expected: all tests pass without swallowed persistence errors.

- [ ] **Step 6: Commit reliability coverage**

```bash
git add zero_agent/frontends/session_store.py tests/test_session_store.py tests/test_desktop_bridge.py
git commit -m "test: cover SQLite session failure handling"
```

---

### Task 6: Full verification and cleanup

**Files:**
- Modify: implementation files only if a failing verification exposes a real contract issue.

- [ ] **Step 1: Run all relevant Python tests**

Run:

```bash
uv run pytest tests/test_session_store.py tests/test_desktop_bridge.py tests/test_sessions.py tests/test_bots.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run frontend regression tests**

Run:

```bash
node tests/frontend_message_reconciliation.test.js
node tests/frontend_plan_ui.test.js
node tests/frontend_session_sidebar.test.js
```

Expected: each command exits with status `0`; the frontend continues using the unchanged bridge API.

- [ ] **Step 3: Run a desktop bridge smoke scenario**

Start the existing desktop bridge entrypoint using the repository's normal command, create a session, send one prompt through the existing RPC/API, observe the message in the response, stop the bridge, restart it, and observe the same session/message through `session/list` and `session/poll`. Confirm `workspace/sessions/sessions.sqlite3` exists and the old `sessions.json` bytes are unchanged.

- [ ] **Step 4: Run static checks for the changed Python files**

Run:

```bash
uv run python -m compileall zero_agent/frontends/session_store.py zero_agent/frontends/desktop_bridge.py tests/test_session_store.py
```

Expected: command exits successfully.

- [ ] **Step 5: Check the final diff for scope and stale JSON paths**

Run:

```bash
git diff --check HEAD~5
```

Then search changed source for active writes or reads of `sessions.json`. The only permitted references are compatibility comments/tests documenting that the file is intentionally ignored; no runtime path may load or rewrite it.

- [ ] **Step 6: Commit final cleanup if required**

```bash
git add zero_agent/frontends/session_store.py zero_agent/frontends/desktop_bridge.py tests/test_session_store.py tests/test_desktop_bridge.py tests/test_sessions.py
 git commit -m "chore: finalize SQLite session persistence"
```

Do not modify unrelated frontend, LLM, or raw-log behavior.
