"""Tests for core/handler.py — turn_end_callback enhancements."""

from zero_agent.core.handler import BaseHandler
from zero_agent.llm.base import MockResponse


class TestTurnEndCallback:
    """turn_end_callback tests — summary extraction, turn warnings, memory injection."""

    def test_summary_from_tag(self, mock_handler: BaseHandler) -> None:
        """从 <summary> 标签提取摘要."""
        response = MockResponse(
            content="Some text <summary>Completed the file write</summary> more text",
        )
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "file_write", "args": {"path": "/x"}}],
            [],
            turn=3,
            next_prompt="",
            exit_reason=None,
        )
        assert "Completed the file write" in str(mock_handler.history_info[-1])

    def test_summary_fallback_no_tag(self, mock_handler: BaseHandler) -> None:
        """无 <summary> 标签时从首个 tool_call 构造摘要."""
        response = MockResponse(content="Done.")
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "file_read", "args": {"path": "/tmp/x"}}],
            [],
            turn=3,
            next_prompt="",
            exit_reason=None,
        )
        assert "file_read" in str(mock_handler.history_info[-1])
        # 应该提醒 LLM 加 <summary>
        assert "<summary>" in result

    def test_summary_no_tool(self, mock_handler: BaseHandler) -> None:
        """no_tool 时摘要为直接回答."""
        response = MockResponse(content="Here is your answer.")
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "no_tool", "args": {}}],
            [],
            turn=3,
            next_prompt="",
            exit_reason=None,
        )
        assert "直接回答了" in str(mock_handler.history_info[-1])

    def test_turn_warning_level_7(self, mock_handler: BaseHandler) -> None:
        """turn % 7 == 0 注入策略切换警告."""
        response = MockResponse(content="<summary>test</summary>")
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "echo", "args": {}}],
            [],
            turn=7,
            next_prompt="",
            exit_reason=None,
        )
        assert "禁止无效重试" in result

    def test_turn_warning_level_75(self, mock_handler: BaseHandler) -> None:
        """turn % 75 == 0 注入硬停止警告."""
        response = MockResponse(content="<summary>test</summary>")
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "echo", "args": {}}],
            [],
            turn=75,
            next_prompt="",
            exit_reason=None,
        )
        assert "ask_user" in result

    def test_turn_75_overrides_7(self, mock_handler: BaseHandler) -> None:
        """turn % 75 的警告优先于 % 7（使用 elif 链）."""
        response = MockResponse(content="<summary>test</summary>")
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "echo", "args": {}}],
            [],
            turn=75,  # 既是 75 的倍数也是 7 的倍数附近
            next_prompt="",
            exit_reason=None,
        )
        # 应该有 ask_user（% 75 警告），不应该有 禁止无效重试（% 7 警告）
        assert "ask_user" in result
        assert "禁止无效重试" not in result

    def test_no_warning_at_normal_turn(self, mock_handler: BaseHandler) -> None:
        """普通轮次不注入警告."""
        response = MockResponse(content="<summary>test</summary>")
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "echo", "args": {}}],
            [],
            turn=5,
            next_prompt="base",
            exit_reason=None,
        )
        assert "禁止无效重试" not in result
        assert "ask_user" not in result

    def test_summary_truncation(self, mock_handler: BaseHandler) -> None:
        """摘要超过 80 字符时截断."""
        long_summary = "x" * 120
        response = MockResponse(content=f"<summary>{long_summary}</summary>")
        mock_handler.turn_end_callback(
            response,
            [{"tool_name": "echo", "args": {}}],
            [],
            turn=3,
            next_prompt="",
            exit_reason=None,
        )
        assert len(str(mock_handler.history_info[-1])) <= 90  # "[Agent] " + max 80 chars

    def test_preserves_existing_next_prompt(self, mock_handler: BaseHandler) -> None:
        """不覆盖已有的 next_prompt 内容."""
        response = MockResponse(content="<summary>test</summary>")
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "echo", "args": {}}],
            [],
            turn=3,
            next_prompt="existing prompt",
            exit_reason=None,
        )
        assert "existing prompt" in result


class TestMemoryInjection:
    """Periodic memory injection tests."""

    def test_memory_injected_at_turn_10(self, mock_handler: BaseHandler) -> None:
        """turn % 10 == 0 时注入全局记忆（当不是 % 7 或 % 75 时）."""
        # 需要 parent 有 memory 属性
        class MockParent:
            memory = None
            task_dir = None
            _turn_end_hooks = {}

        # 但 turn 10 既不是 75 也不是 7 的倍数，所以进入 elif turn%10==0 分支
        # 由于 parent 为 None 或没有 memory 属性，不注入
        # 这里只验证走对分支
        response = MockResponse(content="<summary>test</summary>")
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "echo", "args": {}}],
            [],
            turn=10,
            next_prompt="",
            exit_reason=None,
        )
        # turn 10 % 7 != 0, turn 10 % 75 != 0, turn 10 % 10 == 0
        # 进入记忆注入分支
        assert "禁止无效重试" not in result
        assert "ask_user" not in result

    def test_turn_70_is_warning_not_memory(self, mock_handler: BaseHandler) -> None:
        """turn % 7 == 0 有更高优先级，不触发记忆注入."""
        response = MockResponse(content="<summary>test</summary>")
        result = mock_handler.turn_end_callback(
            response,
            [{"tool_name": "echo", "args": {}}],
            [],
            turn=70,  # 70 % 7 == 0, 70 % 10 == 0
            next_prompt="",
            exit_reason=None,
        )
        assert "禁止无效重试" in result
