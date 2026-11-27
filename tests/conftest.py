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
        evidence_kind="execute",
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


@pytest.fixture(autouse=True)
def isolated_extension_home(tmp_path, monkeypatch):
    """测试不触碰真实 ~/.zeroagent.

    扩展默认启用且默认 home 是 ``~/.zeroagent``；若开发者本机存在插件或损坏的
    ``mcp.json``，测试结果会随环境漂移。这里把默认 home 固定到临时目录，
    显式传 ``extensions_home`` 的测试不受影响。
    """
    home = tmp_path / "extension-home"
    monkeypatch.setattr(
        "zero_agent.extensions.runtime._default_home",
        lambda: home,
    )
    return home


@pytest.fixture(autouse=True)
def guard_real_user_profile(monkeypatch):
    """测试不得写开发者的真实 ``memory/user_profile.md``.

    桌面 bridge 在模块导入时就创建了单例 ``manager``；只 monkeypatch
    ``load_default_config`` 对它无效，写接口会落到真实 memory 目录，把用户
    画像整份替换成测试里的样例文本（“喜欢黑咖啡”）。这里在写入口直接拦住，
    让漏隔离的测试立刻失败并指出正确写法。
    """
    from pathlib import Path as _Path

    from zero_agent.memory import manager as memory_manager

    real_memory_dir = (_Path(__file__).resolve().parents[1] / "memory").resolve()
    original = memory_manager.MemoryManager.write_user_profile

    def guarded(self, text):
        if _Path(self.memory_dir).resolve() == real_memory_dir:
            raise AssertionError(
                "test tried to overwrite the developer's real memory/user_profile.md; "
                "construct MemoryManager(memory_dir=tmp_path) or rebind the live "
                "bridge manager's memory_dir"
            )
        return original(self, text)

    monkeypatch.setattr(
        memory_manager.MemoryManager, "write_user_profile", guarded,
    )
