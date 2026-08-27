# SQLite 会话存储设计

**日期：** 2026-08-27  
**状态：** 已确认，待实施

## 1. 目标

将桌面端当前聚合式 `workspace/sessions/sessions.json` 替换为 SQLite 存储，解决会话数量和消息数量增长后每次变更都要重写整个 JSON 文件的问题。

本次只替换桌面端会话状态存储。LLM 原始交互日志 `model_responses_*.txt` 保持不变，继续服务于 `/continue`、原始日志查看和历史回放。

## 2. 范围与非目标

### 范围

- 使用 `workspace/sessions/sessions.sqlite3` 保存桌面端会话。
- 持久化会话元数据、消息、active session 和分组实体。
- 新会话、消息追加、分组变更、删除和重启恢复全部切换到 SQLite。
- 保持现有 `AgentManager`、HTTP bridge 和前端 API 契约。
- 保留现有专属日志 `model_responses_session_<sid>.txt`。

### 非目标

- 不迁移既有 `sessions.json` 数据。
- 不把 `model_responses_*.txt` 导入 SQLite。
- 不替换 `/continue` 的原始日志扫描机制。
- 不把附件二进制内容写入 SQLite。
- 不引入第三方 ORM 或数据库依赖，使用 Python 标准库 `sqlite3`。

切换后，旧 `sessions.json` 不读取、不删除，作为用户可手工保留的旧数据；新运行只从 SQLite 创建和读取会话。

## 3. 存储边界

```text
内存
  AgentManager.sessions
  LiteLLMSession.history

SQLite 主存储
  workspace/sessions/sessions.sqlite3
  ├── sessions
  ├── messages
  ├── groups
  └── app_state

原始 LLM 日志
  workspace/sessions/model_responses_*.txt

浏览器本地状态
  localStorage：侧栏折叠偏好
  sessionStorage：附件预览数据和文件名
```

`AgentManager` 继续持有运行时对象和线程；数据库只持久化可恢复的数据，不保存 `agent`、`thread`、`partial` 等进程对象或流式临时状态。

## 4. 数据模型

### 4.1 `sessions`

保存当前 `Session` 中可跨进程恢复的字段：

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    cwd TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    msg_seq INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    terminal_status TEXT NOT NULL DEFAULT '',
    terminal_reason TEXT NOT NULL DEFAULT '',
    model_override TEXT,
    token_usage_json TEXT NOT NULL DEFAULT '{}',
    group_id TEXT,
    sub_agents_json TEXT NOT NULL DEFAULT '[]',
    plan_path TEXT,
    plan_status TEXT NOT NULL DEFAULT 'inactive',
    plan_task TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
);
```

运行态字段不持久化：`status` 启动后统一恢复为 `idle`，`agent`、`thread`、`partial` 和 `restore_history` 由启动流程重新构建。

### 4.2 `messages`

每条消息单独一行，避免追加一条消息时重写整个会话：

```sql
CREATE TABLE messages (
    session_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, message_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_messages_session_timestamp
    ON messages(session_id, timestamp, message_id);
```

`metadata_json` 保存当前消息字典中除 `id`、`role`、`content`、`ts` 之外的字段，例如 `image_ids`、`kind`、`candidates`。消息正文保持 UTF-8 字符串；不保存附件数据本身。

### 4.3 `groups`

```sql
CREATE TABLE groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    position INTEGER NOT NULL
);
```

`groups` 由 `group_id` 与 `sessions` 关联。删除分组前将关联会话的 `group_id` 置空，避免会话被删除。

### 4.4 `app_state`

```sql
CREATE TABLE app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

使用键 `active_session_id` 保存 active session。读取时如果该 ID 不存在，回退到最近更新的有效会话。

### 4.5 Schema 版本

数据库初始化时创建单独的 schema 元数据：

```sql
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

当前版本写入 `1`。初始化必须幂等；未来 schema 变化通过受控版本升级完成，不依赖文件内容猜测。

## 5. 数据生命周期

### 创建

`create_session()` 先创建内存对象，不立即插入数据库。这样保持现有“空会话不进入历史列表”的行为。

### 首条有效用户消息

`add_message()` 检测到会话包含有效用户消息时，在同一事务中：

1. `INSERT ... ON CONFLICT DO UPDATE` 写入会话元数据。
2. 插入当前消息。
3. 更新 `active_session_id`（如果该会话已成为当前会话）。

### assistant / system / error 消息

消息追加使用单条 `INSERT`，同时更新会话的 `updated_at`、`msg_seq` 和相关状态字段。流式期间的 `partial` 仍只存在内存中；终态消息完成后才落库。

### 分组

创建、修改、删除分组使用数据库事务。移动会话失败时回滚内存字段；删除分组只清除关联会话的 `group_id`，不会删除会话。

### 删除

事务内删除 `sessions` 记录，依赖 `ON DELETE CASCADE` 删除其 `messages`。数据库提交成功后，再删除对应的 `model_responses_session_<sid>.txt`，避免日志先删除而数据库删除失败造成不可恢复数据丢失。

如果删除的是 active session，事务内选择剩余会话中 `updated_at` 最大者作为新的 active session。

### 重启恢复

`AgentManager` 初始化时：

1. 创建数据库目录和 schema（不存在时）。
2. 读取有效 `sessions`，按 `updated_at` 排序。
3. 批量读取每个会话的 `messages`，按 `message_id` 排序。
4. 将运行状态归一为 `idle`。
5. 校验 `active_session_id`，失效时回退到最近会话。
6. `make_agent()` 时从恢复的消息构建 LLM history。

不读取旧 `sessions.json`，不执行旧数据导入。

## 6. 数据库访问封装

新增一个只负责持久化的 `SessionStore`，避免 `AgentManager` 直接散落 SQL：

- `initialize()`：创建连接、设置 pragma、初始化 schema。
- `load_sessions()`：加载会话、消息和 active session。
- `upsert_session()`：保存会话元数据。
- `append_message()`：追加一条消息并更新会话时间。
- `delete_session()`：删除会话及消息。
- `load_groups()`、`save_group()`、`delete_group()`：分组 CRUD。
- `set_active_session()`：更新 active session。
- `close()`：关闭连接。

业务层继续使用现有 `Session` dataclass 和 `AgentManager` API，前端无需知道数据库实现。

## 7. 并发、事务和故障处理

- 使用 `sqlite3` 参数绑定，禁止拼接用户输入到 SQL。
- 开启 `PRAGMA foreign_keys = ON`。
- 开启 `PRAGMA journal_mode = WAL`，降低读取与写入互相阻塞。
- 设置有限的 `busy_timeout`，避免短暂锁竞争立即失败。
- 数据库写入使用进程内锁；事务边界覆盖会话和消息的相关更新。
- 关键持久化失败必须向现有 bridge 调用方返回错误，并保持内存状态可回滚；不能像旧 response log 写入一样静默吞掉数据库异常。
- 临时数据库文件、WAL 文件和 SHM 文件都放在 `workspace/sessions`，不应被提交到 Git。
- 启动时如果数据库损坏，报告明确错误，不自动覆盖数据库，也不尝试读取旧 `sessions.json`。

## 8. 原始日志兼容

`LiteLLMSession._write_model_response_log()` 保持现有行为：

- 桌面端继续写入每个 session 专属的 `model_responses_session_<sid>.txt`。
- CLI fallback 继续使用 `model_responses_<pid>.txt`。
- `/continue` 继续扫描 `model_responses_*.txt`。
- 桌面端的 SQLite 会话不重复加入 `/continue` 的外部日志列表。

SQLite 不取代原始日志，也不改变日志格式。

## 9. 验证要求

必须覆盖以下可观察行为：

1. 新建空会话不会创建会话记录。
2. 首条有效用户消息会创建 SQLite 会话和消息记录。
3. assistant、system、error 消息会按顺序持久化。
4. 消息追加不会影响其他会话。
5. manager 重启后会话、消息、标题、时间戳和 active session 可恢复。
6. 重启时所有恢复会话为 `idle`，不恢复 `partial` 或运行线程。
7. 删除会话会级联删除消息，并删除对应专属日志。
8. 删除 active session 后选择最近剩余会话。
9. 分组创建、移动、删除及重启恢复有效。
10. 数据库写入失败时内存状态回滚或返回明确错误。
11. 旧 `sessions.json` 不会被读取或修改。
12. `/continue` 仍能读取保留的 `model_responses_*.txt`。
13. 前端现有 `/session/list`、消息轮询和会话切换行为保持不变。

## 10. 切换后的文件关系

```text
workspace/sessions/
├── sessions.sqlite3                         # 新主存储
├── model_responses_session_sess-*.txt       # 桌面端原始 LLM 日志
├── model_responses_<pid>.txt                # CLI 原始日志
├── sessions.json                            # 旧文件，保留但不再读取
├── groups.json                              # 旧分组文件，不再作为主存储
└── session_names.json                       # 旧 /continue 名称侧车，可继续兼容
```

本实现不删除旧 JSON 文件，也不保证旧 JSON 与 SQLite 后续同步；SQLite 切换后是唯一的桌面会话主存储。
