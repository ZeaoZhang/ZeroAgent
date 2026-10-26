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
        ├── context/           ←── local_v1 上下文运行时
        │   ├── types.py / uri.py ←── 对象、身份和逻辑地址
        │   ├── local_store.py    ←── SQLite manifest、修订、CAS 与访问统计
        │   ├── service.py         ←── 授权读取、列表、检索和来源查询
        │   ├── sources.py         ←── 持久来源回执与工具 claim policy
        │   ├── admin.py           ←── 项目、授权和 bot 身份管理
        │   ├── resources.py       ←── 显式文本资料导入
        │   └── assembly.py        ←── 有字节预算的提示上下文
        ├── memory/            ←── 记忆受控更新
        │   └── service.py     ←── 来源检查、偏好冲突和生命周期
        ├── skills/            ←── 固定版本的内置技能
        │   ├── manifest.py    ←── 来源 hash、SOP 要求和入口清单
        │   └── runtime.py     ←── 私有依赖快照与既有执行器委托
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

### local_v1 上下文运行时

`local_v1` 是唯一支持的上下文模式，入口在每个任务边界提供 `RuntimeContext`；旧
`context.mode: legacy` 配置会被拒绝。`ContextService` 使用逻辑 URI、当前身份和授权读取个人/项目对象，
`MemoryService` 通过持久来源回执、偏好注册表和 revision CAS 处理长期写入。正文修订
不可变地存于 context root，SQLite manifest 是已提交版本的权威指针。固定规则、已确认
偏好、当前项目概览、SOP 触发目录和工作便签由 `ContextAssembler` 按 UTF-8 字节预算注入。

项目身份只通过经入口验证的 `RuntimeContext` 和 `ContextAdminService` 绑定；workspace link 仅选择任务工作目录，不注入文件型 project memory。对象访问计数按 principal、scope、URI 和 revision 保存，读取管理视图时再通过当前 grants 过滤。
`ContextService.list_access_stats()` 返回已按当前身份和授权重新过滤的 `ContextAccessStat` 记录；撤权后不返回对应历史计数。

管理命令、项目授权和 bot 身份链接共用 `ContextAdminService`。迁移清单显式绑定源
preimage；导入不执行脚本、不删除源文件。完整配置、运维命令、备份和回退边界见
[上下文与记忆指南](context-memory-skills.md)。

技能清单为发行包内每项 SOP 固定来源 hash、触发条件、分类和章节级要求映射。任务路由
与动作门禁要求已触发的 SOP 在写入、执行等对应操作前加载精确版本；只有清单批准的脚本入口会委托现有执行器。host
capability 是调用授权，不会为任意宿主 Python/Shell 提供操作系统级沙箱。

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
