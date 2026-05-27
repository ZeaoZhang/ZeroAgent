"""Tests for core/config.py — YAML config parsing and runtime effects."""

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.llm.failover import AutoFailoverSession
from zero_agent.llm.factory import LLMFactory


def test_from_yaml_loads_failover_thinking_and_log_dir(tmp_path) -> None:
    """YAML 配置应读取 failover、thinking 和 log_dir 字段."""
    log_dir = tmp_path / "logs"
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
    assert config.llm_backends["primary"].thinking_type == "enabled"
    assert config.llm_backends["primary"].thinking_budget_tokens == 4096


def test_factory_applies_yaml_log_dir_to_sessions(tmp_path) -> None:
    """log_dir 配置应传入普通 session 和 failover 包装内的 session."""
    log_dir = str(tmp_path / "logs")
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
    )

    sessions = LLMFactory.create_all_sessions(config)

    primary = sessions["primary"]
    assert isinstance(primary, AutoFailoverSession)
    assert primary.primary._log_dir == log_dir
    assert primary.backups[0]._log_dir == log_dir
    assert sessions["backup"]._log_dir == log_dir
