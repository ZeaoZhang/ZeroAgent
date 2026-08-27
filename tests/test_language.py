"""Tests for language switching — AgentConfig.resolved_language and tool descriptions."""

import io
from importlib import resources

import pytest

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.exceptions import ConfigError
from zero_agent.core.localization import PROMPT_REPLY_LANGUAGE, PromptLocalizer
from zero_agent.core.types import TaskMode
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


def test_prompt_localizer_falls_back_per_message_to_english(monkeypatch) -> None:
    class FakeTranslations:
        def __init__(self, messages: dict[str, str]) -> None:
            self.messages = messages
            self.fallback = None

        def add_fallback(self, fallback) -> None:
            self.fallback = fallback

        def gettext(self, message_id: str) -> str:
            if message_id in self.messages:
                return self.messages[message_id]
            if self.fallback is not None:
                return self.fallback.gettext(message_id)
            return message_id

    class FakeResource:
        def __init__(self, language: str) -> None:
            self.language = language

        def open(self, mode: str = "rb") -> io.BytesIO:
            return io.BytesIO(self.language.encode())

    class FakeTraversable:
        def joinpath(self, *parts: str) -> FakeResource:
            return FakeResource(parts[1])

    catalogs = {
        "zh": FakeTranslations({"known": "已翻译"}),
        "en": FakeTranslations({"known": "translated", "new": "English fallback"}),
    }
    monkeypatch.setattr(resources, "files", lambda _package: FakeTraversable())
    monkeypatch.setattr(
        "zero_agent.core.localization.gettext.GNUTranslations",
        lambda stream: catalogs[stream.read().decode()],
    )

    localizer = PromptLocalizer("zh")

    assert localizer.text("known") == "已翻译"
    assert localizer.text("new") == "English fallback"


@pytest.mark.parametrize(
    ("language", "reply_text", "state_text"),
    [
        (
            "zh",
            "使用用户当前使用的语言回复，除非用户明确指定其他语言。",
            "无需工具即可完整回答时可直接给出最终答复。",
        ),
        (
            "en",
            "Reply in the language the user is currently using unless they explicitly request another.",
            "When no tools are needed, provide the complete final answer directly.",
        ),
    ],
)

def test_prompt_localizer_uses_language_consistent_task_contracts(
    language: str,
    reply_text: str,
    state_text: str,
) -> None:
    localizer = PromptLocalizer(language)

    assert localizer.text(PROMPT_REPLY_LANGUAGE) == reply_text
    protocol = localizer.task_control(TaskMode.OPEN)
    assert protocol.startswith('<task_control state="open">')
    assert state_text in protocol
    assert protocol.endswith("</task_control>")


def test_prompt_localizer_uses_english_fallback_for_unknown_language() -> None:
    localizer = PromptLocalizer("fr-FR")

    assert localizer.text(PROMPT_REPLY_LANGUAGE).startswith("Reply in the language")
    assert localizer.task_control(TaskMode.OPEN).startswith(
        '<task_control state="open">'
    )


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        (TaskMode.OPEN, "open"),
        (TaskMode.EXECUTING, "executing"),
        (TaskMode.PLAN, "plan"),
    ],
)
def test_task_control_catalogs_keep_machine_identifiers_in_english(
    mode: TaskMode,
    state: str,
) -> None:
    for language in ("zh", "en"):
        protocol = PromptLocalizer(language).task_control(mode)
        assert protocol.startswith(f'<task_control state="{state}">')
        assert protocol.endswith("</task_control>")


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

def test_resolved_tool_language_can_follow_each_backend() -> None:
    config = AgentConfig(
        language="auto",
        llm_backends={
            "international": LLMBackendConfig(
                name="international",
                provider="openai",
                api_key="k",
                api_base="https://x.com",
                model="gpt-4o",
            ),
            "chinese": LLMBackendConfig(
                name="chinese",
                provider="openai",
                api_key="k",
                api_base="https://x.com",
                model="qwen-max",
            ),
        },
    )

    assert config.resolved_tool_language_for_backend("international") == "en"
    assert config.resolved_tool_language_for_backend("chinese") == "zh"


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
