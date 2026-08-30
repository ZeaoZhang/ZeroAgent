# 证据契约修复设计

## 背景

当前修改为 `ToolDefinition` 增加了 `evidence_kind`，并让声明了可完成证据类型的注册工具把 `OPEN` 任务推进到 `EXECUTING`。但证据状态仍主要按内置工具名判断：一个声明 `evidence_kind="execute"` 的自定义工具若返回普通值（例如 `{"result": 42}`），会被记录为 `status="unknown"`，随后无法通过 `complete_task(evidence_refs=[...])` 完成。

同时，vision 工具的失败路径把自动锚点替换成单个换行，导致失败重试时看不到任务契约和最近证据；新增契约测试中有一个测试没有真正 dispatch 自定义工具；新增测试还存在类型标注缺失和过时模块说明。

## 目标

- 让声明 `read`、`write`、`execute`、`web` 或 `verify` 的自定义注册工具，其正常返回值默认成为成功证据。
- 显式 `status="error"` 或 `status="interrupt"` 的结果仍不可作为成功证据。
- 让证据记录、完成计数修正和自动提示使用一致的状态判定。
- vision 成功和失败后都在证据入账后生成包含当前任务契约的自动锚点。
- 用回归测试锁定普通返回值、显式失败、失败锚点和真实自定义记录场景。
- 补齐本次新增测试的类型标注，删除已过时的迁移说明。

## 非目标

- 不改变 `ToolRegistry.with_builtins()` 的工具范围。
- 不改变工具 schema 的公开格式。
- 不新增自定义状态回调或其他扩展 API。
- 不改变工具异常抛出后的既有 loop 处理方式。
- 不把 `memory`、`user` 或 `system` 证据改为可完成证据。

## 设计

### 1. 证据状态判定

在 `BaseHandler` 中让状态判定接收工具声明的 `evidence_kind`。现有内置工具的专用判定保持不变。对注册工具的通用路径：

1. 仅当结果中的 `status`（不区分大小写）为 `error` 或 `interrupt` 时返回对应失败状态。
2. `status="success"` 返回成功状态。
3. 工具声明了 eligible `evidence_kind` 且没有上述显式失败状态时，只要 handler 正常返回，普通返回值默认视为成功。
4. 没有 eligible 声明时，继续记录为未知或非完成证据。

handler 抛出异常时沿用现有 loop 处理，不伪造成功证据。`_successful_completion_correction()` 和自动提示判断复用同一规则，避免证据记录与完成控制分叉。

### 2. 自动提示与 vision

`dispatch()` 的顺序保持为：执行工具 → 记录证据 → 刷新自动提示 → 触发 `tool_after` hook。vision handler 对成功和失败结果均返回自动提示标记；由统一收尾逻辑在证据已经存在后生成锚点。自定义 `next_prompt`、`WAIT_FOR_USER`、`FAIL` 和 `REQUEST_COMPLETION` 控制不被覆盖。

### 3. 测试与文档

新增或修正以下行为测试：

- eligible 自定义工具返回普通字典后记录成功证据并可完成；
- eligible 自定义工具显式返回 error 后不可完成；
- vision session 不可用时仍生成带 `<task_contract>` 的提示；
- non-eligible 自定义工具测试先注册并 dispatch，再验证引用被拒绝；
- 现有自定义 next prompt、等待用户和 memory/control 语义继续保持。

同时为新增测试 helper、局部 handler 和 vision 测试补齐参数/返回值标注，删除“当前生产代码尚未实现、测试应失败”的模块说明。

## 验收标准

- `ToolDefinition(evidence_kind="execute")` 的 handler 返回 `{"result": 42}` 时，证据记录为 `kind="execute", status="success"`，`complete_task(evidence_refs=[1])` 返回 `REQUEST_COMPLETION`。
- 同一工具返回 `{"status": "error", ...}` 或 `{"status": "interrupt", ...}` 时，完成请求被拒绝。
- vision 工具失败后返回的 `next_prompt` 包含 `<task_contract>` 和对应 evidence record。
- non-eligible 证据仍不能满足 `EXECUTING` 完成要求。
- 受影响测试和完整 pytest 套件通过。
