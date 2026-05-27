"""Tests for core/exceptions.py — exception hierarchy."""

import pytest

from zero_agent.core.exceptions import ConfigError, LLMError, ToolError, ZeroAgentError


class TestZeroAgentError:
    """Base exception tests."""

    def test_is_exception(self) -> None:
        err = ZeroAgentError("test")
        assert isinstance(err, Exception)

    def test_message(self) -> None:
        err = ZeroAgentError("something went wrong")
        assert str(err) == "something went wrong"


class TestConfigError:
    """ConfigError tests."""

    def test_is_zero_agent_error(self) -> None:
        err = ConfigError("bad config")
        assert isinstance(err, ZeroAgentError)

    def test_catch_as_zero_agent_error(self) -> None:
        try:
            raise ConfigError("missing api_key")
        except ZeroAgentError as e:
            assert "api_key" in str(e)


class TestLLMError:
    """LLMError tests."""

    def test_is_zero_agent_error(self) -> None:
        err = LLMError("api call failed")
        assert isinstance(err, ZeroAgentError)

    def test_can_catch_as_base(self) -> None:
        try:
            raise LLMError("timeout")
        except ZeroAgentError:
            pass  # should catch

    def test_chained_exception(self) -> None:
        cause = ConnectionError("refused")
        err = LLMError("LLM call failed")
        err.__cause__ = cause
        assert err.__cause__ is cause


class TestToolError:
    """ToolError tests."""

    def test_is_zero_agent_error(self) -> None:
        err = ToolError("tool execution failed")
        assert isinstance(err, ZeroAgentError)

    def test_tool_name_in_message(self) -> None:
        err = ToolError("code_run failed: timeout")
        assert "code_run" in str(err)
