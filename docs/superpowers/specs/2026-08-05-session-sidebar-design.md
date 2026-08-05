# Session Sidebar Management Design

**Date:** 2026-08-05

## Goal

修复桌面前端会话删除按钮无效的问题，并将会话侧栏调整为接近 ChatGPT 的组织方式：未分组会话按更新时间分桶，用户可以创建分组并将会话移入或移出分组。

## Scope

本次只覆盖桌面前端及其现有 HTTP bridge 链路：

- `zero_agent/frontends/desktop/static/app.js`
- `zero_agent/frontends/desktop/static/index.html`
- `zero_agent/frontends/desktop/static/styles.css`
- `zero_agent/frontends/desktop/static/za-web.js`
- `zero_agent/frontends/desktop_bridge.py`
- 对应的前端回归测试和 bridge 测试

不新增完整后端分组实体 CRUD；继续使用当前会话 `group_id` 字段和前端分组元数据。

## Chosen Architecture

保留现有前端状态模型。前端维护会话列表、分组名称和折叠状态；bridge 维护会话本体、会话时间戳及会话到分组的归属。

- `state.sessions` 使用本地渲染 ID 映射到会话对象；每个对象通过 `bridgeSessionId` 指向 bridge 会话。
- `state.sessionGroups` 保存分组名称和折叠状态，并写入 `localStorage`。
- bridge 的 `/sessions` 返回 `createdAt`、`updatedAt`、`groupId`；前端据此进行时间分桶和分组渲染。
- bridge 的 `/session/{sid}/group` 更新分组归属后立即持久化 `sessions.json`。

该方案复用现有接口，改动小于将分组完全提升为后端实体，同时能保证会话归属在重启后保留。

## Sidebar Behavior

### Ordering and buckets

未分组会话按 `updatedAt` 降序排列，并按本地日期划分为：

1. 今天
2. 昨天
3. 最近 7 天
4. 更早

空时间桶不显示。用户分组显示在时间桶之前，组内会话按 `updatedAt` 降序排列；空分组仍显示，以便用户看到刚创建的分组。

### Actions

每个会话项包含：

- 点击主体：切换活动会话。
- 移动按钮：打开分组菜单，可选择已有分组、创建新分组或移出分组。
- 删除按钮：显示应用内确认弹窗，不依赖 Tauri shell 中不稳定的原生 `confirm()`。

动作按钮使用真实 `button` 元素、独立点击处理、`stopPropagation()` 和可读的 `aria-label`。按钮不依赖 hover 才能接收指针事件；hover 只控制视觉显示，键盘 focus 时仍可操作。

## Deletion Flow

1. 删除按钮触发确认弹窗。
2. 同一会话的并发删除请求复用 `sessionDeletionPromises`，防止重复点击。
3. 多会话场景使用 `bridgeSessionId` 调用 `session/delete`。
4. bridge 返回 404 时视为远端已经不存在，继续清理本地会话；其他错误保留本地状态并显示错误。
5. 删除当前会话时，从剩余会话中按更新时间选择最近项并激活。
6. 删除非当前会话时只重绘列表。
7. 最后一个会话使用 `session/replace` 原子替换远端会话，并重置现有本地会话容器，避免侧栏变空。
8. 删除成功后清理运行时状态、活动 agent 和 DOM 缓存关联，避免旧消息或忙碌状态泄漏到新活动会话。

## Group Flow

- 顶部“新建分组”按钮通过应用内 prompt 获取名称。
- 分组 ID 使用规范化后的名称；重复名称被拒绝。
- 选择已有分组或创建新分组后，前端先更新显示，再调用 `session/group`；调用失败时显示错误并重新同步本地状态，避免假成功。
- 删除分组只清除其会话的 `groupId`，会话回到时间分桶；不会删除会话。
- 分组名称与折叠状态写入 `localStorage`。
- bridge 更新 `group_id` 后调用 `_persist_sessions()`。

## Error Handling

- 删除失败不从 `state.sessions` 移除会话。
- 404 删除按幂等成功处理，解决 bridge 重启导致的本地残留。
- 分组更新失败显示可见错误，并保持会话在可恢复的本地状态。
- 损坏的分组 `localStorage` 数据被忽略，不阻止会话列表加载。
- 缺少唯一会话替换结果时抛出明确错误，不静默清空会话。

## Testing and Verification

新增或扩展测试覆盖以下可观察行为：

- 删除按钮事件链路和 RPC 参数使用 bridge 会话 ID。
- 删除当前及非当前会话后的活动会话选择。
- 删除 404 时的本地清理。
- 最后一个会话的原子替换。
- 时间桶排序和分组归属渲染所需字段。
- 分组修改写入持久化存储，重新加载后仍保留 `group_id`。
- 现有桌面 bridge 路由、会话删除和前端消息回归测试继续通过。

完成后运行对应 Python 测试、Node 前端回归测试、静态语法检查，并启动 bridge/前端执行一次删除、创建分组和移动会话的 smoke scenario。
