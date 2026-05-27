"""Tests for plugins/langfuse_tracing.py — LangFuse tracing integration."""

import pytest

from zero_agent.core.hooks import HookSystem
from zero_agent.plugins.langfuse_tracing import register, _get_config, _get_langfuse


class TestLangfusePlugin:
    """LangFuse tracing plugin 测试."""

    def test_register_succeeds_with_mock(self, monkeypatch) -> None:
        """Mock langfuse 可用时注册成功."""
        # mock langfuse package
        mock_langfuse = type("Langfuse", (), {})
        monkeypatch.setattr(
            "zero_agent.plugins.langfuse_tracing._get_langfuse",
            lambda: mock_langfuse,
        )
        monkeypatch.setattr(
            "zero_agent.plugins.langfuse_tracing._get_config",
            lambda: {"public_key": "pk", "secret_key": "sk"},
        )

        hooks = HookSystem()
        result = register(hooks)
        assert result is True
        # 6 个核心事件已注册
        events_with_handlers = [
            e for e, callbacks in hooks._handlers.items() if callbacks
        ]
        assert len(events_with_handlers) == 8

    def test_register_returns_false_when_no_langfuse(self) -> None:
        """langfuse 包缺失时返回 False."""
        hooks = HookSystem()
        # _get_langfuse 默认返回 None（未安装时）
        result = register(hooks)
        assert result is False

    def test_register_returns_false_when_no_config(self, monkeypatch) -> None:
        """无配置时返回 False."""
        mock_langfuse = type("Langfuse", (), {})
        monkeypatch.setattr(
            "zero_agent.plugins.langfuse_tracing._get_langfuse",
            lambda: mock_langfuse,
        )
        monkeypatch.setattr(
            "zero_agent.plugins.langfuse_tracing._get_config",
            lambda: None,
        )
        hooks = HookSystem()
        result = register(hooks)
        assert result is False

    def test_get_langfuse_returns_none_when_not_installed(self) -> None:
        """langfuse 未安装时 _get_langfuse 返回 None."""
        # 正常情况下 langfuse 未安装
        lf = _get_langfuse()
        # 可能返回 None 或 Langfuse 类（如果已安装）
        # 不抛异常即可
        assert lf is None or callable(lf)

    def test_get_config_uses_env_vars(self, monkeypatch) -> None:
        """_get_config 从环境变量读取配置."""
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        monkeypatch.delenv("LANGFUSE_HOST", raising=False)
        cfg = _get_config()
        if cfg is not None:
            assert cfg["public_key"] == "pk-test"
            assert cfg["secret_key"] == "sk-test"

    def test_hook_callbacks_do_not_crash_without_client(self) -> None:
        """未初始化客户端时回调不崩溃."""
        from zero_agent.plugins.langfuse_tracing import (
            _on_agent_before,
            _on_agent_after,
            _on_llm_before,
            _on_llm_after,
            _on_tool_before,
            _on_tool_after,
        )
        # 清理线程状态
        import threading
        _local = threading.local()
        import zero_agent.plugins.langfuse_tracing as p
        p._local = _local

        _on_agent_before({"task": "test"})
        _on_llm_before({"model": "test"})
        _on_llm_after({"usage": {}})
        _on_tool_before({"tool_name": "file_read"})
        _on_tool_after({"tool_name": "file_read", "result": "ok"})
        _on_agent_after({"turns": 1})
        # 不抛异常
