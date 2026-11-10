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
        ├── core/hooks.py      ←── HookSystem: 8 事件钩子 + 插件发现
        ├── core/types.py      ←── StepOutcome, TurnResult
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

### LLM 后端选择

```
AgentConfig.llm_backends:
  provider: anthropic 等    → LiteLLMSession  (通过 litellm 统一调用)
  failover_backends: [...]  → AutoFailoverSession (多后端链路容错)
```
