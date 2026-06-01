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
# 还需要在浏览器中加载/连接 bundled extension:
# zero_agent/assets/tmwd_cdp_bridge

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
| `/exit` | 退出 |
