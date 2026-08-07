# 会话侧栏优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有视觉风格的前提下，实现可折叠时间/自定义分组、后端持久化分组、拖拽入组和长列表事件优化。

**Architecture:** 扩展现有 `AgentManager`，把分组实体保存到与会话同目录的 `groups.json`，会话继续保存 `group_id`；前端把所有时间桶和用户分组渲染为统一的 section，折叠状态保存在浏览器，拖拽和菜单都调用同一个可回滚的移动函数。保留现有删除/替换逻辑。

**Tech Stack:** 原生 JavaScript、DOM/CSS、aiohttp、Node `vm` 回归测试、pytest。

---

## 文件职责

- Modify `zero_agent/frontends/desktop_bridge.py`: 分组实体模型、`groups.json` 读写、分组 CRUD、会话归属验证和路由。
- Modify `zero_agent/frontends/desktop/static/za-web.js`: 暴露 `groups/list`、`groups/create`、`groups/delete` RPC。
- Modify `zero_agent/frontends/desktop/static/app.js`: 统一 section 渲染、折叠状态、稳定 group ID 迁移、拖拽生命周期、菜单移动、事件委托和回滚。
- Modify `zero_agent/frontends/desktop/static/styles.css`: 不改变既有颜色/字体/尺寸，只增加 section 状态、拖拽状态和动态操作菜单样式。
- Modify `zero_agent/frontends/desktop/static/index.html`: 将侧栏操作菜单准备为可复用语义容器；现有 session menu 保留。
- Modify `tests/frontend_session_sidebar.test.js`: 先添加时间桶折叠、拖拽 payload、分组 CRUD 和回滚的行为测试。
- Modify `tests/test_desktop_bridge.py`: 添加分组实体 CRUD、空分组重载、归属更新和删除分组不删除会话测试。

## Task 1: Add RED regression tests

**Files:** `tests/frontend_session_sidebar.test.js`, `tests/test_desktop_bridge.py`

- [ ] **Step 1: Extend the Node harness with section/drag exports and failing assertions**

Export `buildGroupElement`, `renderTimeBuckets`, `toggleGroup`, `createGroup`, `deleteGroup`, `assignSessionToGroup`, and a `setSessionList` hook. Add tests asserting:

```js
const section = buildGroupElement(
  { id: 'group-work', name: '工作', collapsed: true, position: 0 },
  [],
);
assert.equal(section.querySelector('.session-group-header').getAttribute('aria-expanded'), 'false');
assert.equal(section.querySelector('.session-group-header').getAttribute('aria-controls'), section.querySelector('.session-group-sessions').id);

const dragItem = createSessionItem({
  id: 'local-1', bridgeSessionId: 'bridge-1', title: 'Task', groupId: null,
  updatedAt: Date.now(), messages: [],
});
assert.equal(dragItem.getAttribute('draggable'), 'true');
const dragStart = dragItem.listeners.get('dragstart')[0];
const dataTransfer = { values: new Map(), setData(type, value) { this.values.set(type, value); } };
dragStart({ dataTransfer });
assert.deepEqual(JSON.parse(dataTransfer.values.get('application/json')), {
  sessionId: 'bridge-1', localSessionId: 'local-1',
});
```

Add a test where `groups/create` rejects and assert the empty group is removed from local state. Add a test where moving to a group rejects and assert the session returns to its previous `groupId`.

- [ ] **Step 2: Run the Node test and verify RED**

Run `node tests/frontend_session_sidebar.test.js`. It must fail because current sections are divs without ARIA/content IDs, there is no draggable contract, and group creation has no bridge persistence.

- [ ] **Step 3: Add failing Python bridge tests**

Add tests that instantiate `AgentManager` with a temporary `sessions_dir` and assert:

```python
group = manager.create_group("工作")
assert group["id"].startswith("group-")
assert manager.list_groups()[0]["name"] == "工作"
reloaded = desktop_bridge.AgentManager()
assert reloaded.list_groups()[0]["id"] == group["id"]
manager.delete_group(group["id"])
assert session.id in manager.sessions
assert manager.sessions[session.id].group_id is None
```

Also assert assigning an unknown group ID raises `web.HTTPNotFound` or `web.HTTPBadRequest`, and the HTTP route returns the normalized group object. Run the focused tests and observe the expected missing-method failure.

## Task 2: Implement bridge group entities

**Files:** `zero_agent/frontends/desktop_bridge.py`, `tests/test_desktop_bridge.py`

- [ ] **Step 1: Add `SessionGroup` serialization helpers and manager state**

Add a small dataclass or dict-normalization helpers near `Session` with fields `id`, `name`, `created_at`, `position`. Initialize `self.groups` as an ordered dict and `self.groups_store` alongside `sessions_dir`. Use `groups.json` payload `{ "version": 1, "groups": [...] }`.

- [ ] **Step 2: Load groups before session associations**

In `AgentManager.__init__`, load groups before `_load_persisted_sessions`. Accept old sessions whose `group_id` is a name: after loading, match it to a group name; if no match exists, create a stable migrated group ID and update that session. Persist once after migration. Empty groups must survive because they live in `groups.json`, not inferred from sessions.

- [ ] **Step 3: Add atomic group CRUD methods**

Implement these exact manager interfaces:

- `list_groups(self) -> List[dict]` returns public groups ordered by `position`, each with `id`, `name`, `createdAt`, `position`, and `sessionIds`.
- `create_group(self, name: str) -> dict` returns the created public group.
- `delete_group(self, group_id: str) -> dict` returns `{ "ok": True, "groupId": group_id, "sessionIds": affected_ids }`.

`create_group` trims and rejects blank/duplicate names, allocates `group-` + UUID, sets `position` after the current maximum, persists groups, and returns the public group. `delete_group` clears matching session `group_id`, persists sessions and groups under the manager lock, and returns the deleted ID plus affected session IDs. It never removes a `Session`.

Update `set_session_group` to reject non-null IDs not present in `self.groups`, persist the session, and emit the existing state notification.

- [ ] **Step 4: Replace `/groups` and add CRUD routes**

`list_groups_handler` returns `manager.list_groups()` with each group’s `sessionIds`. Add `create_group_handler` at `POST /groups`, `delete_group_handler` at `DELETE /groups/{gid}`. Register routes after the existing group route. Keep current `POST /session/{sid}/group` route, now validating group IDs.

- [ ] **Step 5: Run focused Python tests GREEN**

Run the new tests and existing group persistence tests. Confirm zero failures before moving to frontend work.

## Task 3: Add RPC mappings and unified collapsible sections

**Files:** `zero_agent/frontends/desktop/static/za-web.js`, `app.js`, `styles.css`, `tests/frontend_session_sidebar.test.js`

- [ ] **Step 1: Add RPC cases**

Add:

```js
case 'groups/create': return http('/groups', { method: 'POST', body: params || {} });
case 'groups/delete': {
  const gid = params.groupId || params.id;
  if (!gid) throw new Error('groups/delete missing groupId');
  return http(`/groups/${encodeURIComponent(gid)}`, { method: 'DELETE' });
}
```

- [ ] **Step 2: Normalize group loading and local state**

Change `state.sessionGroups` entries to `{ id, name, position, createdAt, sessionIds, collapsed }`. Load `groups/list` before `loadSessionGroups()`. Retain old name-keyed local metadata only as a migration lookup; do not create a local group that is absent from bridge data. Use `SECTION_COLLAPSE_STORAGE_KEY` for `today`, `yesterday`, `week`, `older`, and group IDs.

- [ ] **Step 3: Implement one section builder**

Create `buildSessionSection({ id, label, sessions, collapsed, userGroup, onDelete })`. Use a `<section>` wrapper, a `<button>` header with `aria-expanded` and `aria-controls`, and a content `<div>` with stable ID. Render children only when expanded. User group headers accept drop events; time headers do not. Use the existing text, icon, spacing and delete button classes.

- [ ] **Step 4: Make `renderSessionList` use section ordering**

Partition valid assigned sessions by group ID; put orphaned/unknown assignments back into ungrouped. Sort custom groups by `position`, then append time sections in fixed order. Use local date bucket calculation and descending `updatedAt` order. Empty custom groups remain visible; empty time sections do not.

- [ ] **Step 5: Make section toggles persistent and accessible**

`toggleGroup` updates `collapsed`, writes the browser state, and rerenders. `toggleTimeBucket` does the same under the bucket key. `aria-expanded` and the `collapsed` class must agree. Default `today` and `yesterday` expanded; `week` and `older` collapsed unless saved.

- [ ] **Step 6: Add CSS for sections without changing visual tokens**

Style section wrapper/header/content using current `.session-group-*` rules. Add `.session-group-header.drag-over`, `.session-item.dragging`, and collapsed-state selectors. Do not add new palette colors; use `var(--bg-hover)`, `var(--bg-active)`, `var(--accent)`, and existing border variables.

- [ ] **Step 7: Run Node tests GREEN**

Run `node tests/frontend_session_sidebar.test.js` and the existing message reconciliation test. Fix production code, not assertions, when behavior fails.

## Task 4: Implement drag and menu fallback

**Files:** `app.js`, `styles.css`, `tests/frontend_session_sidebar.test.js`

- [ ] **Step 1: Add drag lifecycle to session rows and group headers**

On session rows set `draggable=true`; `dragstart` writes JSON with bridge/local IDs, adds `.dragging`, and sets `effectAllowed='move'`. Group headers accept `dragenter`/`dragover`, add `.drag-over`, and on `drop` parse/validate the payload and call `assignSessionToGroup(localSessionId, groupId)`. `dragleave` and `dragend` always remove temporary classes.

- [ ] **Step 2: Make one move function authoritative**

`assignSessionToGroup(sessionId, groupId)` does nothing for the same group, validates the destination, captures previous state, calls `session/group` using `bridgeSessionId`, updates local state after success, and rolls back on failure. A newly created group is removed locally only if it has no assigned sessions and its create operation failed or the subsequent assignment failed.

- [ ] **Step 3: Wire menu and create-group actions to group CRUD**

Top-level create calls `groups/create`, then renders the returned group. Menu “new group” creates through the same function and assigns through the same move function. Delete group confirmation calls `groups/delete`; on success remove the group from local state and clear matching session IDs from local state using the returned affected IDs.

- [ ] **Step 4: Add keyboard semantics and menu focus**

Use buttons for headers and menu items. Header Enter/Space toggles. On opening the session menu, focus the first actionable group item; Escape closes it. Keep existing `aria-label` text and `stopPropagation()` for action buttons.

- [ ] **Step 5: Run focused Node tests GREEN**

Run the sidebar and reconciliation Node tests. Confirm drag payload, same-group no-op, successful move, failed move rollback, and menu fallback assertions pass.

## Task 5: Optimize long-list event handling

**Files:** `app.js`, `styles.css`, `tests/frontend_session_sidebar.test.js`

- [ ] **Step 1: Move session-row click/context/drag handling to the list container**

Use `data-session-id` and resolve the local session in one delegated listener. Keep only the per-row button handlers that need direct async state. Do not create global document listeners per row. Ensure action-button clicks stop at the button.

- [ ] **Step 2: Collapse row actions into one menu trigger**

Replace the separate move/delete buttons with one familiar ellipsis icon button per session row. Reuse the existing session menu: list move targets first, then `移出分组` when applicable, and `删除会话` as the destructive final action. Opening the menu creates only the current session's action items; the user-visible style remains hover/focus reveal.

- [ ] **Step 3: Ensure collapsed sections omit child nodes**

`buildSessionSection` must not append session rows when collapsed. Add a test that counts zero `.session-item` children for a collapsed section.

- [ ] **Step 4: Run Node tests and syntax checks**

Run `node --check` for `app.js` and `za-web.js`, then both Node regressions.

## Task 6: Full verification and browser smoke

**Files:** no planned changes.

- [ ] **Step 1: Run Python desktop bridge tests**

Run `uv run pytest -q tests/test_desktop_bridge.py`. If pytest is unavailable in the uv environment, report the exact environment error and run the available focused Python checks without claiming the full suite passed.

- [ ] **Step 2: Start authenticated bridge and load frontend**

Use an explicit local token, open `http://127.0.0.1:14168/#token=<token>`, and verify the actual page shows loaded sessions and `Ready` or a visible configuration error without losing the session list.

- [ ] **Step 3: Exercise user-visible behavior**

With real sessions: collapse/expand `今天` and a custom group; create an empty group; drag one ungrouped session into it; verify it leaves its time section; use menu to ungroup; reload and verify group/assignment; delete group and verify the session remains.

- [ ] **Step 4: Run final checks and inspect diff**

Run `git diff --check`, syntax checks, both Node regressions, and the Python test module. Inspect only changed files and report exact exit statuses.
