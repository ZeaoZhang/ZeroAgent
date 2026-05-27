# ZeroAgent - 干净可复用的自主 Agent 框架

基于 GenericAgent 架构重构，保持核心 generator-based agent loop 模式，
按模块化、可测试、可扩展的原则重写。

## Features

- **Generator-based Agent Loop** — LLM 调用和工具执行是 Python generator，yield 状态字符串，return 结果
- **Bilingual** — 系统提示词/工具描述/handler 消息全部中英双语自适应
- **Multi-backend Failover** — litellm Router fallback chains + cooldowns
- **Event Hook System** — 8 个标准钩子点，支持插件自动发现
- **Text Tool-call Fallback** — 对不支持原生 tool_calling 的模型，从文本 XML/JSON 解析工具调用
- **Sub-message Compression** — compress_history_tags 标签级压缩 + 消息级裁剪混合策略
- **Content Extraction** — file_write/web_execute_js 支持从 LLM 响应代码块/标签回退提取内容
- **Inline Eval** — code_run 支持进程内 exec() 用于自省/调试
- **Cache Control** — 对 Anthropic 模型自动标记 ephemeral cache
- **Path Traversal Protection** — expand_file_refs 防止 ../../ 攻击
- **Runtime Model Switching** — 切换后端时保留对话历史，schema 语言自动适配

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

## 实现路线

| 步骤 | 模块 | 状态 |
|------|------|------|
| 1 | core/exceptions.py + core/types.py | done |
| 2 | utils/text.py + utils/files.py | done |
| 3 | core/config.py | done |
| 4 | tools/registry.py | done |
| 5 | tools/builtin/code.py | done |
| 6 | tools/builtin/file.py + memory.py + user.py | done |
| 7 | llm/base.py | done |
| 8 | llm/sessions.py | done |
| 9 | llm/native.py | removed（litellm 完全覆盖原生 API + cache_control） |
| 10 | llm/protocol.py | removed（litellm 统一处理协议翻译） |
| 11 | llm/mixin.py + factory.py | done（mixin 已移除，改用 litellm Router） |
| 12 | core/handler.py | done |
| 13 | core/loop.py | done |
| 14 | core/agent.py | done |
| 15 | memory/manager.py | done |
| 16 | runners/cli.py | done |
| 17 | tests/ | done |
| 18 | tools/builtin/web.py | done |
| 19 | core/hooks.py | done |
| 20 | LICENSE | done |

## Gap Fixes (vs GenericAgent)

已修复的 GenericAgent 差距：

| 功能 | 状态 |
|------|------|
| _parse_text_tool_calls — 文本工具调用回退 | done |
| _compress_history_tags — 标签级历史压缩 | done |
| _try_parse_tool_args — 粘连 JSON 解析 | done |
| _fix_messages — Anthropic 消息格式修复 | done |
| _stamp_cache_markers — 缓存控制标记 | done |
| _write_llm_log — LLM 调用日志 | done |
| file_write content 回退提取 | done |
| web_execute_js script 回退提取 | done |
| code_run inline_eval 模式 | done |
| bad_json 工具处理 | done |
| file_read SOP 读取提示 | done |
| HookSystem — 8 事件钩子 | done |
| MixinSession 属性广播 | done |
| MixinSession 部分故障检测 | done |
| extra_sys_prompt 支持 | done |
| 运行时工具 schema 语言切换 | done |
| /session 斜杠命令 | done |
| /resume 斜杠命令 | done |
| expand_file_refs 路径遍历保护 | done |
| service_tier / verify 配置字段 | done |
| LICENSE | done |
