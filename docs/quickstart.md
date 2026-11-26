# Quick Start

5 分钟快速上手 ZeroAgent。

## 安装

```bash
pip install -e .
```

## 配置

运行交互式配置向导：

```bash
zero-agent-configure
```

或直接使用环境变量：

```bash
export ZA_LLM_PROVIDER=anthropic
export ZA_LLM_API_KEY=sk-ant-xxx
export ZA_LLM_MODEL=claude-sonnet-4-6
```

或创建项目根目录的 YAML 配置文件 `config.yaml`：

```yaml
default_backend: default
llm_backends:
  default:
    provider: anthropic
    api_key: sk-ant-xxx
    api_base: https://api.anthropic.com
    model: claude-sonnet-4-6
```

## 首次运行

### 交互模式

```bash
zero-agent
```

输入任务描述即可开始：

```
> 帮我创建一个 hello.py 文件
```

### 一次性任务

```bash
zero-agent -i "列出当前目录下的文件"

### 函数模式 (单次子 agent 任务)

```bash
zero-agent --func prompt.txt --nobg
```

### 文件 I/O 持续子 agent 模式

```bash
zero-agent --task /path/to/task_dir --nobg
```

### Conductor 多子 agent 编排 UI

```bash
zero-agent-conductor
```

### ACP 协议桥 (JSON-RPC over stdio)

```bash
zero-agent-acp
```

### TUI 终端界面

```bash
pip install -e ".[ui]"
zero-agent-tui
```

### 使用 YAML 配置

```bash
zero-agent -c config.yaml -i "帮我分析 data.csv"
```

## 可选功能

```bash
# Streamlit Web UI
pip install -e ".[ui]"
streamlit run zero_agent/frontends/stapp.py

# 浏览器控制功能
pip install -e ".[browser]"
# 浏览器工具是可选能力扩展；确认 config.yaml:
# extensions:
#   browser: auto   # auto|true|false；auto 仅在 [browser] runtime 可导入时加载
# 还需要在浏览器中加载/连接 bundled extension:
# zero_agent/assets/tmwd_cdp_bridge
# 升级扩展代码后，在 chrome://extensions 点 Reload 使其生效；
# 常驻 bridge 还需重启 Python 进程以加载新驱动。
# 标签页生命周期、原生输入、网络捕获、无人值守改包和验证码读取需要扩展 v3.0+。
# 同一个扩展实例会同时连接 ZeroAgent(18765/18766) 和 dsh-browser-control(18865)，
# 不要再启用 dsh 自带的 extension，避免两个扩展抢同一个 tab 的 chrome.debugger。
# 无人值守流程：web_intercept(rules=[...]) 自动放行未匹配请求；
# web_captcha(selector=...) 先本地 OCR，失败时自动用视觉后端；
# 识别后 web_input(action="type", selector=输入框, text=验证码, submit=True)。

# PDF / Word / Excel / PPT 读取
pip install -e ".[docs]"

# OCR 功能
pip install -e ".[memory]"

# 全部功能
pip install -e ".[all-extras]"
```

## REPL 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/tools` | 列出可用工具 |
| `/model` | 显示当前模型 |
| `/backends` | 列出可用 LLM 后端 |
| `/switch <n>` | 切换后端 |
| `/resume` | 恢复历史会话 |
| `/continue` | 保存会话快照 |
| `/new` | 开始新会话 |
| `/plan <task>` | 创建并运行一个可验证的计划流程；验证完成后等待明确 `/plan execute`，不会自动改代码 |
| `/exit` | 退出 |
