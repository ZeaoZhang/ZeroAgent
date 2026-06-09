"""Tests for tools/registry.py — ToolDefinition and ToolRegistry."""

import pytest

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def _stub_handler(args, _response, _handler):
    yield "ok"
    return {"result": "ok"}


class TestToolDefinition:
    """ToolDefinition dataclass tests."""

    def test_create(self) -> None:
        td = ToolDefinition(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
            handler=_stub_handler,
        )
        assert td.name == "test_tool"
        assert td.category == "general"

    def test_custom_category(self) -> None:
        td = ToolDefinition(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
            handler=_stub_handler,
            category="custom",
        )
        assert td.category == "custom"


class TestToolRegistry:
    """ToolRegistry tests."""

    def test_register_and_get(self) -> None:
        registry = ToolRegistry()
        td = ToolDefinition(
            name="my_tool",
            description="desc",
            parameters={"type": "object", "properties": {}},
            handler=_stub_handler,
        )
        registry.register(td)
        assert registry.get("my_tool") is td

    def test_get_nonexistent(self) -> None:
        registry = ToolRegistry()
        assert registry.get("nonexistent") is None

    def test_register_overwrites(self) -> None:
        registry = ToolRegistry()
        td1 = ToolDefinition(
            name="tool", description="v1",
            parameters={}, handler=_stub_handler,
        )
        td2 = ToolDefinition(
            name="tool", description="v2",
            parameters={}, handler=_stub_handler,
        )
        registry.register(td1)
        registry.register(td2)
        assert registry.get("tool").description == "v2"

    def test_list_all(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="a", description="", parameters={}, handler=_stub_handler,
        ))
        registry.register(ToolDefinition(
            name="b", description="", parameters={}, handler=_stub_handler,
        ))
        tools = registry.list_all()
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"a", "b"}

    def test_list_all_empty(self) -> None:
        registry = ToolRegistry()
        assert registry.list_all() == []

    def test_list_by_category(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="t1", description="", parameters={},
            handler=_stub_handler, category="cat_a",
        ))
        registry.register(ToolDefinition(
            name="t2", description="", parameters={},
            handler=_stub_handler, category="cat_b",
        ))
        registry.register(ToolDefinition(
            name="t3", description="", parameters={},
            handler=_stub_handler, category="cat_a",
        ))
        assert len(registry.list_by_category("cat_a")) == 2
        assert len(registry.list_by_category("cat_b")) == 1
        assert len(registry.list_by_category("nonexistent")) == 0

    def test_generate_openai_schema(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="echo",
            description="Echo back",
            parameters={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
            },
            handler=_stub_handler,
        ))
        schema = registry.generate_openai_schema()
        assert len(schema) == 1
        assert schema[0]["type"] == "function"
        assert schema[0]["function"]["name"] == "echo"

    def test_generate_claude_schema(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="echo",
            description="Echo back",
            parameters={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
            },
            handler=_stub_handler,
        ))
        schema = registry.generate_claude_schema()
        assert len(schema) == 1
        assert schema[0]["name"] == "echo"
        assert "input_schema" in schema[0]

    def test_generate_openai_schema_empty(self) -> None:
        registry = ToolRegistry()
        assert registry.generate_openai_schema() == []

    def test_with_builtins_registers_only_core_tools(self) -> None:
        """默认内置工具只包含 ZeroAgent 核心原子工具."""
        config = AgentConfig(
            llm_backends={
                "default": LLMBackendConfig(
                    name="default",
                    provider="openai",
                    api_key="test-key",
                    api_base="https://api.openai.com/v1",
                    model="test-model",
                ),
            },
        )

        registry = ToolRegistry.with_builtins(config)
        names = {tool.name for tool in registry.list_all()}

        assert names == {
            "code_run",
            "file_read",
            "file_patch",
            "file_write",
            "web_scan",
            "web_execute_js",
            "update_working_checkpoint",
            "ask_user",
            "start_long_term_update",
        }
        assert "search_web" not in names
        assert "vision" not in names
        assert "memory_plot" not in names
        assert "send_im" not in names
