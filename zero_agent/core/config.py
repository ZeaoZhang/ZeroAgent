"""Agent 配置系统.

支持三种配置来源（优先级从高到低）:
    1. 代码直接构造 AgentConfig(...)
    2. YAML 文件 → AgentConfig.from_yaml(path)
    3. 环境变量 → AgentConfig.from_env()

LLMBackendConfig: 单个 LLM 后端的连接和参数配置.
AgentConfig: 顶层 agent 配置.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 配置文件 mtime 缓存，用于热加载检测
_config_mtime: dict[str, int] = {}


@dataclass
class LLMBackendConfig:
    """单个 LLM 后端的完整配置.

    Attributes:
        name: 后端别名.
        provider: litellm provider 标识 (如 "anthropic", "openai", "deepseek").
        api_key: API 密钥.
        api_base: API 基础 URL.
        model: 模型 ID.
        context_window: 上下文窗口大小.
        max_tokens: 单次响应最大 token 数.
        temperature: 采样温度 0-2.
        reasoning_effort: 推理力度 (none/minimal/low/medium/high/xhigh).
        thinking_type: Claude thinking 类型，如 "enabled".
        thinking_budget_tokens: Claude thinking token 预算.
        max_retries: HTTP 请求失败最大重试次数.
        connect_timeout: TCP 连接超时秒数.
        read_timeout: 读取超时秒数.
        proxy: HTTP 代理 URL.
        stream: 是否启用 SSE 流式响应.
        verify: SSL 证书验证.
        service_tier: 优先级 (仅部分提供商支持).
    """

    name: str
    provider: str
    api_key: str
    api_base: str
    model: str = ""
    context_window: int = 30000
    max_tokens: Optional[int] = None
    temperature: float = 1.0
    reasoning_effort: Optional[str] = None
    thinking_type: Optional[str] = None
    thinking_budget_tokens: Optional[int] = None
    max_retries: int = 4
    connect_timeout: int = 5
    read_timeout: int = 30
    proxy: Optional[str] = None
    extra_headers: Optional[dict] = None
    stream: bool = True
    verify: bool = True
    service_tier: Optional[str] = None
    health_check_interval: int = 60
    spring_back_multiplier: float = 1.0  # spring-back 定时器乘数
    api_mode: str = "chat_completions"  # "chat_completions" | "responses"


@dataclass
class AgentConfig:
    """顶层 Agent 配置，聚合 LLM 后端和工作环境参数.

    Attributes:
        llm_backends: 所有可用的 LLM 后端配置字典，key 为后端的 name.
        default_backend: 默认使用的后端 name.
        max_turns: Agent 单次任务最大轮次上限.
        workspace_dir: 工作目录.
        memory_dir: 记忆文件存储目录.
        sessions_dir: 会话历史日志存储目录.
        verbose: 是否输出详细日志.
        language: 界面语言 "auto" | "zh" | "en".
        incremental_output: 是否增量输出流式内容到 UI.
        log_dir: LLM 调用日志目录.
    """

    llm_backends: dict[str, LLMBackendConfig] = field(default_factory=dict)
    default_backend: str = "default"
    max_turns: int = 80
    workspace_dir: str = "./workspace"
    memory_dir: str = "./zero_agent/memory"
    sessions_dir: str = "./workspace/sessions"
    verbose: bool = True
    language: str = "auto"
    incremental_output: bool = False
    failover_backends: list[str] = field(default_factory=list)
    log_dir: Optional[str] = None

    @property
    def resolved_language(self) -> str:
        """解析系统提示词和 handler 消息的语言.

        "auto" 时依次尝试:
            1. 系统 locale（含 zh/chinese → zh）
            2. 模型类型（国产模型 → zh）
            3. 默认 en

        Returns:
            "zh" 或 "en".
        """
        if self.language != "auto":
            return self.language

        try:
            import locale
            sys_locale = (locale.getlocale()[0] or "").lower()
            if any(k in sys_locale for k in ("zh", "chinese")):
                return "zh"
        except Exception:
            pass

        return "en"

    @property
    def resolved_tool_language(self) -> str:
        """解析工具描述的语言.

        国产模型用中文，国际模型默认英文。
        显式设置 language 时覆盖此行为.

        Returns:
            "zh" 或 "en".
        """
        if self.language != "auto":
            return self.language

        for backend in self.llm_backends.values():
            model_lower = backend.model.lower()
            if any(
                k in model_lower
                for k in ("glm", "minimax", "kimi", "qwen", "deepseek")
            ):
                return "zh"

        return "en"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentConfig":
        """从 YAML 配置文件加载 Agent 配置.

        YAML 格式示例:
            llm_backends:
              default:
                provider: anthropic
                api_key: sk-ant-xxx
                api_base: https://api.anthropic.com
                model: claude-sonnet-4-6
            max_turns: 80

        Args:
            path: YAML 文件路径.

        Returns:
            解析后的 AgentConfig 实例.

        Raises:
            ImportError: 未安装 yaml 库（pip install pyyaml）.
            FileNotFoundError: 配置文件不存在.
        """
        import yaml

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls._from_dict(data)

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """从环境变量构建最小可用配置.

        读取的环境变量:
            ZA_LLM_PROVIDER  — 后端类型 (默认 "anthropic")
            ZA_LLM_API_KEY   — API 密钥
            ZA_LLM_API_BASE  — API 基础 URL
            ZA_LLM_MODEL     — 模型 ID
            ZA_MAX_TURNS     — 最大轮次 (默认 80)
            ZA_WORKSPACE_DIR — 工作目录 (默认 "./workspace")

        Returns:
            从环境变量构建的 AgentConfig.
        """
        provider = os.environ.get("ZA_LLM_PROVIDER", "anthropic")
        api_key = os.environ.get("ZA_LLM_API_KEY", "")
        api_base = os.environ.get("ZA_LLM_API_BASE", "https://api.anthropic.com")
        model = os.environ.get("ZA_LLM_MODEL", "")
        max_turns = int(os.environ.get("ZA_MAX_TURNS", "80"))
        workspace_dir = os.environ.get("ZA_WORKSPACE_DIR", "./workspace")
        language = os.environ.get("ZA_LANG", "auto")

        return cls(
            llm_backends={
                "default": LLMBackendConfig(
                    name="default",
                    provider=provider,
                    api_key=api_key,
                    api_base=api_base,
                    model=model,
                )
            },
            default_backend="default",
            max_turns=max_turns,
            workspace_dir=workspace_dir,
            language=language,
        )

    @classmethod
    def _from_dict(cls, data: dict) -> "AgentConfig":
        """从已解析的字典构建 AgentConfig（内部方法）.

        Args:
            data: 配置字典，格式与 YAML 文件一致.

        Returns:
            AgentConfig 实例.
        """
        data = data or {}
        backends: dict[str, LLMBackendConfig] = {}
        for name, cfg in data.get("llm_backends", {}).items():
            backends[name] = LLMBackendConfig(name=name, **cfg)

        return cls(
            llm_backends=backends,
            default_backend=data.get("default_backend", "default"),
            max_turns=data.get("max_turns", 80),
            workspace_dir=data.get("workspace_dir", "./workspace"),
            memory_dir=data.get("memory_dir", "./zero_agent/memory"),
            sessions_dir=data.get("sessions_dir", "./workspace/sessions"),
            verbose=data.get("verbose", True),
            language=data.get("language", "auto"),
            incremental_output=data.get("incremental_output", False),
            failover_backends=data.get("failover_backends", []),
            log_dir=data.get("log_dir"),
        )


def reload_config_if_changed(config_path: str) -> Optional[AgentConfig]:
    """若配置文件自上次读取以来已更改，则重新加载并返回新配置.

    与 GenericAgent 的 reload_mykeys() 对齐：通过比较文件 mtime 检测变更，
    仅在文件内容变化时重新读取，避免不必要的 I/O.

    Args:
        config_path: YAML 配置文件路径.

    Returns:
        新的 AgentConfig 如果文件已变更，否则 None.
    """
    try:
        mtime = os.stat(config_path).st_mtime_ns
    except OSError:
        return None

    if config_path in _config_mtime and _config_mtime[config_path] == mtime:
        return None

    _config_mtime[config_path] = mtime
    return AgentConfig.from_yaml(config_path)
