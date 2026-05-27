# Contributing to ZeroAgent

感谢贡献！请遵循以下流程和规范。

## 开发环境搭建

```bash
git clone <repo-url> && cd ZeroAgent
pip install -e ".[all-extras]"
pip install pytest
```

运行测试确认环境正常:

```bash
pytest tests/ -v
```

## PR 流程

1. Fork 仓库并创建 feature 分支
2. 编写代码，遵循编码规范（见 CLAUDE.md）
3. 为新功能添加测试（`tests/` 目录下对应文件）
4. 运行 `pytest tests/ -v` 确保全部通过
5. 提交 PR，描述清楚做了什么和为什么

## 编码规范

见 [CLAUDE.md](CLAUDE.md) 中的完整规范:

- **注释**: Google Python 风格，中文 docstring（Args/Returns/Raises/Yields）
- **类型标注**: 所有函数参数和返回值
- **类设计**: dataclass 优先，依赖注入优先
- **异常**: 使用 ZeroAgentError → ConfigError / LLMError / ToolError 层次
- **命名**: 函数 snake_case，类 PascalCase，私有 _prefix，常量 UPPER_SNAKE_CASE

## 测试

- 每个模块对应 `tests/` 下的一个测试文件
- 使用 pytest + fixtures
- Mock 外部依赖（LLM API、文件系统等）
- 目标: >80% 行覆盖率

## 项目结构

```
zero_agent/
  core/        — 编排层: exceptions, types, config, handler, loop, agent, hooks
  llm/         — LLM 后端: base, sessions, failover, factory, converters, sse_parsers
  tools/       — 工具系统: registry, builtin/(code, file, memory, user, web, im, search, vision, memory_plot)
  memory/      — 记忆管理: manager, ocr_utils, vision_api, compress_session
  reflect/     — 反射式唤醒: runner, autonomous, goal_mode, scheduler, agent_team_worker
  frontends/   — 用户界面: stapp (Streamlit)
  runners/     — 运行器: cli
  utils/       — 工具函数: configure, files, text, memory_stats, keychain
  plugins/     — 可插拔扩展: langfuse_tracing
tests/         — 测试
```
