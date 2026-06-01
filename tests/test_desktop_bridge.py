"""Tests for the ZeroAgent desktop bridge."""

from zero_agent.frontends import desktop_bridge


def _write_config_without_api_key(path) -> None:
    path.write_text(
        """
default_backend: default
llm_backends:
  default:
    provider: anthropic
    api_key: ""
    api_base: https://api.anthropic.com
    model: claude-sonnet-4-6
""".lstrip(),
        encoding="utf-8",
    )


def test_model_profiles_returns_placeholder_when_api_key_missing(monkeypatch, tmp_path) -> None:
    """The web shell should boot cleanly before the user configures an API key."""
    config_path = tmp_path / "config.yaml"
    _write_config_without_api_key(config_path)
    monkeypatch.setenv("ZA_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("ZA_LLM_API_KEY", raising=False)

    profiles = desktop_bridge.AgentManager().list_model_profiles()

    assert profiles == [{
        "id": 0,
        "llmNo": 0,
        "name": "Default / Auto",
        "active": True,
        "configured": False,
        "error": "LLM 后端 'default' 缺少 api_key",
    }]


def test_run_agent_turn_reports_config_error_without_traceback(
    monkeypatch, tmp_path, capsys
) -> None:
    """Missing API keys should be a user-visible setup error, not a server crash."""
    config_path = tmp_path / "config.yaml"
    _write_config_without_api_key(config_path)
    monkeypatch.setenv("ZA_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("ZA_LLM_API_KEY", raising=False)
    manager = desktop_bridge.AgentManager()
    sess = manager.create_session()

    manager.run_agent_turn(sess, "hello")

    assert sess.status == "error"
    assert sess.last_error == "LLM 后端 'default' 缺少 api_key"
    assert sess.messages[-1]["role"] == "error"
    assert sess.messages[-1]["content"] == "LLM 后端 'default' 缺少 api_key"
    assert "Traceback" not in capsys.readouterr().err
