# 统一 Gateway 与会话隔离设计

**日期：** 2026-09-04  
**状态：** 已确认设计基线，待制定实施计划

## 1. 问题

ZeroAgent 当前同时存在桌面 bridge 和多个独立 bot 入口。桌面端通过 `AgentManager` 管理 `Session`，Telegram、微信、企业微信、钉钉、QQ、飞书和 Discord 则分别创建 `ZeroAgent/AgentRunner`。多数渠道在一个进程内复用全局 runner，因此聊天历史、取消信号和模型选择不能可靠地按聊天隔离。

当前代码还将会话、记忆、工作区、浏览器标签页和原始日志分散在不同模块中。桌面 bridge 使用一个 bridge 级 bearer token，WebSocket 会向所有连接广播 session state；机器人渠道没有统一的消息信封、事件序号、资源授权或跨进程会话所有权。

本设计将单机目标明确为：一个独立的中心 Gateway 持有所有 Agent 运行时，桌面端和外部渠道都通过适配器访问 Gateway。

## 2. 目标

1. 以一个中心 Gateway 统一接收消息、解析会话、调度 Agent、发布事件和持久化状态。
2. 每个外部 Conversation 映射到独立的可恢复 Session Runtime。
3. 群聊按群组共享上下文；私聊按用户隔离；不同 Channel 严格隔离。
4. `/stop`、`/new`、`/llm`、等待输入和计划执行均只影响目标 Session。
5. 统一隔离对话历史、工作记忆、工作区、附件、日志和浏览器上下文。
6. 适配器只处理平台协议，不再直接创建或持有 `ZeroAgent` 对话状态。
7. Gateway 重启、适配器重连和平台重复投递不会造成上下文串线或重复执行。
8. 保留现有桌面 RPC 和各渠道平台协议，支持渐进式迁移。

## 3. 非目标

- 本阶段不做跨 Channel 的统一用户身份或上下文合并。
- 本阶段不引入 Redis、PostgreSQL 或跨机器分布式 Worker。
- 不替换 Telegram、微信、Discord 等平台 SDK。
- 不允许通过 per-chat runner 的复制继续维护第二套会话编排逻辑。
- 不把本设计扩展为完整的操作系统级沙箱；本阶段实现 Gateway 级 workspace policy 和工具路径约束。
- 不将浏览器账号自动绑定到自然人身份；浏览器上下文只按 Session 受控分配。

## 4. 领域不变量

### 4.1 Session key

Gateway 使用以下稳定键解析外部会话：

```text
SessionKey =
    channel
    + channel_account_id
    + conversation_type
    + conversation_id
```

语义固定为：

- 私聊：`conversation_id` 为平台用户或私聊会话 ID。
- 群聊：`conversation_id` 为群组、频道或群聊会话 ID。
- `channel_account_id` 区分同一平台上的多个 bot/account。
- `actor_id` 不进入群聊 Session key，只用于授权、审计和限流。
- 相同自然人在 Telegram 和微信中的私聊属于不同 Session。

桌面手工创建的会话使用独立的本地 principal 和 opaque `session_id`，不与外部 Channel key 自动合并。

### 4.2 Runtime 隔离

每个有效 Session 只能绑定一个 `SessionRuntime`：

- 一个 Session 同时最多运行一个 turn。
- 不同 Session 可以并行，但受 Gateway 全局并发上限限制。
- 每个 Runtime 独占 `ZeroAgent`、`AgentRunner`、LLM history、handler task state、取消状态和当前 turn token。
- Session A 的取消、模型切换、等待输入和事件不能影响 Session B。
- Runtime 被淘汰前必须停止、持久化并清理；再次使用时从持久化状态恢复。

### 4.3 Context 隔离

- 同一群聊的成员共享该群聊 Session 上下文。
- 不同私聊、不同群聊、不同 Channel、不同 bot account 之间不共享上下文。
- Channel 级共享记忆只有在显式配置后才启用。
- 系统 SOP 可以只读共享；用户事实、工作记忆和 L4 会话归档默认属于 Session scope。

## 5. 总体架构

```text
                          ┌──────────────────────┐
Desktop UI ──────────────▶│                      │
                          │  Gateway Service     │
Channel SDK ─▶ Adapter ─▶│  - Auth / Policy     │
                          │  - Session Resolver  │
Other Adapter ───────────▶│  - Runtime Pool      │
                          │  - Event Store       │
                          │  - SQLite Store      │
                          └──────────┬───────────┘
                                     │
                         ┌───────────▼───────────┐
                         │ SessionRuntime         │
                         │ ZeroAgent + AgentRunner│
                         │ workspace/memory/tools │
                         └────────────────────────┘
```

### 5.1 Gateway Service

Gateway 是单机唯一的会话状态拥有者和 SQLite 写入者。它提供：

- inbound message 接收。
- Channel 与 Session key 解析。
- 命令分发和 Session 生命周期管理。
- 每 Session single-flight 调度。
- Agent Runtime 创建、恢复、停止和淘汰。
- 事件序号、事件存储、重连和 ack。
- session、turn、inbox 去重和资源状态持久化。
- Channel adapter 注册、心跳和能力声明。
- admin/session/message 三类权限检查。

最终 Gateway 为独立进程，不依附桌面窗口生命周期。当前 `desktop_bridge.py` 的父进程监控只适用于桌面 bridge，不能作为长期 Gateway 的退出条件。

### 5.2 Desktop adapter

桌面 bridge 逐步变为 Gateway 的本地管理/数据适配器：

- 保留现有 `/sessions`、`/session/{sid}`、`/config` 等兼容接口。
- 将现有 HTTP handler 转换为 Gateway 调用。
- 桌面 active session 只属于桌面 principal，不再作为 Gateway 全局 active session。
- WebSocket 事件必须按 principal 和 session 过滤，不再向所有连接广播全部 Session 状态。

### 5.3 Channel adapter

每个 Channel adapter 只负责：

1. 连接平台 SDK。
2. 验证平台签名、token 或 webhook 认证。
3. 规范化平台消息为 `InboundEnvelope`。
4. 向 Gateway 注册 channel/account 和能力。
5. 订阅属于自身 account 的 `GatewayEvent`。
6. 将 Gateway event 转为平台消息、按钮、文件或媒体。
7. 对 event delivery 做幂等处理。

Adapter 不得直接调用 `ZeroAgent()`、`AgentRunner.put_task()`、`runner.abort()` 或 `runner.next_llm()`。

本地第一版使用 loopback HTTP 作为命令/数据通道、WebSocket 作为事件通道。Adapter 使用独立 internal credential；桌面管理凭证不能被 Adapter 复用。

## 6. Gateway 接口

### 6.1 Adapter 注册

```text
POST /v1/adapters/register
```

请求包含：

- `adapter_id`：稳定的 adapter/account 标识。
- `channel`。
- `channel_account_id`。
- `capabilities`：文本、媒体、按钮、流式输出等。
- `protocol_version`。

Gateway 返回 connection credential、Gateway 版本和事件订阅信息。Adapter 必须定期发送 heartbeat；超时的连接不再接收新 delivery，但已持久化事件保留。
`adapter_id` 标识一个逻辑 adapter/account；进程重连时复用该 ID，每次连接另发唯一 `connection_id`。心跳和事件确认使用独立接口：

```text
POST /v1/adapters/{adapter_id}/heartbeat
POST /v1/events/{event_id}/ack
```

Adapter 心跳过期只撤销当前连接，不删除 adapter/account 的历史事件。


### 6.2 接收消息

```text
POST /v1/inbound
```

`InboundEnvelope` 至少包含：

```text
channel
channel_account_id
conversation_type
conversation_id
actor_id
external_message_id
text
attachments
reply_context
received_at
```

Gateway 先以 `(adapter_id, external_message_id)` 做持久化去重，再解析 Session key。重复消息返回 `duplicate=true` 及原 turn/session 信息，不再次启动 Agent。

### 6.3 会话事件

```text
WS /v1/events
```

或通过带 `after_seq` 的 HTTP 拉取接口恢复。事件至少包含：

```text
session_id
turn_id
event_id
seq
kind
payload
delivery_context
```

`kind` 包含：

- `accepted`
- `progress`
- `chunk`
- `waiting`
- `completed`
- `failed`
- `cancelled`
- `interrupted`

事件必须带目标 adapter/account 或 desktop principal。订阅者只能接收授权范围内的事件。

Gateway 对流式 chunk 做短时间合并后写入事件日志，避免每个 token 产生一次 SQLite 事务。事件以 at-least-once 方式投递，Adapter 以 `event_id` 去重并 ack；重复投递不能重复发送不可幂等的平台操作。
`seq` 在一个 Session 内单调递增。事件压缩后，`after_seq` 拉取可以先返回当前 Session snapshot，再返回保留的终态事件；它不承诺无限期重放所有历史 chunk。

当一个事件需要同时投递给 Adapter 和 Desktop 等多个订阅者时，事件正文只保存一次，ack 状态放在按订阅者区分的 `event_deliveries` 记录中，不能使用单个全局 `acked_at` 判断所有订阅者均已收到。

### 6.4 Session 控制

统一由 Gateway 提供：

```text
POST /v1/sessions/{session_id}/cancel
POST /v1/sessions/{session_id}/new
POST /v1/sessions/{session_id}/model
GET  /v1/sessions/{session_id}
GET  /v1/sessions/{session_id}/events?after_seq=N
```

外部命令先由 Gateway 解析为当前 Session 操作：

- `/stop`：只取消该 Session 的当前 turn。
- `/new`：关闭当前 generation，创建同一 Session key 的新 generation；旧 Session 保留为历史记录。
- `/llm`：只修改该 Session 的 `model_override`。
- `/status`：返回该 Session 的状态，不返回其他会话状态。
- `/continue`、waiting 输入和计划执行：继续当前 Session 的状态机。

同一 Session 在运行中收到新的普通消息时返回明确的 `SESSION_BUSY`，不静默追加到另一个 turn。是否允许排队由 Gateway policy 决定，本阶段默认拒绝并要求调用方等待或取消。
所有有副作用的 Session 控制请求都必须带稳定 `request_id`。Gateway 对同一 principal 的重复 `request_id` 返回原结果，不重复执行；`/new` 的关闭旧 generation、创建新 generation 和更新 binding 必须在同一事务中完成。

## 7. Session 生命周期

```text
created → idle → running → idle
             │
             └→ closed
```

- `waiting` 表示 Agent 正在等待该 Session 的下一条用户输入。
- Gateway 重启时，未完成的 `running` turn 标记为 `interrupted`，不能伪造为 completed。
- 可恢复的 waiting 状态保留 pending task contract、evidence ledger 和等待 payload。
- Session lifecycle 值固定为 `created`、`idle`、`running`、`waiting` 和 `closed`。`turn.status` 的终态为 `completed`、`failed`、`cancelled` 或 `interrupted`；turn 终态事件写入后，Session 回到 `idle`，除非已显式关闭。
- 删除 Session 是显式 admin/session 操作；其 Runtime、事件订阅、附件和专属日志按保留策略清理。

## 8. 数据存储

当前 `zero_agent/frontends/session_store.py` 作为 SQLite 访问基础，但必须由中心 Gateway 独占写入。Channel adapter 不得直接打开或修改会话数据库。

### 8.1 sessions

在当前会话元数据基础上增加：

```text
id
channel
channel_account_id
conversation_type
conversation_id
owner_principal
lifecycle
workspace_id
memory_scope
created_at
updated_at
model_override
plan state
```

Session key 字段使用独立列，避免依赖不可查询的 JSON。外部会话的历史 generation 不覆盖旧记录。

### 8.2 conversation_bindings

保存当前 Conversation key 指向的 active Session：

```text
channel
channel_account_id
conversation_type
conversation_id
session_id
updated_at
```

`/new` 在一个事务中关闭旧 generation、创建新 Session 并更新 binding，保证重复点击不会产生多个 active Session。

### 8.3 messages

沿用当前按 Session 的消息行模型，保留平台元数据、附件引用和等待输入信息。消息正文不保存附件二进制。

### 8.4 inbox_messages

```text
adapter_id
external_message_id
session_id
turn_id
received_at
```

对 `(adapter_id, external_message_id)` 建唯一约束，解决 webhook、长轮询或平台重试造成的重复执行。

### 8.5 turns

```text
id
session_id
request_id
status
source
started_at
finished_at
error_code
```

`request_id` 用于 API 重试幂等；一个 request 不能生成两个 turn。

### 8.6 events

```text
session_id
turn_id
event_id
seq
kind
payload_json
created_at
```

对 `(session_id, seq)` 和 `event_id` 建唯一约束。终态事件必须持久化；历史事件按保留策略清理，但至少保留最近 Session snapshot 和每个 turn 的终态事件，以支持适配器重连。

### 8.7 event_deliveries

保存每个事件针对每个订阅者的投递状态：

```text
event_id
subscriber_id
attempts
acked_at
```

`event_deliveries` 对 `(event_id, subscriber_id)` 建唯一约束。事件正文与投递状态分离，避免 Desktop 的 ack 覆盖 Adapter 的 ack。

SQLite 继续使用 WAL、foreign keys 和有限 busy timeout。中心 Gateway 是唯一业务写入者，因此不再允许多个独立 AgentManager 对同一数据库执行全量 `sync_sessions()`。

## 9. 资源隔离

### 9.1 Workspace

客户端不能直接决定任意文件系统路径。`cwd` 改为 Gateway 解析的 `workspace_id`，由 allowlist 映射到实际根目录。

默认目录结构：

```text
gateway-data/
  sessions/
    <session_id>/
      workspace/
      memory/
      attachments/
      logs/
```

桌面会话如果需要操作真实项目目录，必须引用已授权的 workspace。Gateway 解析真实路径后拒绝：

- `..` 穿越。
- 符号链接逃逸。
- 未授权绝对路径。
- 不属于当前 Session 或授权项目根的附件输出路径。

同一个真实项目被多个 Session 使用时，必须显式声明共享 workspace，并由 Gateway 提供项目级并发策略；默认不共享。

### 9.2 Memory

- `system`：内置 SOP 和只读结构说明。
- `channel`：可选的渠道级共享记忆。
- `session`：默认的私有工作记忆、事实和 L4 归档。

`MemoryManager` 和内置 memory 工具需要接收 Session resource scope，不能继续把配置级 `global_mem.txt` 自动作为所有 Session 的可写共享记忆。

### 9.3 Tools

文件工具、代码工具和浏览器保存结果共用同一个 `WorkspacePolicy`。路径约束不能只存在于 bridge handler；所有工具调用都必须在工具入口做最终校验。

浏览器工具必须携带 Session 对应的 browser context/tab ID。目标标签页失效时只能返回明确错误，禁止自动切换到另一个活动标签页。

### 9.4 Logs and attachments

日志和附件路径包含 `session_id` 与 `turn_id`，不使用单纯 PID 作为逻辑会话标识。原始 LLM 日志必须受 Session owner 权限保护，并继续执行 API key、凭证和不必要敏感字段的脱敏。

## 10. 鉴权与授权

Gateway 将访问分为：

```text
message plane
  提交消息、订阅当前 adapter 的事件

session plane
  读取、取消、恢复、切换当前 Session

admin plane
  配置、渠道启停、workspace allowlist、路径打开、运行状态
```

规则：

- Adapter credential 只能访问自身 `channel_account_id` 的 message/event 范围。
- Desktop principal 只能访问本地桌面创建或授权给它的 Session。
- Admin credential 才能调用渠道生命周期、配置和路径打开接口。
- actor 身份由 Adapter 从平台消息中提取并传入；Gateway 统一执行 allowlist、群聊控制和审计。
- CORS 只解决浏览器来源，不作为授权机制。
- 长期 bearer token 不放入可被任意页面读取的持久 URL；浏览器使用短期 bootstrap credential，内部 Adapter 使用受限凭证。

## 11. 渠道生命周期

最终由 Gateway supervisor 管理 Adapter 进程：

1. 启动 Adapter 并注入 Gateway endpoint 和 internal credential。
2. 等待 Adapter register/heartbeat，而不是仅依据 PID 或脚本命令行判断健康。
3. 停止时先撤销 adapter connection，再停止子进程。
4. Adapter 退出时，Gateway 停止向其投递新消息，但保留未确认事件。
5. Adapter 重连后按 `adapter_id` 和 `after_seq` 恢复授权范围内的事件。

现有 `desktop_commands.py` 的 PID 扫描和 terminate/kill 流程可作为迁移期兼容实现，但不能作为长期会话或渠道健康状态的唯一事实来源。

## 12. 错误与故障处理

| 场景 | Gateway 行为 |
| --- | --- |
| 重复 inbound message | 返回原 session/turn，`duplicate=true`，不重复执行 |
| 同一 Session 正在运行 | 返回 `SESSION_BUSY` 和当前 turn 信息 |
| 未授权 actor/adapter | 返回 401/403，不创建 Session，不写入 Agent history |
| 未知 session | 返回结构化 404，不回退到 active session |
| workspace 越界 | 返回 `WORKSPACE_FORBIDDEN`，不执行工具 |
| LLM 失败 | 生成 failed turn 和 durable error event，Session 回到可继续状态 |
| 用户取消 | 只设置当前 Runtime 的 cancel event，生成 cancelled event |
| Gateway 重启 | running turn 标记 interrupted；waiting 状态按持久化 contract 恢复 |
| Adapter 断线 | 保留事件和终态，重连后按 seq/ack 恢复 |
| SQLite 暂时锁冲突 | 使用有限重试/timeout；最终返回持久化错误，不静默丢状态 |
| 数据库损坏或 schema 不兼容 | Gateway 拒绝启动并报告明确错误，不覆盖原库 |

错误响应使用稳定 `error_code` 和可读 `message`。适配器不能把失败事件伪装成成功回复。

## 13. 渐进式迁移

### 阶段一：契约和测试

新增 `SessionKey`、`InboundEnvelope`、`GatewayEvent` 和隔离测试矩阵；不改变现有渠道入口。先证明：群聊共享、跨渠道隔离、取消独立、模型切换独立、重复消息只执行一次。

### 阶段二：抽取 Gateway Runtime

从 `desktop_bridge.py` 抽出独立的 Gateway session/runtime/store/policy 模块。桌面 HTTP handler 改为兼容适配器，保留现有前端 RPC。

### 阶段三：建立独立 Gateway 生命周期

提供独立 Gateway 入口和本地 HTTP/WS 协议。桌面 UI 作为 admin/data client；不要让 Gateway 依附 `ZA_DESKTOP_PARENT_PID`。

### 阶段四：迁移一个渠道

先迁移 Telegram 作为代表性适配器，覆盖文本、`/new`、`/stop`、`/status`、`/llm`、waiting 输入和附件。验证通过后迁移微信、企业微信、钉钉、QQ、飞书和 Discord。

### 阶段五：统一资源隔离

迁移 workspace、memory、attachment、log 和 browser context；删除渠道模块级 `za`、`runner` 和重复的 command/task orchestration。

### 阶段六：监督和清理

加入 adapter register/heartbeat、事件 ack、重连恢复、限流和运行指标。确认所有渠道不再直接创建 Agent 后，移除 per-channel runner 分支和旧的共享状态路径。

## 14. 验证策略

### 14.1 Contract tests

使用 fake adapter 和 fake Agent Runtime 覆盖：

1. 同一私聊连续消息保留历史。
2. 两个私聊历史完全独立。
3. 同一群聊不同成员共享上下文。
4. 两个群聊完全独立。
5. Telegram 与微信同一 actor 不合并。
6. A 的 `/stop` 不影响 B。
7. A 的 `/llm` 不影响 B。
8. `/new` 创建新 generation，旧历史仍可恢复。
9. 重复 external message ID 只产生一个 turn。
10. event seq 断线恢复不跨 Session、不重复终态。
11. Gateway 重启将运行中的 turn 标记为 interrupted。
12. workspace、attachment 和 browser context 不能越界。
13. Adapter credential 不能读取其他 account 或 admin 资源。

### 14.2 Existing contract tests

保持现有桌面 bridge、SessionStore、渠道配置、前端 RPC 和各渠道消息格式测试。兼容接口的测试必须证明旧 RPC 最终访问的是同一个 Gateway Session，而不是另建本地状态。

### 14.3 Smoke

启动独立 Gateway、两个 fake adapters 和桌面 client，执行以下路径：

```text
adapter A -> message -> session runtime -> event -> adapter A
adapter B -> message -> independent session runtime -> event -> adapter B
adapter A reconnect -> after_seq recovery
Gateway restart -> session restoration
```

真实渠道迁移后，再启动 Telegram 与一个群聊/私聊组合验证平台侧发送和取消行为。

## 15. 风险与取舍

- **单 Gateway 是单点故障。** 用独立进程监督、SQLite WAL、interrupted 状态和 Adapter 重连缓解；不保留渠道侧独立 Agent fallback，避免 split-brain。
- **每 Session 一个 Runtime 有内存成本。** 通过 idle runtime 淘汰和持久化恢复控制成本；淘汰不得丢历史。
- **事件持久化增加 SQLite 写入。** 对 chunk 做合并并保留终态；事件保留策略必须受限，不能无限增长。
- **共享真实项目目录仍可能发生文件级竞争。** 默认每 Session 独立 workspace；共享项目必须显式授权并增加项目级锁策略。
- **旧 `/continue` 原始日志格式不能直接代表新 Session。** 迁移期保留只读兼容扫描；新 Gateway turn 以 SQLite/session event 为权威。

## 16. 完成标准

设计进入实施计划前，以下事实必须固定：

- Gateway 是独立生命周期的单机中心进程。
- Desktop 和 Channel 都是 Gateway client/adapter。
- Session key 包含 Channel 和 channel account。
- 群聊共享，跨 Channel 隔离。
- 一个 Session 一个 Runtime，同 Session single-flight。
- SQLite 只有 Gateway 写入。
- 消息去重、事件 seq/ack、Session owner 和 workspace policy 是统一 Gateway 能力。
- 不允许用全局 runner、全局 memory 或浏览器默认标签页作为跨 Session fallback。
