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
def isolate_project_runtime_dirs(tmp_path, monkeypatch):
    """Keep every test out of the developer's real runtime directories.

    - ``desktop_bridge`` builds its AgentManager lazily; drop any instance so a
      test can only build one from its own patched config.
    - ``continue_cmd`` writes ``model_responses_<pid>.txt`` into its sessions
      dir; a module reload inside test_bots used to reset it to the real one.
    - ``workspace_cmd`` keeps ``temp/workspaces.json`` under ``project_root()``.
    """
    import dataclasses

    from zero_agent.bots.shared import continue_cmd
    from zero_agent.frontends import desktop_bridge
    from zero_agent.frontends.commands import workspace_cmd

    # Do not create the directory: tests assert tmp_path stays empty, and the
    # writers create their sessions dir on demand.
    continue_cmd.set_sessions_dir(str(tmp_path / "continue-sessions"))
    monkeypatch.setattr(
        workspace_cmd, "project_root", lambda: tmp_path / "project",
    )
    # The bridge singleton is built on first use (e.g. emit_session_state), so
    # point its config loader at tmp as well even in tests that do not touch it.
    isolated = dataclasses.replace(
        desktop_bridge.load_default_config(),
        workspace_dir=str(tmp_path / "workspace"),
        memory_dir=str(tmp_path / "memory"),
        sessions_dir=str(tmp_path / "sessions"),
    )
    monkeypatch.setattr(desktop_bridge, "load_default_config", lambda: isolated)
    type(desktop_bridge.manager)._instance = None


@pytest.fixture(autouse=True)
def redirect_real_response_log(monkeypatch, tmp_path):
    """Keep response logs out of the developer's real workspace/sessions.

    Many AgentConfig fixtures use the ``./workspace/sessions`` default; the
    historical logs they produced are what littered the project directory and
    made ``/continue`` list pytest runs. Redirect such writes to tmp instead of
    failing, because the log payload itself is what the test exercises.
    """
    import os
    from pathlib import Path as _Path

    from zero_agent.llm import sessions as llm_sessions

    real_sessions = (
        _Path(__file__).resolve().parents[1] / "workspace" / "sessions"
    ).resolve()
    original = llm_sessions.LiteLLMSession._write_model_response_log

    def guarded(self, messages, mock):
        base = self._session_log_path or self._sessions_dir
        if base:
            resolved = _Path(str(base)).expanduser().resolve()
            if resolved.name != "model_responses_%d.txt" % os.getpid():
                resolved = resolved / ("model_responses_%d.txt" % os.getpid())
            if real_sessions == resolved.parent or real_sessions in resolved.parents:
                redirected = tmp_path / "llm-logs"
                redirected.mkdir(parents=True, exist_ok=True)
                self._session_log_path = None
                self._sessions_dir = str(redirected)
        return original(self, messages, mock)

    monkeypatch.setattr(
        llm_sessions.LiteLLMSession, "_write_model_response_log", guarded,
    )


@pytest.fixture(autouse=True)
def guard_real_session_store(monkeypatch):
    """Fail a test that opens the developer's real session store."""
    from pathlib import Path as _Path

    from zero_agent.frontends import session_store as store_mod

    real_workspace = (_Path(__file__).resolve().parents[1] / "workspace").resolve()
    original = store_mod.SessionStore.__init__

    def guarded(self, path, *args, **kwargs):
        resolved = _Path(str(path)).expanduser().resolve()
        if resolved == real_workspace or real_workspace in resolved.parents:
            raise AssertionError(
                "test opened the developer's real workspace/sessions store; "
                "isolate the manager's sessions_dir"
            )
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(store_mod.SessionStore, "__init__", guarded)


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
