# ZeroAgent

基于 litellm 的干净可复用自主 Agent 框架。从 GenericAgent 重构而来，保持核心 generator-based agent loop 模式，按模块化、可测试、可扩展的原则重写。

## Features

- **Generator-based Agent Loop** — LLM 调用和工具执行是 Python generator，yield 状态字符串，return 结果
- **Bilingual** — 系统提示词/工具描述/handler 消息全部中英双语自适应
- **Multi-backend Failover** — MixinSession 多后端容错回退 + spring-back，支持部分故障检测
- **Event Hook System** — 8 个标准钩子点，支持插件自动发现
- **Text Tool-call Fallback** — 对不支持原生 tool_calling 的模型，从文本 XML/JSON 解析工具调用
- **Sub-message Compression** — compress_history_tags 标签级压缩 + 消息级裁剪混合策略
- **Content Extraction** — file_write/web_execute_js 支持从 LLM 响应代码块/标签回退提取内容
- **Inline Eval** — code_run 支持进程内 exec() 用于自省/调试
- **Cache Control** — 对 Anthropic 模型自动标记 ephemeral cache
- **Plan Mode** — 基于 plan.md 清单的任务计划追踪，自动完成检测
- **Resource Tracking** — LLM 调用 token 使用和缓存命中率统计
- **Keychain** — XOR 加密凭据存储
- **Path Traversal Protection** — expand_file_refs 防止 ../../ 攻击
- **Runtime Model Switching** — 切换后端时保留对话历史，schema 语言自动适配

## 安装

```bash
pip install -e .
```

依赖：
- Python >= 3.10, < 3.14
- litellm >= 1.50
- requests >= 2.28

## 快速开始

```bash
# 交互式 REPL
zero-agent

# 单任务执行
zero-agent -i "你的任务描述"

# 指定最大轮次
zero-agent -i "任务" --max-turns 50
```

## 编码规范

- Google Python 风格：中文 docstring（Arg/Returns/Raises）
- 所有函数参数和返回值必须有类型标注
- dataclass 优先于普通 class
- 依赖注入：所有组件通过构造函数接收依赖，零模块级全局状态
- 异常分层：ZeroAgentError → ConfigError / LLMError / ToolError

## 项目结构

```
zero_agent/
  core/        — 编排层：exceptions, types, config, handler, loop, agent, hooks
  llm/         — LLM 后端：base, sessions, mixin, factory
  tools/       — 工具系统：registry, builtin/code, file, memory, user, web
  memory/      — 记忆管理：manager
  runners/     — 运行器：cli
  utils/       — 工具函数：text, files
tests/         — 94 个测试
```

## License

MIT
