"""Tests for core/config.py — YAML config parsing and runtime effects."""

from zero_agent.core.config import (
    AgentConfig,
    LLMBackendConfig,
    default_config_path,
    load_default_config,
)
from zero_agent.llm.failover import AutoFailoverSession
from zero_agent.llm.factory import LLMFactory


def test_from_yaml_loads_failover_thinking_and_log_dir(tmp_path) -> None:
    """YAML 配置应读取 failover、thinking 和 log_dir 字段."""
    log_dir = tmp_path / "logs"
    cost_map = tmp_path / "model_prices_and_context_window.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
default_backend: primary
max_turns: 12
workspace_dir: {tmp_path / "workspace"}
memory_dir: {tmp_path / "memory"}
failover_backends:
  - backup
log_dir: {log_dir}
litellm_model_cost_map: ./model_prices_and_context_window.json
llm_backends:
  primary:
    provider: anthropic
    api_key: sk-primary
    api_base: https://api.anthropic.com
    model: claude-test
    thinking_type: enabled
    thinking_budget_tokens: 4096
  backup:
    provider: openai
    api_key: sk-backup
    api_base: https://api.openai.com/v1
    model: gpt-test
""",
        encoding="utf-8",
    )

    config = AgentConfig.from_yaml(config_path)

    assert config.max_turns == 12
    assert config.failover_backends == ["backup"]
    assert config.log_dir == str(log_dir)
    assert config.litellm_model_cost_map == str(cost_map)
    assert config.llm_backends["primary"].thinking_type == "enabled"
    assert config.llm_backends["primary"].thinking_budget_tokens == 4096


def test_from_yaml_defaults_sessions_dir_under_workspace(tmp_path) -> None:
    """YAML 未配置 sessions_dir 时，应默认落在 workspace/sessions."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm_backends:
  default:
    provider: openai
    api_key: sk-test
    api_base: https://api.openai.com/v1
    model: gpt-test
""".lstrip(),
        encoding="utf-8",
    )

    config = AgentConfig.from_yaml(config_path)

    assert config.workspace_dir == str(tmp_path / "workspace")
    assert config.sessions_dir == str(tmp_path / "workspace" / "sessions")


def test_factory_applies_yaml_log_dir_to_sessions(tmp_path) -> None:
    """log_dir 配置应传入普通 session 和 failover 包装内的 session."""
    log_dir = str(tmp_path / "logs")
    sessions_dir = str(tmp_path / "workspace" / "sessions")
    config = AgentConfig(
        llm_backends={
            "primary": LLMBackendConfig(
                name="primary",
                provider="anthropic",
                api_key="sk-primary",
                api_base="https://api.anthropic.com",
                model="claude-test",
            ),
            "backup": LLMBackendConfig(
                name="backup",
                provider="openai",
                api_key="sk-backup",
                api_base="https://api.openai.com/v1",
                model="gpt-test",
            ),
        },
        default_backend="primary",
        failover_backends=["backup"],
        log_dir=log_dir,
        sessions_dir=sessions_dir,
    )

    sessions = LLMFactory.create_all_sessions(config)

    primary = sessions["primary"]
    assert isinstance(primary, AutoFailoverSession)
    assert primary.primary._log_dir == log_dir
    assert primary.backups[0]._log_dir == log_dir
    assert primary.primary._sessions_dir == sessions_dir
    assert primary.backups[0]._sessions_dir == sessions_dir
    assert sessions["backup"]._log_dir == log_dir
    assert sessions["backup"]._sessions_dir == sessions_dir


def test_factory_create_from_config_applies_sessions_dir_to_failover_backups(
    tmp_path,
) -> None:
    """create_from_config 的 failover 备份 session 也应写入配置的 sessions_dir."""
    sessions_dir = str(tmp_path / "workspace" / "sessions")
    config = AgentConfig(
        llm_backends={
            "primary": LLMBackendConfig(
                name="primary",
                provider="anthropic",
                api_key="sk-primary",
                api_base="https://api.anthropic.com",
                model="claude-test",
            ),
            "backup": LLMBackendConfig(
                name="backup",
                provider="openai",
                api_key="sk-backup",
                api_base="https://api.openai.com/v1",
                model="gpt-test",
            ),
        },
        default_backend="primary",
        failover_backends=["backup"],
        sessions_dir=sessions_dir,
    )

    session = LLMFactory.create_from_config(config)

    assert isinstance(session, AutoFailoverSession)
    assert session.primary._sessions_dir == sessions_dir
    assert session.backups[0]._sessions_dir == sessions_dir


def test_factory_registers_configured_litellm_cost_map(monkeypatch, tmp_path) -> None:
    """LLMFactory 应按 AgentConfig 注册本地 LiteLLM model cost map."""
    cost_map = str(tmp_path / "model_prices_and_context_window.json")
    config = AgentConfig(
        llm_backends={
            "default": LLMBackendConfig(
                name="default",
                provider="openai",
                api_key="sk-test",
                api_base="https://api.openai.com/v1",
                model="gpt-test",
            ),
        },
        litellm_model_cost_map=cost_map,
    )
    registered: list[str | None] = []

    monkeypatch.setattr(
        "zero_agent.llm.factory.register_model_cost_map",
        lambda path: registered.append(path) or True,
    )

    LLMFactory.create_all_sessions(config)

    assert registered == [cost_map]


def test_default_config_path_uses_project_config(monkeypatch, tmp_path) -> None:
    """默认配置文件路径应指向项目根目录的 config.yaml."""
    monkeypatch.delenv("ZA_CONFIG_PATH", raising=False)
    monkeypatch.setattr("zero_agent.core.config.PROJECT_ROOT", tmp_path)

    assert default_config_path() == tmp_path / "config.yaml"


def test_load_default_config_prefers_project_config(monkeypatch, tmp_path) -> None:
    """未指定 ZA_CONFIG_PATH 时，应优先读取项目根目录 config.yaml."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
default_backend: local
llm_backends:
  local:
    provider: openai
    api_key: sk-local
    api_base: https://example.invalid/v1
    model: local-test
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("ZA_CONFIG_PATH", raising=False)
    monkeypatch.setattr("zero_agent.core.config.PROJECT_ROOT", tmp_path)

    config = load_default_config()

    assert config.default_backend == "local"
    assert config.llm_backends["local"].model == "local-test"
    assert getattr(config, "_source_path") == str(config_path)
