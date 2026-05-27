"""Tests for core/types.py — StepOutcome and TurnResult."""

from zero_agent.core.types import StepOutcome, TurnResult


class TestStepOutcome:
    """StepOutcome dataclass tests."""

    def test_defaults(self) -> None:
        outcome = StepOutcome(data="test")
        assert outcome.data == "test"
        assert outcome.next_prompt is None
        assert outcome.should_exit is False

    def test_next_prompt_empty_string(self) -> None:
        """空字符串 next_prompt 表示继续但上下文最小."""
        outcome = StepOutcome(data={}, next_prompt="")
        assert outcome.next_prompt == ""
        assert outcome.should_exit is False

    def test_next_prompt_custom(self) -> None:
        """自定义 next_prompt 作为下一轮 user message."""
        outcome = StepOutcome(data={}, next_prompt="Continue with step 2")
        assert outcome.next_prompt == "Continue with step 2"

    def test_should_exit(self) -> None:
        """should_exit=True 表示硬退出."""
        outcome = StepOutcome(data={"question": "..."}, should_exit=True)
        assert outcome.should_exit is True
        assert outcome.next_prompt is None

    def test_data_dict(self) -> None:
        outcome = StepOutcome(data={"status": "success", "output": "hello"})
        assert outcome.data["status"] == "success"

    def test_data_str(self) -> None:
        outcome = StepOutcome(data="just a string")
        assert outcome.data == "just a string"


class TestTurnResult:
    """TurnResult dataclass tests."""

    def test_defaults(self) -> None:
        tr = TurnResult(turn=1)
        assert tr.turn == 1
        assert tr.tool_calls == []
        assert tr.tool_results == []
        assert tr.exit_reason is None

    def test_with_data(self) -> None:
        tr = TurnResult(
            turn=3,
            tool_calls=[{"tool_name": "echo", "args": {"message": "hi"}}],
            tool_results=[{"tool_use_id": "call_1", "content": '{"result": "hi"}'}],
            exit_reason={"result": "CURRENT_TASK_DONE", "data": "done"},
        )
        assert len(tr.tool_calls) == 1
        assert len(tr.tool_results) == 1
        assert tr.exit_reason is not None
