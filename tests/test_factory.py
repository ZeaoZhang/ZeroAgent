"""Tests for LLMFactory session wiring."""

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.llm.factory import LLMFactory
from zero_agent.llm.failover import AutoFailoverSession
from zero_agent.llm.text_tool_client import TextToolSession


def _backend(name: str, *, tool_protocol: str = "native") -> LLMBackendConfig:
    return LLMBackendConfig(
        name=name,
        provider="openai",
        api_key=f"sk-{name}",
        api_base="https://api.openai.com/v1",
        model=f"{name}-model",
        tool_protocol=tool_protocol,
    )


def test_create_all_sessions_accepts_text_protocol_backup_in_failover() -> None:
    config = AgentConfig(
        llm_backends={
            "primary": _backend("primary"),
            "backup": _backend("backup", tool_protocol="text"),
        },
        default_backend="primary",
        failover_backends=["backup"],
    )

    sessions = LLMFactory.create_all_sessions(config)

    primary = sessions["primary"]
    assert isinstance(primary, AutoFailoverSession)
    assert len(primary.backups) == 1
    assert isinstance(primary.backups[0], TextToolSession)
    assert sessions["backup"] is primary.backups[0]
