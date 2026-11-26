# ZeroAgent 架构

## 总体架构

```
runners/cli.py          ←── 入口: REPL / oneshot / reflect
        │
        ▼
core/agent.py           ←── ZeroAgent: 组装所有组件
        │
        ├── core/config.py     ←── AgentConfig: YAML/ENV/程序化
        ├── core/loop.py       ←── AgentLoop: generator-based 事件循环
        ├── core/handler.py    ←── BaseHandler: 工具执行 + 计划模式 + 轮次管理
        ├── core/types.py      ←── StepOutcome, TurnResult, PendingTaskState
        ├── core/checkpoint.py ←── 热重启检查点（history / 契约 / 证据 / 预算）
        ├── core/hot_restart.py←── 核心代码签名、重启前校验、exec
        ├── core/exceptions.py ←── ZeroAgentError 层次
        │
        ├── llm/               ←── LLM 后端
        │   ├── sessions.py    ←── LiteLLMSession (主力)
        │   ├── failover.py    ←── AutoFailoverSession (多后端容错)
        │   ├── factory.py     ←── LLMFactory (统一创建入口)
        │   ├── base.py        ←── MockResponse 等基类
        │   ├── sse_parsers.py ←── SSE 流解析器
        │   └── converters.py  ←── 消息格式转换器
        │
        ├── tools/             ←── 工具系统
        │   ├── registry.py    ←── ToolRegistry + ToolDefinition
        │   └── builtin/       ←── code, file, memory, user, web
        │
        ├── extensions/        ←── 扩展运行时（插件 / skill / MCP）
        │   ├── runtime.py     ←── 发现、加载、三类事件、注册归属
        │   ├── context.py     ←── 面向插件的稳定 API
        │   ├── events.py      ←── 通知 / 变换 / 门控契约
        │   ├── sandbox.py     ←── code_run 的可选进程沙箱（sandbox-exec / bwrap）
        │   └── builtin/       ←── permission_gate, skills_index, mcp_client,
        │                           path_policy, shell_policy
        │
        ├── memory/            ←── 记忆管理
        │   └── manager.py     ←── L0-L4 分层记忆
        │
        ├── reflect/           ←── 反射式唤醒
        │   ├── runner.py      ←── ReflectRunner (循环调度)
        │   ├── autonomous.py  ←── 空闲检测
        │   ├── goal_mode.py   ←── 目标驱动
        │   ├── scheduler.py   ←── 定时任务
        │   └── agent_team_worker.py ←── BBS 协作
        │
        ├── utils/             ←── 工具函数
        │   ├── text.py        ←── 文本处理
        │   ├── files.py       ←── 文件操作
        │   ├── keychain.py    ←── 凭证存储
        │   └── memory_stats.py ←── 记忆访问统计
        │
        └── plugins/           ←── 可插拔扩展
            └── langfuse_tracing.py ←── LangFuse 追踪
```

## 核心设计

### Generator-based Agent Loop

```
AgentLoop.run() → Generator[yield str, send None, return TurnResult]
  │
  ├── _build_system_prompt()
  ├── for turn in range(max_turns):
  │     ├── yield "turn info"
  │     ├── _build_anchor_prompt()
  │     ├── llm.chat() → Generator → MockResponse
  │     ├── handler.dispatch() → StepOutcome
  │     └── if should_exit → return TurnResult
  └── return TurnResult(max_turns_exceeded=True)
```

### 工具执行

```
LLM 返回 tool_calls → ToolRegistry.dispatch()
  │
  ├── 原生 tool_calling (95% 模型)
  └── 文本回退解析 (_parse_text_tool_calls)
       ├── XML <tool_use> 格式
       └── JSON 数组格式
```

### 扩展体系

```
~/.zeroagent/
  ├── extensions/<name>.py 或 <name>/__init__.py   → 插件，导出 setup(ctx)
  ├── skills/<name>/SKILL.md                       → 只读技能，正文按需
  └── mcp.json                                     → MCP server（单个文件）
```

加载时机：`ZeroAgent.__init__` 中，内置工具注册之后、handler 创建之前。
插件按文件名字典序加载，单个失败只跳过自身。内置能力扩展同样走这套运行时，
但由代码显式安装；`browser_tools` 是其中之一，由 `extensions.browser` 控制，
把 `web_*` 工具从默认工具面里拆出去。

加载之后并不固化：`AgentLoop` 在每个 turn 边界调用 `ZeroAgent.reload_workflow()`，
按内容签名同步 config / 插件（含目录插件包内任意 `*.py`）/ `mcp.json` /
`extensions.{enabled,home,browser}`。核心代码（`zero_agent/**/*.py`）变化时先做
语法与子进程 import 校验，再 `os.execv` 热重启；有运行中任务时先写检查点，新进程
从检查点恢复 history、working memory、task contract 与证据账本后续跑（预算 10 分钟
5 次），空闲时直接重启、不恢复对话。
权限门控的 `permissions.mode` 决定是否执行这一层：`full`（默认）不执行任何
`on_tool_call` 门控，`ask` 才启用审批与拦截。强制隔离只有 `extensions.sandbox`。

三类扩展点（语义不同，不可混用）：

| 类别 | 事件 | 失败时 |
| --- | --- | --- |
| 通知 | `agent_start / turn_start / llm_before / llm_after / tool_before / tool_after / turn_end / agent_end` | 记录后继续 |
| 变换 | `llm_request / tool_result / prompt_block` | 退回原值 |
| 门控 | `tool_call` | **fail-closed：判定失败即拒绝** |

注册归属：插件注册的工具记录 owner，卸载或 registry 重建时可整体撤销与重放。
详见 [`docs/extension-design.md`](extension-design.md)。

### LLM 后端选择

```
AgentConfig.llm_backends:
  provider: anthropic 等    → LiteLLMSession  (通过 litellm 统一调用)
  failover_backends: [...]  → AutoFailoverSession (多后端链路容错)
```
