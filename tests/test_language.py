"""Tests for language switching — AgentConfig.resolved_language and tool descriptions."""

import io
from importlib import resources

import pytest

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.exceptions import ConfigError
from zero_agent.core.localization import (
    PROMPT_REPLY_LANGUAGE,
    PROMPT_TASK_CONTROL,
    PromptLocalizer,
)
from zero_agent.tools.registry import ToolRegistry


def test_prompt_localizer_rejects_corrupt_requested_catalog(monkeypatch) -> None:
    original_files = resources.files

    class FakeResource:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def open(self, mode: str = "rb") -> io.BytesIO:
            return io.BytesIO(self._data)

    class FakeTraversable:
        def joinpath(self, *parts: str) -> object:
            path = "/".join(parts)
            if path == "locale/zh/LC_MESSAGES/zero_agent.mo":
                return FakeResource(b"not a GNU mo file")
            if path == "locale/en/LC_MESSAGES/zero_agent.mo":
                return original_files("zero_agent.assets").joinpath(path)
            raise FileNotFoundError(path)

    monkeypatch.setattr(resources, "files", lambda package: FakeTraversable())

    with pytest.raises(ConfigError):
        PromptLocalizer("zh")


def test_prompt_localizer_uses_chinese_catalog() -> None:
    localizer = PromptLocalizer("zh")

    assert localizer.text(PROMPT_REPLY_LANGUAGE) == (
        "按用户的语言回复，或遵循用户明确指定的语言。"
    )
    protocol = localizer.text(PROMPT_TASK_CONTROL)
    assert protocol.startswith("## 任务控制协议")
    assert (
        "调用过任何真实工具后，任务处于 EXECUTING 状态，"
        "必须在任务完成时调用 provider-native"
    ) in protocol

def test_prompt_localizer_uses_english_fallback_for_unknown_language() -> None:
    localizer = PromptLocalizer("fr-FR")

    assert localizer.text(PROMPT_REPLY_LANGUAGE) == (
        "Summarize and reply in user's language or follow user's prompt."
    )
    assert localizer.text(PROMPT_TASK_CONTROL).startswith("## Task control protocol")


class TestResolvedLanguage:
    """AgentConfig.resolved_language — locale-based, for system prompts."""

    def test_explicit_zh(self) -> None:
        config = AgentConfig(language="zh")
        assert config.resolved_language == "zh"

    def test_explicit_en(self) -> None:
        config = AgentConfig(language="en")
        assert config.resolved_language == "en"

    def test_auto_defaults_to_en_without_locale_match(self, monkeypatch) -> None:
        """非中文 locale → en（系统提示词用英文）."""
        monkeypatch.setattr(
            "locale.getlocale", lambda: ("en_US", "UTF-8"),
        )
        config = AgentConfig(
            language="auto",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="gpt-4o",
                ),
            },
        )
        assert config.resolved_language == "en"

    def test_explicit_overrides_locale(self) -> None:
        """显式设置优先于 locale."""
        config = AgentConfig(language="en")
        # 中文 locale 系统上显式 en 仍然返回 en
        assert config.resolved_language == "en"


class TestResolvedToolLanguage:
    """AgentConfig.resolved_tool_language — model-based, for tool schemas.

    工具描述语言由模型类型决定，与系统 locale 无关.
    """

    def test_explicit_zh(self) -> None:
        config = AgentConfig(language="zh")
        assert config.resolved_tool_language == "zh"

    def test_explicit_en(self) -> None:
        config = AgentConfig(language="en")
        assert config.resolved_tool_language == "en"

    def test_chinese_model_glm(self) -> None:
        config = AgentConfig(
            language="auto",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="glm-4-flash",
                ),
            },
        )
        assert config.resolved_tool_language == "zh"

    def test_chinese_model_qwen(self) -> None:
        config = AgentConfig(
            language="auto",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="qwen-max",
                ),
            },
        )
        assert config.resolved_tool_language == "zh"

    def test_chinese_model_deepseek(self) -> None:
        config = AgentConfig(
            language="auto",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="deepseek-chat",
                ),
            },
        )
        assert config.resolved_tool_language == "zh"

    def test_international_model_en(self) -> None:
        """国际模型始终英文，不受 locale 影响."""
        config = AgentConfig(
            language="auto",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="gpt-4o",
                ),
            },
        )
        assert config.resolved_tool_language == "en"

    def test_claude_en(self) -> None:
        config = AgentConfig(
            language="auto",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="claude",
                    api_key="k", api_base="https://api.anthropic.com",
                    model="claude-sonnet-4-6",
                ),
            },
        )
        assert config.resolved_tool_language == "en"

    def test_explicit_overrides_model(self) -> None:
        """显式设置优先于模型检测."""
        config = AgentConfig(
            language="en",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="qwen-max",
                ),
            },
        )
        assert config.resolved_tool_language == "en"


class TestBilingualTools:
    """Tool descriptions are in correct language based on model type.

    Tool language is determined by model type,
    NOT system locale. Chinese models (GLM/MiniMax/Kimi/Qwen/DeepSeek)
    get Chinese descriptions; all others default to English.
    """

    def test_international_model_uses_english_tools(self) -> None:
        """国际模型始终用英文工具描述，不受 locale 影响."""
        config = AgentConfig(
            language="auto",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="gpt-4o",
                ),
            },
            workspace_dir="/tmp/ws",
            memory_dir="/tmp/mem",
        )
        registry = ToolRegistry.with_builtins(config)
        tools = {t.name: t.description for t in registry.list_all()}
        # 国际模型 → 英文描述
        assert "Code executor" in tools["code_run"]
        assert "Read file" in tools["file_read"]
        assert "working notepad" in tools["update_working_checkpoint"].lower()

    def test_chinese_model_uses_chinese_tools(self) -> None:
        """国产模型用中文工具描述."""
        config = AgentConfig(
            language="auto",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="glm-4-flash",
                ),
            },
            workspace_dir="/tmp/ws",
            memory_dir="/tmp/mem",
        )
        registry = ToolRegistry.with_builtins(config)
        tools = {t.name: t.description for t in registry.list_all()}
        assert "执行" in tools["code_run"]
        assert "读取" in tools["file_read"]

    def test_explicit_language_overrides_model(self) -> None:
        """显式 language=en 时国际模型工具也保持英文."""
        config = AgentConfig(
            language="en",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="gpt-4o",
                ),
            },
            workspace_dir="/tmp/ws",
            memory_dir="/tmp/mem",
        )
        registry = ToolRegistry.with_builtins(config)
        tools = {t.name: t.description for t in registry.list_all()}
        assert "Code executor" in tools["code_run"]

    def test_tool_schema_generation(self) -> None:
        """生成的 schema 使用正确语言的描述."""
        config = AgentConfig(
            language="auto",
            llm_backends={
                "default": LLMBackendConfig(
                    name="default", provider="openai",
                    api_key="k", api_base="https://x.com",
                    model="gpt-4o",
                ),
            },
            workspace_dir="/tmp/ws",
            memory_dir="/tmp/mem",
        )
        registry = ToolRegistry.with_builtins(config)
        schema = registry.generate_openai_schema()
        code_run = next(s for s in schema if s["function"]["name"] == "code_run")
        assert "Code executor" in code_run["function"]["description"]
