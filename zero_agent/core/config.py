"""Agent 配置系统.

默认配置优先读取项目根目录的 config.yaml；ZA_CONFIG_PATH 仅作为显式覆盖。
支持三种配置来源（优先级从高到低）:
    1. 代码直接构造 AgentConfig(...)
    2. ZA_CONFIG_PATH 或项目 config.yaml → AgentConfig.from_yaml(path)
    3. 环境变量 → AgentConfig.from_env()

LLMBackendConfig: 单个 LLM 后端的连接和参数配置.
AgentConfig: 顶层 agent 配置.
"""

from __future__ import annotations
import re

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from zero_agent.core.exceptions import ConfigError

# 配置文件 mtime 缓存，用于热加载检测
_config_mtime: dict[str, int] = {}


def _config_path_key(path: str | Path) -> str:
    """Return the stable key used for config mtime tracking."""

    return str(Path(path).expanduser())


def _read_config_mtime(path: str | Path) -> Optional[int]:
    """Return a config file mtime, or None when the file is unavailable."""

    try:
        return os.stat(_config_path_key(path)).st_mtime_ns
    except OSError:
        return None


def _seed_config_mtime(path: str | Path, *, mtime: Optional[int] = None) -> None:
    """Record the current mtime baseline for a loaded config file."""

    current_mtime = _read_config_mtime(path) if mtime is None else mtime
    if current_mtime is not None:
        _config_mtime[_config_path_key(path)] = current_mtime


def _commit_config_mtime(path: str | Path, *, mtime: Optional[int] = None) -> None:
    """Commit a successfully-applied hot reload mtime baseline."""

    _seed_config_mtime(path, mtime=mtime)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """Return the ZeroAgent project root for project-local defaults."""
    return PROJECT_ROOT


def default_config_path() -> Path:
    """Return the default config path, preferring project-local config.yaml.

    ZA_CONFIG_PATH remains an explicit override for temporary test/dev runs.
    """
    env_path = os.environ.get("ZA_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return PROJECT_ROOT / "config.yaml"


def load_default_config() -> "AgentConfig":
    """Load the default config from project config.yaml, then env fallback."""
    path = default_config_path()
    if path.is_file():
        config = AgentConfig.from_yaml(path)
    else:
        config = AgentConfig.from_env()
    config._source_path = _config_path_key(path)  # type: ignore[attr-defined]
    return config


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
        thinking_type: 思考类型，如 "enabled"；显式配置时按请求协议透传.
        thinking_budget_tokens: 思考 token 预算.
        vision: 是否允许该 backend 接收图片请求.
        vision_model: 视觉请求的可选模型覆盖，未设置时使用 model.
        vision_max_tokens: 视觉请求的最大输出 token 数.
        vision_detail: 视觉细节级别 (auto/low/high).
        vision_max_pixels: 本地图片预处理的最大像素数.
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
    vision: bool = False
    vision_model: Optional[str] = None
    vision_max_tokens: Optional[int] = 1024
    vision_detail: str = "auto"
    vision_max_pixels: int = 1_440_000
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
    tool_protocol: str = "native"  # "native" | "text"


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
        litellm_model_cost_map: LiteLLM 模型价格/上下文窗口表缓存路径.
    """

    llm_backends: dict[str, LLMBackendConfig] = field(default_factory=dict)
    default_backend: str = "default"
    max_turns: int = 80
    workspace_dir: str = "./workspace"
    memory_dir: str = "./memory"
    sessions_dir: str = "./workspace/sessions"
    verbose: bool = True
    language: str = "auto"
    incremental_output: bool = False
    failover_backends: list[str] = field(default_factory=list)
    log_dir: Optional[str] = None
    peer_hint: bool = False
    enable_worldline: bool = False
    litellm_model_cost_map: Optional[str] = None

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

    def validate(self) -> None:
        """Reject invalid backend selection before creating LLM sessions."""
        if not self.llm_backends:
            raise ConfigError("no LLM backends configured")
        if self.default_backend == "":
            self.default_backend = next(iter(self.llm_backends))
        elif self.default_backend not in self.llm_backends:
            available = ", ".join(sorted(self.llm_backends))
            raise ConfigError(
                f"invalid default backend '{self.default_backend}'; available: {available}"
            )
        for name in self.failover_backends:
            if name not in self.llm_backends:
                available = ", ".join(sorted(self.llm_backends))
                raise ConfigError(
                    f"invalid failover backend '{name}'; available: {available}"
                )


    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        *,
        seed_mtime: bool = True,
    ) -> "AgentConfig":
        """从 YAML 配置文件加载 Agent 配置.

        YAML 格式示例:
            llm_backends:
              default:
                provider: anthropic
                api_key: sk-ant-xxx
                api_base: https://api.anthropic.com
                model: claude-sonnet-4-6
            max_turns: 80

        相对路径 (workspace_dir, memory_dir, sessions_dir, log_dir)
        会自动基于配置文件所在目录解析为绝对路径.

        Args:
            path: YAML 文件路径.
            seed_mtime: 是否把该文件当前 mtime 作为热重载基线.

        Returns:
            解析后的 AgentConfig 实例.

        Raises:
            ImportError: 未安装 yaml 库（pip install pyyaml）.
            FileNotFoundError: 配置文件不存在.
        """
        import yaml

        path_key = _config_path_key(path)
        with open(path_key, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        config = cls._from_dict(data)
        config._resolve_paths(os.path.dirname(os.path.abspath(path_key)))
        config._source_path = path_key  # type: ignore[attr-defined]
        mtime = _read_config_mtime(path_key)
        if mtime is not None:
            config._source_mtime_ns = mtime  # type: ignore[attr-defined]
            if seed_mtime:
                _seed_config_mtime(path_key, mtime=mtime)
        return config

    def _resolve_paths(self, base_dir: str) -> None:
        """将相对路径字段基于 base_dir 解析为绝对路径.

        仅处理非空且非绝对路径的字段。已为绝对路径的值保持不变。

        Args:
            base_dir: 基准目录（通常为配置文件所在目录）.
        """
        for attr in (
            "workspace_dir",
            "memory_dir",
            "sessions_dir",
            "log_dir",
            "litellm_model_cost_map",
        ):
            val = getattr(self, attr)
            if val and not os.path.isabs(val):
                setattr(self, attr, os.path.normpath(os.path.join(base_dir, val)))

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
            ZA_MEMORY_DIR    — 记忆目录 (默认 "./memory")

        Returns:
            从环境变量构建的 AgentConfig.
        """
        provider = os.environ.get("ZA_LLM_PROVIDER", "anthropic")
        api_key = os.environ.get("ZA_LLM_API_KEY", "")
        api_base = os.environ.get("ZA_LLM_API_BASE", "https://api.anthropic.com")
        model = os.environ.get("ZA_LLM_MODEL", "")
        api_mode = os.environ.get("ZA_LLM_API_MODE", "chat_completions")
        if api_mode not in ("chat_completions", "responses"):
            from zero_agent.core.exceptions import ConfigError
            raise ConfigError(
                f"ZA_LLM_API_MODE 不支持: {api_mode!r} (允许: chat_completions | responses)"
            )
        tool_protocol = os.environ.get("ZA_LLM_TOOL_PROTOCOL", "native")
        if tool_protocol not in ("native", "text"):
            from zero_agent.core.exceptions import ConfigError
            raise ConfigError(
                f"ZA_LLM_TOOL_PROTOCOL 不支持: {tool_protocol!r} (允许: native | text)"
            )
        max_turns = int(os.environ.get("ZA_MAX_TURNS", "80"))
        workspace_dir = os.environ.get("ZA_WORKSPACE_DIR", "./workspace")
        memory_dir = os.environ.get("ZA_MEMORY_DIR", "./memory")
        sessions_dir = os.environ.get("ZA_SESSIONS_DIR", "./workspace/sessions")
        language = os.environ.get("ZA_LANG", "auto")
        peer_hint = os.environ.get("ZA_PEER_HINT", "").lower() in ("1", "true", "yes", "on")
        litellm_model_cost_map = os.environ.get("ZA_LITELLM_MODEL_COST_MAP") or None

        config = cls(
            llm_backends={
                "default": LLMBackendConfig(
                    name="default",
                    provider=provider,
                    api_key=api_key,
                    api_base=api_base,
                    model=model,
                    api_mode=api_mode,
                    tool_protocol=tool_protocol,
                )
            },
            default_backend="default",
            max_turns=max_turns,
            workspace_dir=workspace_dir,
            memory_dir=memory_dir,
            sessions_dir=sessions_dir,
            language=language,
            peer_hint=peer_hint,
            litellm_model_cost_map=litellm_model_cost_map,
        )
        config._resolve_paths(os.getcwd())
        return config

    @classmethod
    def _from_dict(cls, data: dict) -> "AgentConfig":
        """Build AgentConfig from parsed YAML data."""
        data = data or {}
        backends: dict[str, LLMBackendConfig] = {}
        env_pattern = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
        for name, cfg in data.get("llm_backends", {}).items():
            backend_data = dict(cfg or {})
            api_key = backend_data.get("api_key")
            if isinstance(api_key, str):
                match = env_pattern.fullmatch(api_key)
                if match:
                    env_name = match.group(1)
                    resolved = os.environ.get(env_name, "")
                    if not resolved:
                        raise ConfigError(f"missing environment variable: {env_name}")
                    backend_data["api_key"] = resolved
            backends[name] = LLMBackendConfig(name=name, **backend_data)

        return cls(
            llm_backends=backends,
            default_backend=data.get("default_backend", "default"),
            max_turns=data.get("max_turns", 80),
            workspace_dir=data.get("workspace_dir", "./workspace"),
            memory_dir=data.get("memory_dir", "./memory"),
            sessions_dir=data.get("sessions_dir", "./workspace/sessions"),
            verbose=data.get("verbose", True),
            language=data.get("language", "auto"),
            incremental_output=data.get("incremental_output", False),
            failover_backends=data.get("failover_backends", []),
            log_dir=data.get("log_dir"),
            peer_hint=data.get("peer_hint", False),
            enable_worldline=data.get("enable_worldline", False),
            litellm_model_cost_map=data.get("litellm_model_cost_map"),
        )


def reload_config_if_changed(config_path: str) -> Optional[AgentConfig]:
    """若配置文件自上次读取以来已更改，则重新加载并返回新配置.

    通过比较文件 mtime 检测变更，仅在文件内容变化时重新读取，
    避免不必要的 I/O. The caller commits the new mtime only after the
    parsed config has been fully applied.

    Args:
        config_path: YAML 配置文件路径.

    Returns:
        新的 AgentConfig 如果文件已变更，否则 None.
    """
    path_key = _config_path_key(config_path)
    mtime = _read_config_mtime(path_key)
    if mtime is None:
        return None

    current = _config_mtime.get(path_key)
    if current == mtime:
        return None

    return AgentConfig.from_yaml(path_key, seed_mtime=False)
