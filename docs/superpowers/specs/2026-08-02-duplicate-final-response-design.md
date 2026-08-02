# 前端最终回复重复渲染修复设计

## 问题

桌面前端处理一轮对话时，会先通过轮询中的 `partial` 将 assistant 流式草稿渲染到消息区；任务完成后，桥接层又返回带正式消息 `id` 的 assistant 最终消息。当前前端在草稿完成后仍将该最终消息追加到 `sess.messages`，导致最后一轮同时出现渲染后的内容和同一内容的原始消息，形成重复显示。

## 目标

- 同一轮对话在前端只显示一次回复，保留现有 Markdown、summary、工具调用和思考内容的渲染/折叠行为。
- 后端继续保存正式 assistant 消息，保证刷新、切换会话和历史恢复可用。
- 不影响不同轮次、不同消息 `id` 的正常追加。

## 方案

以桥接消息 `id` 作为流式草稿与最终消息的关联键，不按文本内容猜测去重。

### 数据流

1. `session/poll` 返回 `partial` 时，前端更新 `runtime.assistantDraft`，并在当前会话中原地更新流式 DOM。
2. `session/poll` 返回正式 assistant 消息时，前端先检查：
   - 消息角色为 `assistant`；
   - 当前存在未完成的 `assistantDraft`；
   - `draft.bridgeMessageId === msg.id`。
3. 满足条件时，将正式消息内容同步到草稿，调用 `finalizeAssistantReply()` 完成当前 DOM，并跳过 `sess.messages.push(msg)`。这样正式消息成为当前回复的 canonical 内容，但不会生成第二个可见消息。
4. 不满足条件时，沿用现有逻辑按消息 `id` 追加消息，并渲染独立 assistant 回复。
5. 刷新或切换会话时，后端返回的正式消息仍通过 `renderMessage()` 渲染，因此历史内容不会丢失。

## 边界与错误处理

- 消息缺少有效 `id`、id 不匹配或没有活动草稿时，不执行文本级去重，按独立消息处理。
- 正式消息内容作为最终内容来源，并沿用现有末尾 `[Info] Final response to user.` 标记清理逻辑。
- 当前轮已有工具/思考分段时保留这些分段；最终消息只更新关联的 assistant 草稿，不额外创建消息节点。
- 轮询结束后的 `finalizeAssistantReply()` 继续负责移除 cursor、停止计时器和标记 DOM 完成，不改变后端消息契约。

## 测试策略

先添加回归测试并确认其在修复前失败，再实现最小修复：

1. 同一 `id` 的流式草稿与正式 assistant 消息只保留一个前端 assistant 消息。
2. 不同 `id` 的正式 assistant 消息仍然追加为独立消息。
3. 后端正式消息仍保留在会话数据中，可供刷新后的历史渲染。

测试优先覆盖 `upsertPolledMessage()` 的可观察消息状态；不依赖 DOM 结构或文本全局去重实现细节。

## 非目标

- 不修改桥接层消息存储协议。
- 不修改 ACP/WebSocket 通知格式。
- 不重构现有 Markdown 或结构化内容渲染器。
- 不为相同文本的不同对话轮次增加内容级去重。
