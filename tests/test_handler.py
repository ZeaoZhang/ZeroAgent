"""Tests for core/handler.py — BaseHandler dispatch and do_no_tool."""

import pytest

from zero_agent.core.handler import BaseHandler
from zero_agent.core.types import StepOutcome
from zero_agent.llm.base import MockResponse
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


class TestBaseHandlerDispatch:
    """BaseHandler.dispatch() tests."""

    def test_dispatch_do_method(self, mock_handler: BaseHandler) -> None:
        """通过 do_<name> 方法分发."""
        # 注册一个 do_test 方法
        def do_test(self, args, response):
            yield "running test\n"
            return StepOutcome({"result": "ok"}, next_prompt="")

        mock_handler.do_test = do_test.__get__(mock_handler)

        gen = mock_handler.dispatch("test", {}, MockResponse())
        result = _exhaust(gen)
        assert result.data == {"result": "ok"}
        assert result.next_prompt == ""

    def test_dispatch_registry_fallback(self, mock_handler: BaseHandler) -> None:
        """回退到 ToolRegistry 中的 handler."""
        gen = mock_handler.dispatch(
            "echo", {"message": "hello"}, MockResponse(),
        )
        result = _exhaust(gen)
        assert result.data == {"result": "hello"}

    def test_dispatch_unknown_tool(self, mock_handler: BaseHandler) -> None:
        """未知工具返回错误提示."""
        gen = mock_handler.dispatch(
            "nonexistent_tool", {}, MockResponse(),
        )
        result = _exhaust(gen)
        assert result.data is None
        assert "未知工具" in result.next_prompt

    def test_dispatch_injects_meta(self, mock_handler: BaseHandler) -> None:
        """dispatch 注入 _index 和 _tool_num 元信息."""
        captured_args = {}

        def do_capture(self, args, response):
            captured_args.update(args)
            return StepOutcome({"ok": True}, next_prompt="")

        mock_handler.do_capture = do_capture.__get__(mock_handler)

        _exhaust(mock_handler.dispatch(
            "capture", {"custom": "val"}, MockResponse(),
            index=2, tool_num=5,
        ))
        assert captured_args["custom"] == "val"
        assert captured_args["_index"] == 2
        assert captured_args["_tool_num"] == 5

    def test_dispatch_registry_za_next_prompt(self, mock_handler: BaseHandler) -> None:
        """registry tool handler 通过 _za_next_prompt 设置自定义 next_prompt."""
        def custom_handler(args, _response, _handler):
            yield "done\n"
            return {"result": "ok", "_za_next_prompt": "custom prompt"}

        mock_handler.registry.register(ToolDefinition(
            name="custom",
            description="",
            parameters={"type": "object", "properties": {}},
            handler=custom_handler,
        ))

        gen = mock_handler.dispatch("custom", {}, MockResponse())
        result = _exhaust(gen)
        assert result.next_prompt == "custom prompt"

    def test_dispatch_preserves_registry_step_outcome(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """registry handler 可直接返回 StepOutcome 并保留 should_exit."""
        def custom_handler(args, _response, _handler):
            yield "done\n"
            return StepOutcome(
                {"result": "interrupt"},
                next_prompt="",
                should_exit=True,
            )

        mock_handler.registry.register(ToolDefinition(
            name="custom_exit",
            description="",
            parameters={"type": "object", "properties": {}},
            handler=custom_handler,
        ))

        result = _exhaust(mock_handler.dispatch(
            "custom_exit", {}, MockResponse(),
        ))

        assert result.data == {"result": "interrupt"}
        assert result.next_prompt == ""
        assert result.should_exit is True

    def test_real_registry_ask_user_exits(self, mock_config) -> None:
        """真实 ToolRegistry 分发 ask_user 时必须让 loop 退出."""
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=mock_config.workspace_dir)

        result = _exhaust(handler.dispatch(
            "ask_user",
            {"question": "继续吗？", "candidates": ["yes", "no"]},
            MockResponse(),
        ))

        assert result.should_exit is True
        assert result.next_prompt == ""
        assert result.data["status"] == "INTERRUPT"
        assert result.data["data"]["question"] == "继续吗？"


class TestBaseHandlerDoNoTool:
    """BaseHandler.do_no_tool() tests."""

    def test_empty_response_retries(self, mock_handler: BaseHandler) -> None:
        """空响应触发重试."""
        gen = mock_handler.do_no_tool({}, MockResponse(content=""))
        result = _exhaust(gen)
        assert result.next_prompt is not None
        assert "regenerate" in result.next_prompt.lower()

    def test_normal_response_completes(self, mock_handler: BaseHandler) -> None:
        """正常文本回复 → 任务完成（next_prompt=None）."""
        gen = mock_handler.do_no_tool(
            {}, MockResponse(content="Task is done, here is the result."),
        )
        result = _exhaust(gen)
        assert result.next_prompt is None

    def test_incomplete_response_retries(self, mock_handler: BaseHandler) -> None:
        """流异常中断触发重试."""
        content = "some text [!!! 流异常中断 in the end"
        gen = mock_handler.do_no_tool({}, MockResponse(content=content))
        result = _exhaust(gen)
        assert result.next_prompt is not None
        assert "incomplete" in result.next_prompt.lower()

    def test_max_tokens_retries(self, mock_handler: BaseHandler) -> None:
        """max_tokens 截断触发重试."""
        content = "some text max_tokens !!!] in the last part"
        gen = mock_handler.do_no_tool({}, MockResponse(content=content))
        result = _exhaust(gen)
        assert result.next_prompt is not None
        assert "max_tokens" in result.next_prompt.lower()

    def test_code_block_without_tool_triggers_prompt(self, mock_handler: BaseHandler) -> None:
        """大代码块未调用工具时提示 LLM 调用工具."""
        # 需要 50+ 字符的代码内容才能匹配 code_block_pattern
        content = (
            "```python\n"
            + "import os\nimport sys\nimport json\nprint('hello world')\nprint('done')\n"
            + "```"
        )
        gen = mock_handler.do_no_tool({}, MockResponse(content=content))
        result = _exhaust(gen)
        # 应该触发提示
        assert result.next_prompt is not None
        assert "代码" in result.next_prompt

    def test_three_empty_retries_exits(self, mock_handler: BaseHandler) -> None:
        """连续 3 次空响应 → should_exit=True."""
        mock_handler._empty_ct = 2
        result = mock_handler._retry_or_exit("retry")
        # _retry_or_exit 直接返回 StepOutcome（不是 generator）
        assert result.should_exit is True
        assert mock_handler._empty_ct == 3

    def test_retry_or_exit_increments(self, mock_handler: BaseHandler) -> None:
        """_retry_or_exit 递增计数器."""
        mock_handler._empty_ct = 0
        result = mock_handler._retry_or_exit("retry")
        assert result.should_exit is False
        assert mock_handler._empty_ct == 1


class TestBaseHandlerWorking:
    """BaseHandler working memory tests."""

    def test_default_next_prompt_with_key_info(self, mock_handler: BaseHandler) -> None:
        mock_handler.working["key_info"] = "Important context"
        prompt = mock_handler._default_next_prompt({})
        assert "Important context" in prompt
        assert "<key_info>" in prompt

    def test_default_next_prompt_skip(self, mock_handler: BaseHandler) -> None:
        """_index > 0 时仅返回工作记忆，不含 Continue 提示."""
        mock_handler.working["key_info"] = "ctx"
        prompt = mock_handler._default_next_prompt({"_index": 1})
        assert "[System] Continue" not in prompt
        assert "<key_info>ctx</key_info>" in prompt

    def test_build_anchor_prompt_with_history(self, mock_handler: BaseHandler) -> None:
        """_build_anchor_prompt 包含历史和 working memory."""
        mock_handler.history_info = ["[Agent] did something"]
        mock_handler.working["key_info"] = "test ctx"
        anchor = mock_handler._build_anchor_prompt()
        assert "did something" in anchor
        assert "<key_info>test ctx</key_info>" in anchor
        assert "<history>" in anchor

    def test_fold_history_compresses(self, mock_handler: BaseHandler) -> None:
        """_fold_history 压缩连续 agent 轮次."""
        lines = [
            "[Agent] called echo",
            "[Agent] called echo",
            "[Agent] called echo",
            "[USER] new task",
            "[Agent] called add",
        ]
        folded = BaseHandler._fold_history(lines)
        assert "3 turns" in folded
        assert "[USER] new task" in folded


def _exhaust(gen):
    """消费 generator 并返回最终值."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value
