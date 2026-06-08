"""Tests for core/loop.py — AgentLoop.

Uses mock LLM client to test the loop flow without real API calls.
"""

from __future__ import annotations

from typing import Any, Dict, Generator, List, Optional

import pytest

from zero_agent.core.handler import BaseHandler
from zero_agent.core.loop import AgentLoop
from zero_agent.core.types import StepOutcome
from zero_agent.llm.base import MockFunction, MockResponse, MockToolCall
from zero_agent.tools.registry import ToolRegistry
from zero_agent.utils.text import smart_format


def _make_mock_client(responses: List[MockResponse]):
    """创建返回预设响应的 mock LLM client."""
    class MockClient:
        def __init__(self):
            self.system = ""
            self.last_tools = ""
            self._responses = list(responses)
            self._call_count = 0

        def chat(
            self,
            messages: List[Dict[str, Any]],
            tools: Optional[List[Dict[str, Any]]] = None,
        ) -> Generator[str, None, MockResponse]:
            if self._call_count >= len(self._responses):
                # 默认返回无工具调用的文本响应
                yield "done"
                return MockResponse(content="Task complete.")
            resp = self._responses[self._call_count]
            self._call_count += 1
            yield resp.content
            return resp

    return MockClient()


class TestAgentLoop:
    """AgentLoop tests."""

    def test_single_turn_completion(self, mock_handler: BaseHandler) -> None:
        """单轮文本回复 → CURRENT_TASK_DONE."""
        client = _make_mock_client([
            MockResponse(content="Task is done, no tools needed."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        gen = loop.run("system prompt", "do something")
        exit_reason = _exhaust(gen)
        assert exit_reason["result"] == "CURRENT_TASK_DONE"

    def test_empty_tool_calls_triggers_no_tool(self, mock_handler: BaseHandler) -> None:
        """LLM 不调用工具时自动触发 do_no_tool."""
        client = _make_mock_client([
            MockResponse(content="Here is my answer."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        gen = loop.run("sp", "task")
        exit_reason = _exhaust(gen)
        assert exit_reason["result"] == "CURRENT_TASK_DONE"

    def test_tool_call_dispatch(self, mock_handler: BaseHandler) -> None:
        """工具调用被正确分发到 handler."""
        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="echo",
                            arguments='{"message": "hello"}',
                        ),
                        id="call_1",
                    ),
                ],
            ),
            MockResponse(content="After tool, task done."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        gen = loop.run("sp", "task")
        exit_reason = _exhaust(gen)
        assert exit_reason["result"] == "CURRENT_TASK_DONE"

    def test_should_exit_tool(self, mock_handler: BaseHandler) -> None:
        """should_exit=True 的工具 → 立即退出.

        ask_user 通过 do_ 方法约定实现 should_exit 行为.
        """
        def do_ask_user(self, args, response):
            yield "asking user\n"
            return StepOutcome(
                {"question": args.get("question", "")},
                should_exit=True,
            )

        mock_handler.do_ask_user = do_ask_user.__get__(mock_handler)

        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="ask_user",
                            arguments='{"question": "proceed?"}',
                        ),
                        id="call_1",
                    ),
                ],
            ),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        gen = loop.run("sp", "task")
        exit_reason = _exhaust(gen)
        assert exit_reason["result"] == "EXITED"

    def test_real_registry_ask_user_exits_loop(self, mock_config) -> None:
        """ask_user 通过真实 registry 分发时让 AgentLoop 返回 EXITED."""
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(
            registry=registry,
            cwd=mock_config.workspace_dir,
        )
        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="ask_user",
                            arguments='{"question": "proceed?"}',
                        ),
                        id="call_1",
                    ),
                ],
            ),
        ])
        loop = AgentLoop(
            client=client,
            handler=handler,
            tools_schema=registry.generate_openai_schema(),
            max_turns=5,
            verbose=False,
        )

        exit_reason = _exhaust(loop.run("sp", "task"))

        assert exit_reason["result"] == "EXITED"
        assert exit_reason["data"]["status"] == "INTERRUPT"
        assert exit_reason["data"]["data"]["question"] == "proceed?"

    def test_max_turns_exceeded(self, mock_handler: BaseHandler) -> None:
        """超出最大轮次限制.

        每轮返回工具调用让循环持续，直到超出 max_turns.
        """
        # 返回多个带工具调用的响应，使循环持续
        responses = []
        for i in range(10):
            responses.append(MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="echo",
                            arguments=f'{{"message": "turn {i}"}}',
                        ),
                        id=f"call_{i}",
                    ),
                ],
            ))
        client = _make_mock_client(responses)
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=2,
            verbose=False,
        )

        gen = loop.run("sp", "task")
        exit_reason = _exhaust(gen)
        assert exit_reason["result"] == "MAX_TURNS_EXCEEDED"

    def test_yield_structure_verbose(self, mock_handler: BaseHandler) -> None:
        """verbose 模式下 yield 的结构."""
        client = _make_mock_client([
            MockResponse(content="Done."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=True,
        )

        gen = loop.run("sp", "task")
        chunks = list(gen)
        # 应该有 turn dict 和状态字符串
        has_turn_dict = any(isinstance(c, dict) and "turn" in c for c in chunks)
        has_status_str = any(isinstance(c, str) for c in chunks)
        assert has_turn_dict
        assert has_status_str

    def test_yield_structure_non_verbose(self, mock_handler: BaseHandler) -> None:
        """非 verbose 模式下 yield 的结构."""
        client = _make_mock_client([
            MockResponse(content="Done."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        gen = loop.run("sp", "task")
        chunks = list(gen)
        has_turn_dict = any(isinstance(c, dict) and "turn" in c for c in chunks)
        assert has_turn_dict

    def test_multi_tool_calls(self, mock_handler: BaseHandler) -> None:
        """单轮多个工具调用."""
        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="echo", arguments='{"message": "a"}',
                        ),
                        id="call_1",
                    ),
                    MockToolCall(
                        function=MockFunction(
                            name="echo", arguments='{"message": "b"}',
                        ),
                        id="call_2",
                    ),
                ],
            ),
            MockResponse(content="Both done."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        gen = loop.run("sp", "task")
        exit_reason = _exhaust(gen)
        assert exit_reason["result"] == "CURRENT_TASK_DONE"

    def test_empty_next_prompt_completes_like_ga(self, mock_handler: BaseHandler) -> None:
        """空 next_prompt 应与 GA 一样直接视为任务完成."""
        def do_empty(self, args, response):
            yield "empty prompt\n"
            return StepOutcome({"result": "ok"}, next_prompt="")

        mock_handler.do_empty = do_empty.__get__(mock_handler)

        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(name="empty", arguments="{}"),
                        id="call_1",
                    ),
                ],
            ),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        exit_reason = _exhaust(loop.run("sp", "task"))

        assert exit_reason["result"] == "CURRENT_TASK_DONE"
        assert client._call_count == 1

    def test_loop_sends_system_once_and_tool_results_as_tool_messages(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """下一轮消息保持 GA 的 tool_results 字段，session 再标准化."""
        client = _make_recording_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="echo", arguments='{"message": "hello"}',
                        ),
                        id="",
                    ),
                ],
            ),
            MockResponse(content="Done."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        exit_reason = _exhaust(loop.run("system prompt", "task"))

        assert exit_reason["result"] == "CURRENT_TASK_DONE"
        assert client.system == "system prompt"
        assert client.calls[0] == [{"role": "user", "content": "task"}]
        assert len(client.calls[1]) == 1
        msg = client.calls[1][0]
        assert msg["role"] == "user"
        assert "### [WORKING MEMORY]" in msg["content"]
        assert "[USER]: task" in msg["content"]
        assert msg["tool_results"] == [
            {"tool_use_id": "call_0", "content": '{"result": "hello"}'}
        ]

    def test_loop_records_initial_user_input_in_handler_history(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        client = _make_mock_client([
            MockResponse(content="Done."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        _exhaust(loop.run("system prompt", "inspect context"))

        assert mock_handler.history_info[0] == "[USER]: inspect context"

    def test_loop_records_initial_user_input_like_ga_compacted_history(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        client = _make_mock_client([
            MockResponse(content="Done."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )
        user_input = "first line\n" + ("x" * 260)

        _exhaust(loop.run("system prompt", user_input))

        expected = smart_format(
            user_input.replace("\n", " "),
            max_str_len=200,
        )
        assert mock_handler.history_info[0] == f"[USER]: {expected}"

    def test_unknown_tool_prompt_clears_tool_protocol_cache(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="missing_tool", arguments="{}",
                        ),
                        id="call_1",
                    ),
                ],
            ),
            MockResponse(content="Done."),
        ])
        client.last_tools = "cached"
        client._last_tools_json = "cached-json"
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        exit_reason = _exhaust(loop.run("system prompt", "task"))

        assert exit_reason["result"] == "CURRENT_TASK_DONE"
        assert client.last_tools == ""
        assert client._last_tools_json == ""

    def test_turn_ten_clears_tool_protocol_cache_field(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        responses = [
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(name="echo", arguments='{"message": "x"}'),
                        id=f"call_{i}",
                    ),
                ],
            )
            for i in range(10)
        ]
        client = _make_mock_client(responses)
        client.last_tools = "cached"
        client._last_tools_json = "cached-json"
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=10,
            verbose=False,
        )

        exit_reason = _exhaust(loop.run("system prompt", "task"))

        assert exit_reason["result"] == "MAX_TURNS_EXCEEDED"
        assert client.last_tools == ""
        assert client._last_tools_json == ""

    def test_bad_json_tool_call_routes_to_bad_json_and_recovers(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="echo", arguments='{"message": "unterminated"',
                        ),
                        id="call_1",
                    ),
                ],
            ),
            MockResponse(content="Recovered."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        exit_reason = _exhaust(loop.run("system prompt", "task"))

        assert exit_reason["result"] == "CURRENT_TASK_DONE"

    def test_multi_tool_results_are_sent_as_separate_messages(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """多工具调用后的 loop payload 保持 GA 的自定义 tool_results."""
        client = _make_recording_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="echo", arguments='{"message": "a"}',
                        ),
                        id="call_1",
                    ),
                    MockToolCall(
                        function=MockFunction(
                            name="echo", arguments='{"message": "b"}',
                        ),
                        id="call_2",
                    ),
                ],
            ),
            MockResponse(content="Both done."),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        exit_reason = _exhaust(loop.run("system prompt", "task"))

        assert exit_reason["result"] == "CURRENT_TASK_DONE"
        assert len(client.calls[1]) == 1
        msg = client.calls[1][0]
        assert msg["role"] == "user"
        assert "### [WORKING MEMORY]" in msg["content"]
        assert "[USER]: task" in msg["content"]
        assert msg["tool_results"] == [
            {"tool_use_id": "call_1", "content": '{"result": "a"}'},
            {"tool_use_id": "call_2", "content": '{"result": "b"}'},
        ]

    def test_done_hook_extends_loop(self, mock_handler: BaseHandler) -> None:
        """_done_hooks 在任务声明完成时追加额外轮次."""
        mock_handler._done_hooks.append("Do one more thing: verify the result.")

        client = _make_mock_client([
            MockResponse(content="Done."),  # 第一轮 → 完成声明
            MockResponse(content="Verified."),  # done hook 触发的第二轮
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        gen = loop.run("sp", "task")
        exit_reason = _exhaust(gen)
        # done hook 被消费后任务正常完成
        assert exit_reason["result"] == "CURRENT_TASK_DONE"


def _exhaust(gen: Generator) -> Any:
    """消费 generator 并返回最终值."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def _make_recording_client(responses: List[MockResponse]):
    """创建记录每次 chat(messages=...) 的 mock LLM client."""
    class RecordingClient:
        def __init__(self):
            self.system = ""
            self.last_tools = ""
            self._responses = list(responses)
            self._call_count = 0
            self.calls: list[list[dict]] = []

        def chat(
            self,
            messages: List[Dict[str, Any]],
            tools: Optional[List[Dict[str, Any]]] = None,
        ) -> Generator[str, None, MockResponse]:
            self.calls.append([dict(m) for m in messages])
            if self._call_count >= len(self._responses):
                yield "done"
                return MockResponse(content="Task complete.")
            resp = self._responses[self._call_count]
            self._call_count += 1
            if resp.content:
                yield resp.content
            return resp

    return RecordingClient()
