# Session Sidebar Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复桌面前端会话删除按钮无效的问题，并提供按更新时间分桶、可创建和移动会话的 ChatGPT 风格会话侧栏。

**Architecture:** 保留当前 `state.sessions` 本地渲染模型，使用 `bridgeSessionId` 调用现有 HTTP bridge。前端负责分组名称/折叠元数据和侧栏渲染，bridge 负责会话数据、时间戳及 `group_id` 归属持久化。删除多会话走幂等 `session/delete`，删除最后一个会话走 `session/replace`。

**Tech Stack:** 原生 JavaScript、DOM/CSS、Tauri static frontend、aiohttp bridge、Node `vm` regression tests、pytest。

---

## Files and Responsibilities

- Modify `zero_agent/frontends/desktop/static/app.js`: session timestamp helpers, deletion state transitions, action-button behavior, group assignment rollback, and time-bucket rendering.
- Modify `zero_agent/frontends/desktop/static/styles.css`: make session action buttons reliably targetable while retaining hover/focus visibility and keyboard accessibility.
- Modify `zero_agent/frontends/desktop_bridge.py`: centralize group assignment and persist `sessions.json` after a successful assignment.
- Modify `tests/test_desktop_bridge.py`: verify group assignment survives manager reload.
- Create `tests/frontend_session_sidebar.test.js`: exercise deletion through the button handler, bridge/local ID separation, last-session replacement, 404 idempotency, grouping, and time ordering.
- Keep `zero_agent/frontends/desktop/static/index.html` and `za-web.js` unchanged unless the regression test proves an existing DOM/RPC contract is missing; the current dialog markup and RPC routes already exist.

---

### Task 1: Add failing session-sidebar regression tests

**Files:**
- Create: `tests/frontend_session_sidebar.test.js`
- Modify: `tests/test_desktop_bridge.py`

- [ ] **Step 1: Add the Node VM harness and failing deletion tests**

Create `tests/frontend_session_sidebar.test.js` with a small fake DOM that records event listeners and supports the operations used by the session sidebar (`createElement`, `append`, `appendChild`, `classList`, `dataset`, `style`, `textContent`, `setAttribute`, `addEventListener`, `focus`, and `click`). Load `app.js` through `vm.runInNewContext` up to the existing `// ─── Slash commands` marker, then append test exports:

```js
const testExports = `
  globalThis.__testExports = {
    state,
    createLocalSession,
    createSessionItem,
    deleteSession,
    closeSession,
    renderSessionList,
    assignSessionToGroup,
    createGroup,
    getSessionTimestamp,
    compareSessionsByUpdatedAt,
    getSessionTimeBucket,
    setSessionDom: (list, title) => { sessionListEl = list; sessionTitleEl = title; },
    setErrorHook: (fn) => { showError = fn; },
    setConfirmHook: (fn) => { showConfirmDialog = fn; },
    setActiveHook: (fn) => { setActiveSession = fn; },
  };
`;
```

Add tests that fail against the current implementation because the timestamp helpers are absent and the active-session fallback uses insertion order rather than the newest remaining session:

```js
(async () => {
  const { state, createSessionItem, deleteSession, closeSession,
    getSessionTimeBucket, compareSessionsByUpdatedAt } = context.__testExports;
  const calls = [];
  context.window.zeroAgent = {
    rpc: async (method, params) => {
      calls.push({ method, params });
      if (method === 'session/delete') return { ok: true };
      throw new Error(`unexpected RPC ${method}`);
    },
  };

  const old = { id: 'local-old', bridgeSessionId: 'bridge-old', title: 'Old', messages: [], updatedAt: Date.now() - 10_000 };
  const newest = { id: 'local-newest', bridgeSessionId: 'bridge-newest', title: 'Newest', messages: [], updatedAt: Date.now() - 100 };
  const active = { id: 'local-active', bridgeSessionId: 'bridge-active', title: 'Active', messages: [], updatedAt: Date.now() - 5_000 };
  state.sessions.clear();
  state.sessions.set(old.id, old);
  state.sessions.set(newest.id, newest);
  state.sessions.set(active.id, active);
  state.activeId = active.id;
  context.__testExports.setConfirmHook(async () => true);
  context.__testExports.setActiveHook((id) => { state.activeId = id; });

  const item = createSessionItem(active);
  const deleteButton = item.children.find((child) => child.className === 'session-delete');
  assert.ok(deleteButton, 'session delete button must be present');
  deleteButton.click();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(state.sessions.has(active.id), false);
  assert.equal(state.activeId, newest.id);
  assert.deepEqual(calls[0], { method: 'session/delete', params: { sessionId: 'bridge-active' } });
  assert.equal(getSessionTimeBucket(newest.updatedAt, newest.updatedAt), 'today');
  assert.ok(compareSessionsByUpdatedAt(newest, old) < 0);
})();
```

Also add cases for a 404 delete (the RPC rejects with `{status: 404, message: 'session not found'}` and the local session is still removed), the one-session replacement response, and assigning a session to an existing group. The assertions must check observable state and RPC arguments, not source strings.

- [ ] **Step 2: Run the focused Node test and confirm the expected RED failure**

Run:

```bash
node tests/frontend_session_sidebar.test.js
```

Expected: FAIL because the new timestamp helpers are not defined and/or the active-session fallback does not select `local-newest`. Do not change the production code before observing this failure.

- [ ] **Step 3: Add the failing bridge persistence test**

Append this test to `tests/test_desktop_bridge.py`:

```python
def test_session_group_assignment_persists_and_reloads(monkeypatch, tmp_path) -> None:
    config = AgentConfig(
        llm_backends={"default": LLMBackendConfig(
            name="default", provider="openai", api_key="test",
            api_base="https://x", model="m",
        )},
        workspace_dir=str(tmp_path / "workspace"),
        memory_dir=str(tmp_path / "memory"),
        sessions_dir=str(tmp_path / "sessions"),
    )
    monkeypatch.setattr(desktop_bridge, "load_default_config", lambda: config)
    first = desktop_bridge.AgentManager()
    session = first.create_session()

    first.set_session_group(session.id, "work")

    second = desktop_bridge.AgentManager()
    assert second.sessions[session.id].group_id == "work"
```

- [ ] **Step 4: Run the focused Python test and confirm the expected RED failure**

Run:

```bash
uv run pytest tests/test_desktop_bridge.py::test_session_group_assignment_persists_and_reloads -q
```

Expected: FAIL with `AttributeError` because `AgentManager.set_session_group` does not exist yet. If `uv` is unavailable, use the repository's configured Python environment and report the exact command/output instead of silently skipping the test.

---

### Task 2: Implement bridge group persistence and frontend session invariants

**Files:**
- Modify: `zero_agent/frontends/desktop_bridge.py:1328-1338`
- Modify: `zero_agent/frontends/desktop/static/app.js:840-1380`
- Modify: `zero_agent/frontends/desktop/static/styles.css:318-347,1193-1223`

- [ ] **Step 1: Add a lock-protected bridge group-assignment method**

Add this method to `AgentManager` near `delete_session`/`replace_session`:

```python
def set_session_group(self, sid: str, group_id: str | None) -> dict:
    with self.lock:
        sess = self.sessions.get(sid)
        if not sess:
            raise web.HTTPNotFound(
                text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False),
                content_type="application/json",
            )
        normalized = str(group_id).strip() if group_id else None
        sess.group_id = normalized or None
        sess.updated_at = time.time()
        self._persist_sessions()
        result = {"ok": True, "sessionId": sid, "groupId": sess.group_id}
    emit_session_state(sess, "group-changed")
    return result
```

Change `set_session_group_handler` to parse the request and return `manager.set_session_group(sid, data.get("groupId"))`; do not mutate `sess.group_id` directly in the handler.

- [ ] **Step 2: Add pure frontend time helpers and use them everywhere sessions are sorted/bucketed**

Insert before `renderSessionList`:

```js
function getSessionTimestamp(sess) {
  return Number(sess?.updatedAt) || Number(sess?.createdAt) || 0;
}

function compareSessionsByUpdatedAt(a, b) {
  return getSessionTimestamp(b) - getSessionTimestamp(a);
}

function getSessionTimeBucket(timestamp, now = Date.now()) {
  const ts = Number(timestamp) || now;
  const age = now - ts;
  if (isSameDay(ts, now)) return 'today';
  if (isSameDay(ts, now - 86400000)) return 'yesterday';
  if (age >= 0 && age < 7 * 86400000) return 'week';
  return 'older';
}
```

Use `compareSessionsByUpdatedAt` for groups and ungrouped sessions. In `renderTimeBuckets`, use `getSessionTimeBucket(ts)` rather than overlapping inline predicates. This makes the “today/yesterday/recent 7 days/older” boundaries deterministic and prevents future timestamps from entering the recent bucket.

- [ ] **Step 3: Make deletion transactionally update the local UI and select the newest remaining session**

Update `discardSession` to remove all local session-owned state, including `_domCache.delete(sess.id)`. Replace the active-session index selection in `deleteSession` with timestamp ordering:

```js
const wasActive = state.activeId === id;
await discardSession(sess);

if (wasActive) {
  const next = [...state.sessions.values()].sort(compareSessionsByUpdatedAt)[0];
  if (next) setActiveSession(next.id);
  else state.activeId = null;
} else {
  renderSessionList();
}
```

Keep the existing bridge-ID call and 404-as-idempotent handling. For the one-session path, update `resetReplacedSession` to clear `groupId`, reset `createdAt`/`updatedAt` from the replacement or current time, clear `messages`, runtime state, active agents, and DOM cache, then render the reset session. Do not remove the local container in this path.

Ensure `closeSession` continues to deduplicate requests but does not mutate local state when a non-404 bridge error is raised; `showError` remains the only failure side effect.

- [ ] **Step 4: Make group assignment and deletion persistent and rollback-safe**

Change `assignSessionToGroup` to retain `previousGroupId`, create a missing local group, call `session/group` with `sess.bridgeSessionId`, and only keep the new assignment after the RPC succeeds. On a non-404 failure, restore `previousGroupId`, remove a newly created empty group, save metadata, render the list, show the error, and rethrow so callers can observe failure.

Change `deleteGroup` to clear each matching session through `assignSessionToGroup(sess.id, null)` so the bridge and `sessions.json` are updated consistently. Keep the session objects; only their grouping changes.

- [ ] **Step 5: Make action buttons reliably targetable without changing the visual pattern**

Keep the existing hover/focus opacity behavior, but make the buttons independent hit targets by adding `z-index: 1`, `position: relative`, and `pointer-events: auto` to `.session-item .session-move` and `.session-item .session-delete`. Keep `stopPropagation()` on both handlers. Add `aria-expanded`/`aria-controls` only if the DOM implementation needs them; do not introduce a second action-menu abstraction.

- [ ] **Step 6: Run the focused tests and confirm GREEN**

Run:

```bash
node tests/frontend_session_sidebar.test.js
uv run pytest tests/test_desktop_bridge.py::test_session_group_assignment_persists_and_reloads -q
```

Expected: all focused tests pass. If a failure is caused by the test fake DOM rather than behavior, fix the harness first; do not weaken the observable assertions.

---

### Task 3: Complete regression coverage and verify existing contracts

**Files:**
- Modify: `tests/frontend_session_sidebar.test.js`
- Modify: `tests/test_desktop_bridge.py`

- [ ] **Step 1: Cover all deletion transitions in the Node regression**

Assert these cases using real `deleteButton.click()` or direct exported session behavior:

1. Deleting a non-active session calls `session/delete` with its bridge ID and leaves `state.activeId` unchanged.
2. Deleting the active session selects the remaining session with the greatest `updatedAt`, not the next `Map` insertion entry.
3. A bridge 404 removes the local session and does not leave an error banner.
4. A non-404 bridge error leaves the local session in `state.sessions` and invokes the error hook.
5. Deleting the only session calls `session/replace`, keeps the same local ID, adopts the replacement bridge ID, clears messages/group/runtime state, and leaves one session.
6. A second delete click while the first promise is pending reuses the same deletion promise and emits one bridge operation.

- [ ] **Step 2: Cover time buckets and groups**

Use fixed timestamps passed to `getSessionTimeBucket` and assert `today`, `yesterday`, `week`, and `older`. Assert descending timestamp comparison. Create a group, assign a session to it, verify the group ID and bridge payload, then ungroup it and verify `groupId: null` is sent.

- [ ] **Step 3: Expand bridge persistence and endpoint coverage**

Add an HTTP-level assertion to the existing route tests that `POST /session/{sid}/group` returns the normalized group ID, then instantiate a fresh `AgentManager` against the same `sessions_dir` and assert the persisted group ID. Keep existing delete/replace idempotency tests unchanged.

- [ ] **Step 4: Run all relevant automated tests**

Run:

```bash
node tests/frontend_message_reconciliation.test.js
node tests/frontend_session_sidebar.test.js
uv run pytest tests/test_desktop_bridge.py -q
```

Expected: both Node regressions pass and the full desktop bridge test module reports zero failures.

---

### Task 4: Smoke-test the actual frontend path

**Files:**
- No additional files unless smoke testing exposes a reproducible defect.

- [ ] **Step 1: Check JavaScript syntax and diff whitespace**

Run:

```bash
node --check zero_agent/frontends/desktop/static/app.js
node --check zero_agent/frontends/desktop/static/za-web.js
git diff --check
```

Expected: syntax checks exit successfully and `git diff --check` prints no whitespace errors.

- [ ] **Step 2: Start the authenticated desktop bridge**

Use the repository's existing bridge entry point and configured environment; do not start a second bridge on port `14168`. Verify the bridge reports ready before interacting with the static frontend.

- [ ] **Step 3: Exercise the user-visible flow**

Open the served frontend and verify:

1. Existing sessions render in the “今天/昨天/最近 7 天/更早” buckets.
2. Clicking the right-side delete button opens the in-app confirmation dialog.
3. Confirming deletion removes the selected session and activates the newest remaining session.
4. Creating a group shows an empty group in the sidebar.
5. Moving a session into the group removes it from the time bucket and shows it under the group.
6. Reloading the frontend keeps the session in the group.
7. Deleting the group returns its session to the time bucket without deleting the session.

Record the actual bridge/frontend command and observed result in the final response; do not claim UI success from static inspection alone.

- [ ] **Step 4: Run the final relevant test set and report exact evidence**

Run the commands from Tasks 3 and 4 again after the smoke scenario. Report pass counts/exit status and any environment limitation exactly.
