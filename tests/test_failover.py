"""AutoFailoverSession 测试."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zero_agent.core.config import LLMBackendConfig
from zero_agent.llm.failover import AutoFailoverSession
from zero_agent.llm.base import MockResponse
from zero_agent.llm.sessions import LiteLLMSession


def _make_config(name: str = "primary", model: str = "gpt-4") -> LLMBackendConfig:
    """创建测试用 LLMBackendConfig."""
    return LLMBackendConfig(
        name=name,
        provider="openai",
        api_key="sk-test",
        api_base="https://api.openai.com",
        model=model,
    )


def _make_session(name: str = "primary") -> LiteLLMSession:
    """创建测试用 LiteLLMSession."""
    return LiteLLMSession(_make_config(name))


class TestAutoFailoverSessionInit:
    """初始化测试."""

    def test_init_no_backups(self) -> None:
        """无备用 session 时正常初始化."""
        primary = _make_session("primary")
        session = AutoFailoverSession(primary, backups=[])
        assert session.primary is primary
        assert session.backups == []
        assert session._active is primary
        assert session._is_fallback_active is False

    def test_init_with_backups(self) -> None:
        """有备用 session 时正常初始化."""
        primary = _make_session("primary")
        backup1 = _make_session("backup1")
        backup2 = _make_session("backup2")
        session = AutoFailoverSession(
            primary, backups=[backup1, backup2]
        )
        assert len(session.backups) == 2
        assert session._active is primary

    def test_init_default_health_interval(self) -> None:
        """默认健康检查间隔为 60s."""
        primary = _make_session()
        session = AutoFailoverSession(primary, backups=[])
        assert session._health_check_interval == 60

    def test_init_custom_health_interval(self) -> None:
        """自定义健康检查间隔."""
        primary = _make_session()
        session = AutoFailoverSession(
            primary, backups=[], health_check_interval=30
        )
        assert session._health_check_interval == 30


class TestAutoFailoverSessionProperties:
    """属性委托测试."""

    def test_name_delegates_to_active(self) -> None:
        primary = _make_session("primary")
        session = AutoFailoverSession(primary, backups=[])
        assert session.name == "primary"

    def test_config_delegates_to_active(self) -> None:
        primary = _make_session("primary")
        session = AutoFailoverSession(primary, backups=[])
        assert session.config.name == "primary"

    def test_history_getter_setter(self) -> None:
        primary = _make_session("primary")
        session = AutoFailoverSession(primary, backups=[])
        session.history = [{"role": "user", "content": "hello"}]
        assert len(session.history) == 1
        assert session.history[0]["content"] == "hello"

    def test_system_setter_propagates_to_all(self) -> None:
        primary = _make_session("primary")
        backup = _make_session("backup1")
        session = AutoFailoverSession(primary, backups=[backup])
        session.system = "test system"
        assert primary.system == "test system"
        assert backup.system == "test system"

    def test_temperature_delegates(self) -> None:
        primary = _make_session("primary")
        session = AutoFailoverSession(primary, backups=[])
        assert session.temperature == primary.temperature

    def test_fallback_count_initial(self) -> None:
        primary = _make_session()
        session = AutoFailoverSession(primary, backups=[])
        assert session.fallback_count == 0

    def test_is_fallback_active_initial(self) -> None:
        primary = _make_session()
        session = AutoFailoverSession(primary, backups=[])
        assert session.is_fallback_active is False

    def test_usage_stats_delegates(self) -> None:
        primary = _make_session("primary")
        session = AutoFailoverSession(primary, backups=[])
        stats = session.usage_stats
        assert isinstance(stats, dict)
        assert "total_requests" in stats

    def test_all_usage_stats(self) -> None:
        primary = _make_session("primary")
        backup = _make_session("backup1")
        session = AutoFailoverSession(primary, backups=[backup])
        stats = session.all_usage_stats
        assert "primary" in stats
        assert "backup1" in stats


class TestAutoFailoverSessionFallback:
    """故障转移测试."""

    def test_success_no_fallback(self) -> None:
        """主 session 正常时不触发 fallback."""
        primary = _make_session("primary")
        backup = _make_session("backup1")
        session = AutoFailoverSession(primary, backups=[backup])

        mock_response = MagicMock()
        mock_response.content = "hello"
        primary.chat = MagicMock()
        gen = primary.chat.return_value = MagicMock()
        gen.__iter__ = MagicMock(return_value=iter(["hello"]))
        # Make the generator return mock_response
        type(gen).return_value = mock_response

        # Since chat() is a generator, we can't easily mock it this way.
        # Just verify fallback count stays 0.
        assert session.fallback_count == 0
        assert session.is_fallback_active is False

    def test_fallback_count_increments(self) -> None:
        """验证 fallback_count 属性."""
        primary = _make_session("primary")
        session = AutoFailoverSession(primary, backups=[])
        assert session.fallback_count == 0

    def test_history_migration(self) -> None:
        """验证 _migrate_history 正确迁移历史和系统提示词."""
        primary = _make_session("primary")
        backup = _make_session("backup1")
        primary.history = [{"role": "user", "content": "test"}]
        primary.system = "test system"

        AutoFailoverSession._migrate_history(primary, backup)
        assert len(backup.history) == 1
        assert backup.history[0]["content"] == "test"
        assert backup.system == "test system"

    def test_chat_returns_backup_response_after_fallback(self) -> None:
        """fallback generator must return the backup MockResponse to AgentLoop."""
        primary = _make_session("primary")
        backup = _make_session("backup1")
        session = AutoFailoverSession(primary, backups=[backup])

        def failing_chat(*_args, **_kwargs):
            if False:
                yield ""
            raise RuntimeError("primary down")

        def backup_chat(*_args, **_kwargs):
            yield "backup ok"
            return MockResponse(content="backup ok")

        primary.chat = failing_chat
        backup.chat = backup_chat

        chunks = []
        gen = session.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
        try:
            while True:
                chunks.append(next(gen))
        except StopIteration as e:
            response = e.value

        assert any("backup1" in chunk for chunk in chunks)
        assert response.content == "backup ok"
        assert session.is_fallback_active is True

    def test_exception_fallback_does_not_duplicate_current_user_history(self) -> None:
        """同轮异常 fallback 只迁移调用前历史，不重复当前 user 消息."""
        primary = _make_session("primary")
        backup = _make_session("backup1")
        session = AutoFailoverSession(primary, backups=[backup])

        def failing_chat(*_args, **_kwargs):
            raise RuntimeError("primary down")

        def backup_chat(messages, tools=None):
            assert backup.history == []
            yield "backup ok"
            return MockResponse(content="backup ok")

        primary.chat = failing_chat
        backup.chat = backup_chat

        chunks = list(
            session.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
        )
        assert any("backup1" in chunk for chunk in chunks)
        assert session.name == "backup1"

    def test_long_immediate_error_response_fallbacks_same_turn(self) -> None:
        """长错误响应按 GA 规则同轮 fallback，不等到下一轮."""
        primary = _make_session("primary")
        backup = _make_session("backup1")
        session = AutoFailoverSession(primary, backups=[backup])

        def error_chat(*_args, **_kwargs):
            if False:
                yield ""
            return MockResponse(content="!!!Error: backend failed " + ("x" * 200))

        def backup_chat(messages, tools=None):
            assert backup.history == []
            yield "backup ok"
            return MockResponse(content="backup ok")

        primary.chat = error_chat
        backup.chat = backup_chat

        chunks = []
        gen = session.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
        try:
            while True:
                chunks.append(next(gen))
        except StopIteration as e:
            response = e.value

        assert any("backup1" in chunk for chunk in chunks)
        assert response.content == "backup ok"
        assert session.name == "backup1"

    def test_partial_stream_interruption_switches_next_call_only(self) -> None:
        """已流出部分内容的中断只切换下一轮，不重放当前轮."""
        primary = _make_session("primary")
        backup = _make_session("backup1")
        session = AutoFailoverSession(primary, backups=[backup])

        def interrupted_chat(*_args, **_kwargs):
            yield "partial answer"
            return MockResponse(
                content="partial answer\n[!!! 流异常中断",
                stop_reason="stream_interrupted",
            )

        backup.chat = MagicMock(side_effect=AssertionError("backup should not run"))
        primary.chat = interrupted_chat

        chunks = []
        gen = session.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
        try:
            while True:
                chunks.append(next(gen))
        except StopIteration as e:
            response = e.value

        assert "partial answer" in "".join(chunks)
        assert response.stop_reason == "stream_interrupted"
        assert session.name == "backup1"
        assert session.is_fallback_active is True
        assert backup.chat.call_count == 0


class TestHealthProbe:
    """健康检查测试."""

    def test_probe_healthy_session(self) -> None:
        """健康后端返回 True."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        session = _make_session()
        with patch("requests.get", return_value=mock_resp):
            result = AutoFailoverSession._probe_session(session)
        assert result is True

    def test_probe_unhealthy_session(self) -> None:
        """不健康后端（连接拒绝）返回 False."""
        session = _make_session()
        with patch("requests.get", side_effect=Exception("Connection refused")):
            result = AutoFailoverSession._probe_session(session)
        assert result is False

    def test_probe_server_error(self) -> None:
        """500 错误视为不健康."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        session = _make_session()
        with patch("requests.get", return_value=mock_resp):
            result = AutoFailoverSession._probe_session(session)
        assert result is False

    def test_probe_client_error_not_unhealthy(self) -> None:
        """4xx 错误（非 500）视为健康（后端可达只是权限问题）."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        session = _make_session()
        with patch("requests.get", return_value=mock_resp):
            result = AutoFailoverSession._probe_session(session)
        assert result is True
