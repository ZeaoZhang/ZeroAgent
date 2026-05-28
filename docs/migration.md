# 从 GenericAgent 迁移到 ZeroAgent

ZeroAgent 是对 GenericAgent 的模块化重构。本文档帮助你从 GenericAgent 迁移到 ZeroAgent。

## 快速对比

| 维度 | GenericAgent | ZeroAgent |
|------|-------------|-----------|
| 入口 | `agentmain.py` | `zero_agent/core/agent.py` |
| CLI | `ga gui/tui/cli/...` | `zero-agent` + `-i`/`-r`/`--task` |
| 配置 | `mykey.py` (Python 模块) | YAML 文件或环境变量 |
| LLM 会话 | 手动 `NativeClaudeSession` / `MixinSession` | litellm 通过 `LLMFactory` |
| 工具定义 | `TOOLS_SCHEMA` 静态 JSON | `ToolRegistry` 动态生成 |
| 记忆 | 基于文件，手动初始化 | `MemoryManager` 类 |
| 钩子 | 模块级 `_hook()` | `HookSystem` 实例 |
| 机器人集成 | `GeneraticAgent()` 直接实例 | `AgentRunner(ZeroAgent())` 适配器 |

## 1. 入口和运行

**GenericAgent:**
```python
from agentmain import agent_runner_loop

gen = agent_runner_loop(
    task="分析这份代码",
    agent_config=my_config,
    client=native_session,
    handler=handler,
    tools_schema=TOOLS_SCHEMA,
)
for chunk in gen:
    print(chunk, end="")
```

**ZeroAgent:**
```python
from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig

config = AgentConfig.from_yaml("config.yaml")
za = ZeroAgent(config=config)
for chunk in za.run("分析这份代码"):
    print(chunk, end="")
```

## 2. 配置迁移

**GenericAgent (`mykey.py`):**
```python
mykeys = [{
    "name": "default",
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "api_key": "sk-ant-xxx",
    "api_base": "https://api.anthropic.com",
    "context_win": 28000,
    "max_tokens": 8192,
    "temperature": 1.0,
}]
```

**ZeroAgent (`config.yaml`):**
```yaml
llm_backends:
  default:
    provider: anthropic
    model: claude-sonnet-4-6
    api_key: sk-ant-xxx
    api_base: https://api.anthropic.com
    context_window: 28000
    max_tokens: 8192
    temperature: 1.0
```

也支持环境变量：`ZA_LLM_PROVIDER`, `ZA_LLM_API_KEY`, `ZA_LLM_API_BASE`, `ZA_LLM_MODEL`

## 3. 工具系统迁移

**GenericAgent:** 工具是 `GenericAgentHandler` 上的 `do_<name>` 方法。
```python
class MyHandler(GenericAgentHandler):
    def do_my_tool(self, args, response, handler, index=0, tool_num=1):
        yield {"data": "result", "next_prompt": ""}
```

**ZeroAgent:** 工具注册到 `ToolRegistry`。
```python
from zero_agent.tools.registry import ToolRegistry, ToolDefinition

registry = ToolRegistry()
registry.register(ToolDefinition(
    name="my_tool",
    description="我的自定义工具",
    parameters={"type": "object", "properties": {...}},
    handler=my_handler_function,
))
# 如果有自定义工具，传递给 ZeroAgent
za = ZeroAgent(config=config, registry=registry)
```

## 4. LLM 会话迁移

**GenericAgent — 直接创建 session:**
```python
from llmcore import NativeClaudeSession, MixinSession

session = NativeClaudeSession(
    api_key="sk-ant-xxx",
    model="claude-sonnet-4-6",
    ...
)
client = MixinSession(sessions=[session])
```

**ZeroAgent — litellm + AutoFailoverSession:**
```yaml
llm_backends:
  primary:
    provider: anthropic
    model: claude-sonnet-4-6
    api_key: sk-ant-xxx
  backup:
    provider: openai
    model: gpt-5
    api_key: sk-xxx

failover_backends: ["backup"]
```

ZeroAgent 使用 litellm 统一处理所有 LLM 提供商的 API 差异。要复制 GenericAgent 的 CC 中继行为，使用新的 `extra_headers` 字段：
```yaml
llm_backends:
  default:
    provider: anthropic
    model: claude-sonnet-4-6
    extra_headers:
      anthropic-beta: "claude-code-20250219,prompt-caching-scope-2026-01-05"
      x-app: "cli"
      user-agent: "zero-agent/1.0"
```

## 5. 机器人集成

**GenericAgent:**
```python
from agentmain import GeneraticAgent
ga = GeneraticAgent(config=...)
def handle_message(text):
    q = ga.put_task(text)
    for msg in iter(q.get, None):
        yield msg
```

**ZeroAgent:**
```python
from zero_agent.adapters.agent_runner import AgentRunner
from zero_agent.core.agent import ZeroAgent

za = ZeroAgent(config=config)
runner = AgentRunner(za)
def handle_message(text):
    q = runner.put_task(text)
    for msg in iter(q.get, None):
        yield msg
```

`AgentRunner` 提供兼容的 `put_task()` → `queue.Queue` 接口，与 GenericAgent 的机器人 mode 完全兼容。

## 6. 运行时会话控制

两个项目都支持 `/session.*` 斜杠命令：

**GenericAgent:**
```
/session.reasoning_effort=high
/session.temperature=0.3
```

**ZeroAgent（功能等价）:**
```
/session.reasoning_effort=high
/session.temperature=0.3
/session.max_tokens=16384
```

ZeroAgent 的实现位于 `runners/cli.py` 第 345-369 行，支持嵌套属性。

## 7. 已被移除（替代方案）

| GenericAgent 特性 | ZeroAgent 替代方案 |
|---|---|
| `NativeClaudeSession` | `LiteLLMSession`（通过 litellm 调用所有提供商） |
| `MixinSession` | `AutoFailoverSession`（显式健康检查 + 历史迁移） |
| `tools_schema.json` | `ToolRegistry.generate_openai_schema()` 动态生成 |
| `agentmain.py` 全部解析 | YAML `AgentConfig` |
| 模块级 `_ga_instance` | 依赖注入（构造函数传递） |
| `mykey_template.py` 交互式设置 | `config.yaml` + 环境变量 |

## 8. 常见问题

### Q: 我的自定义工具在 ZeroAgent 中如何工作？
在 `ToolRegistry` 中注册，并将注册中心传递给 `ZeroAgent(registry=my_registry)`。BaseHandler 首先检查其 `do_<name>` 方法，然后回退到注册中心的 handler 函数。

### Q: 如何回退到原生 Anthropic API？
ZeroAgent 通过 litellm 路由所有调用。对于原生 API 访问，使用带适当提供商设置的 `LiteLLMSession`。不需要像 GenericAgent 那样的单独 `NativeClaudeSession`。

### Q: SOP 文件在哪里？
ZeroAgent 将 SOP 文件存储在其包目录（`zero_agent/memory/sops/`）中，并在首次运行时复制到用户的内存目录。这与 GenericAgent 的 `memory/*.md` 文件集相同。

### Q: 机器人命令（/btw、/review 等）仍然可用吗？
是的。全部 9 个命令（/help、/stop、/status、/llm、/restore、/continue、/new、/btw、/review）均已完整实现。ZeroAgent 的 `AgentBotMixin` 在功能上等价于 GenericAgent 的 `AgentChatMixin`。
