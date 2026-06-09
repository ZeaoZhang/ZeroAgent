"""LLMFactory — 从配置创建 LLM 会话.

使用 litellm 统一路由所有 LLM 提供商。
当配置了 failover_backends 时，自动创建 AutoFailoverSession 包装器。
"""

from __future__ import annotations

from typing import Dict, Union

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.exceptions import ConfigError
from zero_agent.llm.sessions import LiteLLMSession, register_model_cost_map
from zero_agent.llm.failover import AutoFailoverSession


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
    ) -> LiteLLMSession:
        """创建 LiteLLMSession.

        Args:
            backend_config: 单个 LLM 后端的配置.
            log_dir: LLM 调用日志目录.
            sessions_dir: 会话历史日志目录.

        Returns:
            LiteLLMSession 实例.

        Raises:
            ConfigError: 配置不完整（如缺少 api_key）.
        """
        if not backend_config.api_key:
            raise ConfigError(
                f"LLM 后端 '{backend_config.name}' 缺少 api_key"
            )
        if not backend_config.model:
            raise ConfigError(
                f"LLM 后端 '{backend_config.name}' 缺少 model"
            )
        return LiteLLMSession(
            backend_config,
            log_dir=log_dir,
            sessions_dir=sessions_dir,
        )

    @staticmethod
    def create_from_config(
        config: AgentConfig,
    ) -> Union[LiteLLMSession, AutoFailoverSession]:
        """从 AgentConfig 创建 LLM 会话.

        当配置了 failover_backends 时，返回 AutoFailoverSession 包装器.

        Args:
            config: Agent 顶层配置.

        Returns:
            LiteLLMSession 或 AutoFailoverSession 实例.

        Raises:
            ConfigError: 没有可用的 LLM 后端配置.
        """
        if not config.llm_backends:
            raise ConfigError("没有配置任何 LLM 后端")

        register_model_cost_map(config.litellm_model_cost_map)
        primary_session = LLMFactory._get_primary_session(config)

        if config.failover_backends:
            return LLMFactory._wrap_failover(primary_session, config)

        return primary_session

    @staticmethod
    def create_all_sessions(
        config: AgentConfig,
    ) -> Dict[str, Union[LiteLLMSession, AutoFailoverSession]]:
        """创建所有已配置后端的独立会话.

        当配置了 failover_backends 时，default 后端会被包装为
        AutoFailoverSession.

        Args:
            config: Agent 顶层配置.

        Returns:
            后端名称 → session 的字典.

        Raises:
            ConfigError: 没有可用的 LLM 后端配置.
        """
        if not config.llm_backends:
            raise ConfigError("没有配置任何 LLM 后端")

        register_model_cost_map(config.litellm_model_cost_map)
        sessions: Dict[str, Union[LiteLLMSession, AutoFailoverSession]] = {}
        for name, backend_cfg in config.llm_backends.items():
            session = LLMFactory.create_session(
                backend_cfg,
                log_dir=config.log_dir,
                sessions_dir=config.sessions_dir,
            )
            sessions[name] = session

        if config.failover_backends:
            primary_name = config.default_backend or next(
                iter(config.llm_backends.keys())
            )
            if primary_name in sessions and isinstance(
                sessions[primary_name], LiteLLMSession
            ):
                primary = sessions[primary_name]
                backups = [
                    sessions[name]
                    for name in config.failover_backends
                    if name in sessions
                    and name != primary_name
                    and isinstance(sessions[name], LiteLLMSession)
                ]
                if backups:
                    sessions[primary_name] = AutoFailoverSession(
                        primary=primary,
                        backups=backups,
                        health_check_interval=LLMFactory._get_health_interval(
                            config, primary_name
                        ),
                    )

        return sessions

    @staticmethod
    def _get_primary_session(
        config: AgentConfig,
    ) -> LiteLLMSession:
        """获取主 session.

        Args:
            config: Agent 配置.

        Returns:
            主 LiteLLMSession.
        """
        backend_cfg = config.llm_backends.get(config.default_backend)
        if backend_cfg is None:
            backend_cfg = next(iter(config.llm_backends.values()))
        return LLMFactory.create_session(
            backend_cfg,
            log_dir=config.log_dir,
            sessions_dir=config.sessions_dir,
        )

    @staticmethod
    def _wrap_failover(
        primary: LiteLLMSession,
        config: AgentConfig,
    ) -> AutoFailoverSession:
        """创建 AutoFailoverSession 包装器.

        Args:
            primary: 主 session.
            config: Agent 配置.

        Returns:
            AutoFailoverSession 实例.
        """
        backups: list[LiteLLMSession] = []
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
            ))

        health_interval = LLMFactory._get_health_interval(config, primary.name)

        return AutoFailoverSession(
            primary=primary,
            backups=backups,
            health_check_interval=health_interval,
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
