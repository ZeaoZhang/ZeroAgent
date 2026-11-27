"""自我扩展提示词块：真实路径、生效时机、当前模式与底线."""

from __future__ import annotations

from pathlib import Path

from zero_agent.core import self_prompt
from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, LLMBackendConfig


def _config(tmp_path: Path, **overrides) -> AgentConfig:
    base = dict(
        llm_backends={
            "d": LLMBackendConfig(
                name="d",
                provider="openai",
                api_key="k",
                api_base="https://api.openai.com/v1",
                model="gpt-4o",
            )
        },
        default_backend="d",
        workspace_dir=str(tmp_path / "ws"),
        memory_dir=str(tmp_path / "mem"),
        sessions_dir=str(tmp_path / "sessions"),
        extensions_home=str(tmp_path / "home"),
    )
    base.update(overrides)
    config = AgentConfig(**base)
    for path in (config.workspace_dir, config.memory_dir, config.extensions_home):
        Path(path).mkdir(parents=True, exist_ok=True)
    return config


def test_block_lists_real_extension_paths(tmp_path: Path) -> None:
    config = _config(tmp_path, memory_dir=str(tmp_path / "mem"))

    text = self_prompt.build(config, "zh")

    from zero_agent.utils.files import display_path, project_root

    root = project_root()
    home = display_path(str(tmp_path / "home"), root)
    mem = display_path(str(tmp_path / "mem"), root)
    assert f"{home}/skills" in text
    assert f"{home}/extensions" in text
    assert f"{home}/mcp.json" in text
    assert f"{mem}/sops" in text
    assert "zero_agent" in text and "/**/*.py" in text
    assert "setup(ctx)" in text


def test_block_states_mode_sandbox_and_floor(tmp_path: Path) -> None:
    full = self_prompt.build(_config(tmp_path), "zh")
    assert "权限：full" in full
    assert "门控层不执行" in full
    assert "沙箱" in full and "workspace" in full
    assert "~/Documents" in full
    assert ".git" in full

    ask = self_prompt.build(_config(tmp_path, permission_mode="ask"), "zh")
    assert "权限：ask" in ask
    assert "等待用户批准" in ask


def test_block_reports_disabled_extensions(tmp_path: Path) -> None:
    text = self_prompt.build(_config(tmp_path, extensions_enabled=False), "zh")

    assert "extensions.enabled=false" in text


def test_english_block(tmp_path: Path) -> None:
    text = self_prompt.build(_config(tmp_path, language="en"), "en")

    assert "<self_modification>" in text
    assert "Permissions: full" in text
    assert "Extend and upgrade" in text or "extend and upgrade" in text
    assert "hot restart" in text or "os.execv" in text


def test_system_prompt_contains_the_block(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.language = "zh"
    agent = ZeroAgent(config)
    try:
        prompt = agent._build_system_prompt()

        assert "<self_modification>" in prompt
        from zero_agent.utils.files import display_path, project_root

        home = display_path(str(tmp_path / "home"), project_root())
        assert f"{home}/extensions" in prompt
        assert "权限：full" in prompt
        assert "os.execv" in prompt
    finally:
        agent.close()
