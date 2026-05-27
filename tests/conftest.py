"""Shared pytest fixtures for ZeroAgent tests.

Fixtures:
    mock_config: a minimal AgentConfig for testing.
    mock_registry: a ToolRegistry with stub tools.
    mock_handler: a BaseHandler with test tools.
"""

from __future__ import annotations

import pytest

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.handler import BaseHandler
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


@pytest.fixture
def mock_config() -> AgentConfig:
    """创建最小 AgentConfig 用于测试（无需真实 API key）."""
    return AgentConfig(
        llm_backends={
            "default": LLMBackendConfig(
                name="default",
                provider="openai",
                api_key="test-key",
                api_base="https://api.openai.com",
                model="test-model",
            ),
        },
        default_backend="default",
        max_turns=10,
        workspace_dir="/tmp/test-workspace",
        memory_dir="/tmp/test-memory",
    )


@pytest.fixture
def mock_registry() -> ToolRegistry:
    """创建带 stub 工具的 ToolRegistry."""
    registry = ToolRegistry()

    registry.register(ToolDefinition(
        name="echo",
        description="Echo back the message",
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to echo"},
            },
            "required": ["message"],
        },
        handler=_make_echo_handler(),
        category="test",
    ))

    registry.register(ToolDefinition(
        name="add",
        description="Add two numbers",
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First number"},
                "b": {"type": "number", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
        handler=_make_add_handler(),
        category="test",
    ))

    return registry


@pytest.fixture
def mock_handler(mock_registry: ToolRegistry) -> BaseHandler:
    """创建带测试 registry 的 BaseHandler."""
    return BaseHandler(registry=mock_registry, cwd="/tmp/test-workspace")


def _make_echo_handler():
    """创建 echo 工具的 handler."""
    def _handler(args, _response, _handler):
        msg = args.get("message", "")
        yield f"Echo: {msg}\n"
        return {"result": msg}
    return _handler


def _make_add_handler():
    """创建 add 工具的 handler."""
    def _handler(args, _response, _handler):
        a = args.get("a", 0)
        b = args.get("b", 0)
        result = a + b
        yield f"Adding: {a} + {b} = {result}\n"
        return {"result": result}
    return _handler
