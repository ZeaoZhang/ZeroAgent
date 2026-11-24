"""Browser tools are an opt-in capability extension, not core builtins."""

from __future__ import annotations

from pathlib import Path

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.extensions.builtin import browser_tools
from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.runtime import ExtensionRuntime
from zero_agent.tools.registry import ToolRegistry

BROWSER_TOOLS = {
    "web_scan",
    "web_execute_js",
    "web_screenshot",
    "web_tabs",
    "web_cdp",
    "web_input",
    "web_network",
    "web_intercept",
    "web_captcha",
    "web_wait",
    "upload",
    "download",
}


def _config(tmp_path: Path, mode: str) -> AgentConfig:
    return AgentConfig(
        llm_backends={
            "default": LLMBackendConfig(
                name="default",
                provider="openai",
                api_key="k",
                api_base="https://api.openai.com/v1",
                model="gpt-test",
            ),
        },
        workspace_dir=str(tmp_path / "ws"),
        memory_dir=str(tmp_path / "mem"),
        browser_tools=mode,
    )


def _runtime(tmp_path: Path, mode: str) -> ExtensionRuntime:
    config = _config(tmp_path, mode)
    return ExtensionRuntime(
        registry=ToolRegistry.with_builtins(config),
        home=tmp_path / "home",
        config=config,
    )


def test_off_registers_no_browser_tools(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "off")

    browser_tools.setup(ExtensionContext("browser_tools", runtime))

    assert runtime.registry.list_all()
    assert not (BROWSER_TOOLS & {tool.name for tool in runtime.registry.list_all()})


def test_on_registers_browser_tools_with_owner(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(browser_tools, "_runtime_available", lambda: True)
    runtime = _runtime(tmp_path, "on")

    browser_tools.setup(ExtensionContext("browser_tools", runtime))
    runtime.track_owner("browser_tools")

    assert {tool.name for tool in runtime.registry.list_all()} == BROWSER_TOOLS | {
        "complete_task",
        "code_run",
        "file_read",
        "file_patch",
        "file_write",
        "find",
        "read_doc",
        "fetch",
        "web_search",
        "update_working_checkpoint",
        "ask_user",
        "start_long_term_update",
    }
    for name in BROWSER_TOOLS:
        assert runtime.registry.owner_of(name) == "browser_tools"

    runtime.release()
    assert not (BROWSER_TOOLS & {tool.name for tool in runtime.registry.list_all()})


def test_auto_skips_when_runtime_is_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(browser_tools, "_runtime_available", lambda: False)
    runtime = _runtime(tmp_path, "auto")

    browser_tools.setup(ExtensionContext("browser_tools", runtime))

    assert not (BROWSER_TOOLS & {tool.name for tool in runtime.registry.list_all()})


def test_auto_registers_when_runtime_is_available(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(browser_tools, "_runtime_available", lambda: True)
    runtime = _runtime(tmp_path, "auto")

    browser_tools.setup(ExtensionContext("browser_tools", runtime))

    assert {tool.name for tool in runtime.registry.list_all()} >= BROWSER_TOOLS


def _agent(tmp_path: Path, mode: str, enabled: bool = True):
    from zero_agent.core.agent import ZeroAgent

    config = _config(tmp_path, mode)
    config.extensions_home = str(tmp_path / "home")
    config.extensions_enabled = enabled
    return ZeroAgent(config)


def test_agent_gates_browser_extension_by_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(browser_tools, "_runtime_available", lambda: True)

    off_agent = _agent(tmp_path, "off")
    try:
        assert not (BROWSER_TOOLS & {tool.name for tool in off_agent.registry.list_all()})
    finally:
        off_agent.close()

    on_agent = _agent(tmp_path, "on")
    try:
        assert {tool.name for tool in on_agent.registry.list_all()} >= BROWSER_TOOLS
    finally:
        on_agent.close()

    disabled_agent = _agent(tmp_path, "on", enabled=False)
    try:
        assert not (
            BROWSER_TOOLS & {tool.name for tool in disabled_agent.registry.list_all()}
        )
    finally:
        disabled_agent.close()
