# ZeroAgent

ZeroAgent 是一个基于 `litellm` 的可复用自主 Agent 框架，采用 generator-based agent loop 的核心交互模式，同时把 LLM 后端、工具系统、记忆、插件、CLI 和反射式运行拆成可测试的模块。

## 特性

- **Generator-based Agent Loop**: LLM 调用和工具执行以 Python generator 流式产出状态，最终返回结构化结果。
- **多后端与容错**: 支持多个 LLM backend、运行时切换、failover 和 spring-back。
- **工具注册表**: `ToolRegistry.with_builtins()` 默认注册 9 个核心原子工具，并支持自定义工具注册。
- **双语自适应**: 系统提示、工具描述和 handler 消息支持中文、英文和自动语言选择。
- **文本工具调用回退**: 对不支持原生 tool calling 的模型，可从 XML/JSON 文本中解析工具调用。
- **记忆与压缩**: 分层记忆管理、历史标签压缩、消息裁剪和 OCR/视觉记忆扩展。
- **Hook 与插件**: 8 个标准事件钩子，包含 Langfuse tracing 插件示例。
- **Reflect 模式**: 支持反射式唤醒、目标模式、定时任务和 agent team worker。
- **多入口**: 提供 REPL、一次性任务、文件 I/O 批处理、统一 Web2/Tauri 前端和桌面 launcher。
- **工程化测试**: pytest 测试覆盖 core、LLM、tools、memory、plugins 和 reflect 模块。

## 安装

```bash
git clone git@github.com:zhangzeao/ZeroAgent.git
cd ZeroAgent

python -m venv .venv
source .venv/bin/activate
pip install -e .
```

运行环境:

- Python `>=3.10,<3.14`
- `litellm>=1.50`
- `requests>=2.28`

可选功能:

```bash
# Web2 / Tauri UI
pip install -e ".[ui]"

# Browser control runtime (web_scan / web_execute_js)
pip install -e ".[browser]"
# Then load/connect the bundled browser extension from:
# zero_agent/assets/tmwd_cdp_bridge

# OCR 与图像记忆
pip install -e ".[memory]"

# 记忆统计绘图
pip install -e ".[plot]"

# 安装全部可选功能
pip install -e ".[all-extras]"
```

## 工具清单

`ToolRegistry.with_builtins()` 默认只注册以下 9 个核心工具:

- `code_run`
- `file_read`
- `file_write`
- `file_patch`
- `web_scan`
- `web_execute_js`
- `update_working_checkpoint`
- `start_long_term_update`
- `ask_user`

`search_web`、`vision`、`memory_plot`、`send_im` 是 ZeroAgent 的可选/实验性扩展模块，不会默认注册进 `with_builtins()`。

## 配置

推荐先运行配置向导:

```bash
zero-agent-configure
```

也可以使用环境变量:

```bash
export ZA_LLM_PROVIDER=anthropic
export ZA_LLM_API_KEY=sk-ant-xxx
export ZA_LLM_API_BASE=https://api.anthropic.com
export ZA_LLM_MODEL=claude-sonnet-4-6
export ZA_MAX_TURNS=80
export ZA_WORKSPACE_DIR=./workspace
export ZA_LANG=auto
```

或创建项目根目录的 YAML 配置文件 `config.yaml`:

```yaml
default_backend: default
max_turns: 80
workspace_dir: ./workspace
memory_dir: ./memory
sessions_dir: ./workspace/sessions
language: auto

llm_backends:
  default:
    provider: anthropic
    api_key: sk-ant-xxx
    api_base: https://api.anthropic.com
    model: claude-sonnet-4-6
```

多后端示例:

```yaml
default_backend: claude
failover_backends: [openai]

llm_backends:
  claude:
    provider: anthropic
    api_key: sk-ant-xxx
    api_base: https://api.anthropic.com
    model: claude-sonnet-4-6
  openai:
    provider: openai
    api_key: sk-xxx
    api_base: https://api.openai.com/v1
    model: gpt-4o
```

> 不要把真实 API Key 提交到 Git。`config.yaml` 已加入 `.gitignore`，默认作为本机项目配置使用。

## 快速开始

交互式 REPL:

```bash
zero-agent
```

一次性任务:

```bash
zero-agent -i "列出当前目录下的文件"
```

指定模型、工作目录和最大轮次:

```bash
zero-agent \
  -m claude-sonnet-4-6 \
  --workspace ./workspace \
  --max-turns 50 \
  -i "帮我分析这个项目的结构"
```

文件 I/O 批处理模式会读取 `IODIR/input.md`，并写入 `IODIR/output.md` 和 `IODIR/log.jsonl`:

```bash
mkdir -p tasks/demo
printf "总结 README.md 的内容" > tasks/demo/input.md
zero-agent --task tasks/demo
```

Reflect 模式:

```bash
zero-agent --reflect path/to/reflect_module.py
```

Web UI:

```bash
pip install -e ".[ui]"
python -m zero_agent.frontends.desktop_bridge
```

This starts the same Web2 frontend used by the Tauri desktop app and opens it
in your browser at `http://127.0.0.1:14168/`.

pywebview 桌面窗口:

```bash
zero-agent-launcher
```

Tauri 桌面 Web bridge:

```bash
python -m zero_agent.frontends.desktop_bridge
```

The Web UI and Tauri desktop app use the same static frontend under
`zero_agent/frontends/desktop/static`.

## 打包桌面 App

桌面 App 使用 Tauri 2 打包，前端静态资源位于
`zero_agent/frontends/desktop/static`，后端 bridge 仍使用当前 Python 环境运行。
打包前需要准备:

- Python 环境已安装 UI 依赖：`pip install -e ".[ui]"`
- 已安装 Node.js / npm
- 已安装 Rust toolchain 和 Tauri 对应平台依赖

macOS 上推荐使用仓库脚本完成构建、安装和启动验证:

```bash
./scripts/restart.sh
```

该脚本会停止已运行的 ZeroAgent desktop/bridge 进程，重新安装 editable
Python 包，执行 Tauri release build，安装最新 DMG 到
`/Applications/ZeroAgent.app`，然后启动并检查
`http://127.0.0.1:14168/status`。

只打包不安装/不启动:

```bash
./scripts/restart.sh --skip-install --no-start
```

手动打包:

```bash
pip install -e ".[ui]"
cd zero_agent/frontends/desktop
npm ci
npm run tauri -- build
```

构建产物位置:

- macOS DMG: `zero_agent/frontends/desktop/src-tauri/target/release/bundle/dmg/`
- Windows NSIS installer: `zero_agent/frontends/desktop/src-tauri/target/release/bundle/nsis/`

首次启动时，桌面 App 会读取 `~/.zero_agent_desktop_settings.json`
中的 `python_path` 和 `project_dir` 来启动 Python bridge。`scripts/restart.sh`
会自动写入该文件；如果手动打包后看到 Setup 窗口，按提示选择当前项目目录和
Python 解释器即可。

## REPL 命令

| 命令 | 说明 |
| --- | --- |
| `/help` | 显示帮助 |
| `/tools` | 列出可用工具 |
| `/model` | 显示当前模型 |
| `/backends` | 列出可用 LLM 后端 |
| `/switch <name>` | 切换到指定后端 |
| `/session.<k>=<v>` | 动态设置当前 session 属性 |
| `/resume` | 生成恢复历史会话的提示 |
| `/continue` | 保存当前会话快照 |
| `/new` | 开始新会话 |
| `/exit` | 退出 |

## 项目结构

```text
zero_agent/
  core/        # Agent 编排、配置、handler、loop、hooks、异常和类型
  llm/         # LiteLLM session、failover、factory、SSE parser、格式转换
  tools/       # 工具注册表；默认核心工具和可选扩展工具模块
  memory/      # 记忆管理与 L4 会话压缩
  reflect/     # 反射式运行、目标模式、调度、subagent/team worker
  frontends/   # Web2/Tauri desktop bridge、静态前端和 launcher
  runners/     # CLI 入口
  utils/       # 配置向导、文件、文本、keychain、统计工具和 agent 辅助脚本
  plugins/     # Langfuse tracing 等插件
tests/         # pytest 测试
docs/          # quickstart、architecture、reflect 文档
```

## 文档

- [Quick Start](docs/quickstart.md)
- [Architecture](docs/architecture.md)
- [Reflect](docs/reflect.md)
- [Contributing](CONTRIBUTING.md)

## 开发

```bash
pip install -e ".[all-extras]"
pip install pytest
pytest tests/ -v
```

本仓库包含 GitHub Actions workflow，会在 push 和 pull request 时使用 Python 3.10、3.11、3.12 运行 pytest。

编码约定:

- Python 公共 API 使用 Google 风格 docstring（Args/Returns/Raises/Yields 按需出现）。
- 所有公开函数参数和返回值使用类型标注。
- 优先使用 dataclass 和依赖注入，避免模块级全局状态。
- 异常遵循 `ZeroAgentError -> ConfigError / LLMError / ToolError` 层次。
- 新功能需要补充或更新 `tests/` 中的测试。

## Langfuse tracing

设置以下环境变量后，Langfuse 插件可读取配置并注册 tracing hooks:

```bash
export LANGFUSE_PUBLIC_KEY=pk-xxx
export LANGFUSE_SECRET_KEY=sk-xxx
export LANGFUSE_HOST=https://cloud.langfuse.com
```

## License

MIT. See [LICENSE](LICENSE).
