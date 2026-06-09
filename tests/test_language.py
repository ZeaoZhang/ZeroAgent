"""Tests for language switching — AgentConfig.resolved_language and tool descriptions."""

import os

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.tools.registry import ToolRegistry


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
