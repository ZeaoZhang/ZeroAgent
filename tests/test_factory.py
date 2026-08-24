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


def test_tracer_is_shared_by_native_text_and_failover_sessions() -> None:
    config = AgentConfig(
        llm_backends={
            "primary": _backend("primary"),
            "backup": _backend("backup", tool_protocol="text"),
        },
        default_backend="primary",
        failover_backends=["backup"],
    )
    tracer = object()

    sessions = LLMFactory.create_all_sessions(config, tracer=tracer)

    primary = sessions["primary"]
    backup = sessions["backup"]
    assert isinstance(primary, AutoFailoverSession)
    assert isinstance(backup, TextToolSession)
    assert primary.primary._tracer is tracer
    assert backup.backend._tracer is tracer
    assert primary.backups[0].backend._tracer is tracer


def test_factory_rejects_invalid_default_before_constructing_sessions(monkeypatch) -> None:
    config = AgentConfig(llm_backends={"defined": _backend("defined")}, default_backend="missing")
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("session construction should not run")

    monkeypatch.setattr(LLMFactory, "create_session", fail_if_called)
    import pytest
    from zero_agent.core.exceptions import ConfigError
    with pytest.raises(ConfigError, match="missing"):
        LLMFactory.create_all_sessions(config)
    assert called is False


def test_factory_rejects_invalid_failover_before_constructing_sessions(monkeypatch) -> None:
    config = AgentConfig(
        llm_backends={"primary": _backend("primary")},
        default_backend="primary",
        failover_backends=["missing"],
    )

    monkeypatch.setattr(LLMFactory, "create_session", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("session construction should not run")))
    import pytest
    from zero_agent.core.exceptions import ConfigError
    with pytest.raises(ConfigError, match="missing"):
        LLMFactory.create_all_sessions(config)


def test_factory_accepts_explicit_session_log_path(tmp_path) -> None:
    config = AgentConfig(llm_backends={"defined": _backend("defined")}, default_backend="defined")
    path = str(tmp_path / "owned.txt")
    session = LLMFactory.create_all_sessions(config, session_log_path=path)["defined"]
    assert session.log_path == path