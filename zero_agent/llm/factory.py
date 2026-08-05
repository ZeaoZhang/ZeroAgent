"""LLMFactory — 从配置创建 LLM 会话.

使用 litellm 统一路由所有 LLM 提供商。
当配置了 failover_backends 时，自动创建 AutoFailoverSession 包装器。
当 backend.tool_protocol == "text" 时，使用 TextToolSession 包裹 session 提供文本协议回退。
"""

from __future__ import annotations

from typing import Dict, Union

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.exceptions import ConfigError
from zero_agent.llm.sessions import LiteLLMSession, register_model_cost_map
from zero_agent.llm.failover import AutoFailoverSession
from zero_agent.llm.text_tool_client import TextToolSession


class LLMFactory:
    """LLM 会话工厂.

    使用方式:
        config = AgentConfig.from_yaml("config.yaml")
        session = LLMFactory.create_from_config(config)

        sessions = LLMFactory.create_all_sessions(config)
    """

    @staticmethod
    def create_session(
        backend_config: LLMBackendConfig,
        log_dir: str | None = None,
        sessions_dir: str | None = None,
        session_log_path: str | None = None,
    ) -> LiteLLMSession | TextToolSession:
        """创建 LLM 会话.

        当 backend_config.tool_protocol == "text" 时，
        返回包裹在 TextToolSession 中的 LiteLLMSession。

        Args:
            backend_config: 单个 LLM 后端的配置.
            log_dir: LLM 调用日志目录.
            sessions_dir: 会话历史日志目录.

        Returns:
            LiteLLMSession 或 TextToolSession 实例.

        Raises:
            ConfigError: 配置不完整（如缺少 api_key）.
        """
        if not backend_config.api_key:
            raise ConfigError(f"LLM 后端 '{backend_config.name}' 缺少 api_key")
        if not backend_config.model:
            raise ConfigError(f"LLM 后端 '{backend_config.name}' 缺少 model")
        session = LiteLLMSession(
            backend_config,
            log_dir=log_dir,
            sessions_dir=sessions_dir,
            session_log_path=session_log_path,
        )
        if getattr(backend_config, "tool_protocol", "native") == "text":
            session = TextToolSession(session, auto_save_tokens=True)
        return session

    @staticmethod
    def create_from_config(
        config: AgentConfig,
        session_log_path: str | None = None,
    ) -> Union[LiteLLMSession, TextToolSession, AutoFailoverSession]:
        """Create the primary session from validated configuration."""
        config.validate()
        register_model_cost_map(config.litellm_model_cost_map)
        primary_session = LLMFactory._get_primary_session(config, session_log_path=session_log_path)
        if config.failover_backends:
            return LLMFactory._wrap_failover(
                primary_session, config, session_log_path=session_log_path
            )
        return primary_session

    @staticmethod
    def create_all_sessions(
        config: AgentConfig,
        session_log_path: str | None = None,
    ) -> Dict[str, Union[LiteLLMSession, TextToolSession, AutoFailoverSession]]:
        """Create every configured backend session."""
        config.validate()
        register_model_cost_map(config.litellm_model_cost_map)
        sessions: Dict[str, Union[LiteLLMSession, TextToolSession, AutoFailoverSession]] = {}
        for name, backend_cfg in config.llm_backends.items():
            sessions[name] = LLMFactory.create_session(
                backend_cfg,
                log_dir=config.log_dir,
                sessions_dir=config.sessions_dir,
                session_log_path=session_log_path,
            )

        if config.failover_backends:
            primary_name = config.default_backend
            primary = sessions[primary_name]
            if not isinstance(primary, AutoFailoverSession):
                backups = [
                    sessions[name]
                    for name in config.failover_backends
                    if name != primary_name
                    and isinstance(sessions[name], (LiteLLMSession, TextToolSession))
                ]
                if backups:
                    sessions[primary_name] = AutoFailoverSession(
                        primary=primary,
                        backups=backups,
                        health_check_interval=LLMFactory._get_health_interval(config, primary_name),
                    )
        return sessions

    @staticmethod
    def _get_primary_session(
        config: AgentConfig,
        session_log_path: str | None = None,
    ) -> Union[LiteLLMSession, TextToolSession]:
        backend_cfg = config.llm_backends[config.default_backend]
        return LLMFactory.create_session(
            backend_cfg,
            log_dir=config.log_dir,
            sessions_dir=config.sessions_dir,
            session_log_path=session_log_path,
        )

    @staticmethod
    def _wrap_failover(
        primary: Union[LiteLLMSession, TextToolSession],
        config: AgentConfig,
        session_log_path: str | None = None,
    ) -> AutoFailoverSession:
        backups: list[LiteLLMSession | TextToolSession] = []
        for name in config.failover_backends:
            if name == primary.name:
                continue
            backend_cfg = config.llm_backends.get(name)
            if backend_cfg is None:
                continue
            backups.append(LLMFactory.create_session(
                backend_cfg,
                log_dir=config.log_dir,
                sessions_dir=config.sessions_dir,
                session_log_path=session_log_path,
            ))
        return AutoFailoverSession(
            primary=primary,
            backups=backups,
            health_check_interval=LLMFactory._get_health_interval(config, primary.name),
        )

    @staticmethod
    def _get_health_interval(config: AgentConfig, backend_name: str) -> int:
        """获取健康检查间隔.

        Args:
            config: Agent 配置.
            backend_name: 后端名称.

        Returns:
            健康检查间隔秒数.
        """
        backend_cfg = config.llm_backends.get(backend_name)
        if backend_cfg and hasattr(backend_cfg, "health_check_interval"):
            return backend_cfg.health_check_interval
        return 60
