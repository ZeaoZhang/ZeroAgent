"""Tests for core/loop.py — AgentLoop.

Uses mock LLM client to test the loop flow without real API calls.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from zero_agent.core.handler import BaseHandler
from zero_agent.core.loop import AgentLoop
from zero_agent.core.types import StepAction, StepOutcome, TaskContract, TaskMode, TerminalStatus
from zero_agent.llm.base import MockFunction, MockResponse, MockToolCall
from zero_agent.tools.registry import ToolDefinition, ToolRegistry
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

def _complete_response(answer: str, evidence_refs: list[int]) -> MockResponse:
    return MockResponse(
        tool_calls=[MockToolCall(
            function=MockFunction(
                name="complete_task",
                arguments=json.dumps({"answer": answer, "evidence_refs": evidence_refs}),
            ),
            id="call_complete",
        )],
    )


def _set_open_contract(handler: BaseHandler) -> None:
    handler.task_contract = TaskContract(
        task_id=handler.task_contract.task_id,
        user_request=handler.task_contract.user_request,
        mode=TaskMode.OPEN,
        plan_path=handler.task_contract.plan_path,
    )


def _add_success_evidence(handler: BaseHandler) -> None:
    handler._record_evidence(
        "file_read",
        {"path": "config.py"},
        StepOutcome("content", next_prompt="continue"),
    )

def _set_execution_contract(handler: BaseHandler) -> None:
    handler.task_contract = TaskContract(
        task_id=handler.task_contract.task_id,
        user_request=handler.task_contract.user_request,
        mode=TaskMode.EXECUTING,
        plan_path=handler.task_contract.plan_path,
    )



class TestAgentLoop:
    """AgentLoop tests."""

    def test_single_turn_completion(self, mock_handler: BaseHandler) -> None:
        """A single deliverable text response returns completed."""
        _set_open_contract(mock_handler)
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
        terminal = _exhaust(gen)
        assert terminal.status is TerminalStatus.COMPLETED

    def test_empty_tool_calls_triggers_no_tool(self, mock_handler: BaseHandler) -> None:
        """LLM 不调用工具时自动触发 do_no_tool."""
        _set_open_contract(mock_handler)
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
        terminal = _exhaust(gen)
        assert terminal.status is TerminalStatus.COMPLETED

    def test_tool_call_dispatch(self, mock_handler: BaseHandler) -> None:
        """工具调用被正确分发到 handler."""
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)
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
            _complete_response("After tool, task done.", [1]),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        gen = loop.run("sp", "task")
        terminal = _exhaust(gen)
        assert terminal.status is TerminalStatus.COMPLETED

    def test_invalid_completion_recovers_without_exposing_control_payload(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)
        client = _make_recording_client([
            _complete_response("accepted answer", [99]),
            _complete_response("accepted answer", [1]),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=10,
            verbose=True,
        )

        chunks, terminal = _drain(loop.run("system prompt", "task"))
        visible = "".join(chunk for chunk in chunks if isinstance(chunk, str))

        assert len(client.calls) == 2
        assert terminal.status is TerminalStatus.COMPLETED
        assert terminal.text == "accepted answer"
        assert "Available successful evidence_refs:" in client.calls[1][0]["content"]
        assert "ref=1" in client.calls[1][0]["content"]
        assert client.calls[1][0]["tool_results"] == [
            {"tool_use_id": "call_complete", "content": "{}"},
        ]
        assert visible.count("accepted answer") == 1
        for hidden in ("Tool:", "complete_task", "evidence_refs", "```"):
            assert hidden not in visible

    def test_invalid_completion_stops_at_protocol_retry_limit(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)
        client = _make_recording_client([
            _complete_response("Done", [99]),
            _complete_response("Done", [99]),
            _complete_response("Done", [99]),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=10,
            verbose=True,
        )

        _, terminal = _drain(loop.run("system prompt", "task"))

        assert len(client.calls) == 3
        assert terminal.status is TerminalStatus.PROTOCOL_ERROR
        assert terminal.reason == "complete_task_retry_limit"

    def test_wait_for_user_tool(self, mock_handler: BaseHandler) -> None:
        """WAIT_FOR_USER returns the original payload as a waiting terminal."""

        def do_ask_user(self, args, response):
            yield "asking user\n"
            return StepOutcome(
                {"question": args.get("question", "")},
                action=StepAction.WAIT_FOR_USER,
                reason="human_intervention",
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
        terminal = _exhaust(gen)
        assert terminal.status is TerminalStatus.WAITING
        assert terminal.reason == "human_intervention"
        assert terminal.data == {"question": "proceed?"}

    def test_real_registry_ask_user_waits(self, mock_config) -> None:
        """The built-in ask_user payload is preserved in a waiting terminal."""
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

        terminal = _exhaust(loop.run("sp", "task"))

        assert terminal.status is TerminalStatus.WAITING
        assert terminal.reason == "human_intervention"
        assert terminal.data["status"] == "INTERRUPT"
        assert terminal.data["data"]["question"] == "proceed?"

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
        terminal = _exhaust(gen)
        assert terminal.status is TerminalStatus.BUDGET_EXHAUSTED
        assert terminal.reason == "max_turns"

    def test_yield_structure_verbose(self, mock_handler: BaseHandler) -> None:
        """verbose 模式下 yield 的结构."""
        _set_open_contract(mock_handler)
        client = _make_mock_client([
            _complete_response("Done.", []),
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
        _set_open_contract(mock_handler)
        client = _make_mock_client([
            _complete_response("Done.", []),
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
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)
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
            _complete_response("Both done.", [1]),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        gen = loop.run("sp", "task")
        terminal = _exhaust(gen)
        assert terminal.status is TerminalStatus.COMPLETED

    def test_duplicate_tool_calls_are_dispatched_in_order(self) -> None:
        """同轮重复工具调用应按模型返回顺序逐个分发。"""
        calls: list[str] = []
        registry = ToolRegistry()

        def record_handler(args, _response, _handler):
            calls.append(args["path"])
            yield f"wrote {args['path']}\n"
            return {"status": "success"}

        registry.register(ToolDefinition(
            name="file_write",
            description="记录写入调用",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=record_handler,
        ))
        handler = BaseHandler(registry=registry, cwd="/tmp/test-workspace")
        _set_execution_contract(handler)
        _add_success_evidence(handler)
        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(name="file_write", arguments='{"path": "same.txt"}'),
                        id="call_1",
                    ),
                    MockToolCall(
                        function=MockFunction(name="file_write", arguments='{"path": "same.txt"}'),
                        id="call_2",
                    ),
                ],
            ),
            _complete_response("Done.", [1]),
        ])
        loop = AgentLoop(
            client=client,
            handler=handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        terminal = _exhaust(loop.run("sp", "task"))

        assert terminal.status is TerminalStatus.COMPLETED
        assert calls == ["same.txt", "same.txt"]

    def test_invalid_empty_continue_is_protocol_error(self, mock_handler: BaseHandler) -> None:
        """CONTINUE without a non-empty prompt violates the step contract."""

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

        terminal = _exhaust(loop.run("sp", "task"))

        assert terminal.status is TerminalStatus.PROTOCOL_ERROR
        assert terminal.reason == "invalid_step_outcome"
        assert client._call_count == 1

    def test_loop_sends_system_once_and_tool_results_as_tool_messages(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """下一轮消息保持 tool_results 字段，session 再标准化."""
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)
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
            _complete_response("Done.", [1]),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        terminal = _exhaust(loop.run("system prompt", "task"))

        assert terminal.status is TerminalStatus.COMPLETED
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
        _set_open_contract(mock_handler)
        client = _make_mock_client([
            _complete_response("Done.", [1]),
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

    def test_loop_records_initial_user_input_in_compacted_history(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_open_contract(mock_handler)
        client = _make_mock_client([
            _complete_response("Done.", [1]),
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


    def test_usage_from_response_overlays_canonical_cache_aliases(self) -> None:
        response = MockResponse(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
                "input_tokens": 1,
            }
        )
        usage = AgentLoop._usage_from_response(response)
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 20
        assert usage["cache_read_input_tokens"] == 80
        assert usage["cache_miss_input_tokens"] == 20
        assert usage["cache_metrics_available"] is True
        assert usage["prompt_tokens"] == 100

    def test_usage_from_response_reads_object_shaped_nested_cache_details(self) -> None:
        response = MockResponse(usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=80),
        ))
        usage = AgentLoop._usage_from_response(response)
        assert usage == {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 0,
            "cache_miss_input_tokens": 20,
            "cache_metrics_available": True,
        }

    def test_unknown_tool_prompt_clears_tool_protocol_cache(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _add_success_evidence(mock_handler)
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
            _complete_response("Done.", [1]),
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

        terminal = _exhaust(loop.run("system prompt", "task"))

        assert terminal.status is TerminalStatus.COMPLETED
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

        terminal = _exhaust(loop.run("system prompt", "task"))

        assert terminal.status is TerminalStatus.BUDGET_EXHAUSTED
        assert terminal.reason == "max_turns"
        assert client.last_tools == ""
        assert client._last_tools_json == ""

    def test_bad_json_tool_call_routes_to_bad_json_and_recovers(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _add_success_evidence(mock_handler)
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

        terminal = _exhaust(loop.run("system prompt", "task"))

        assert terminal.status is TerminalStatus.COMPLETED

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            ("[]", "got list"),
            ("null", "got null"),
            ('"raw"', "got string"),
            ("123", "got number"),
        ],
    )
    def test_native_non_object_tool_arguments_route_to_bad_json(
        self,
        mock_handler: BaseHandler,
        arguments: str,
        expected: str,
    ) -> None:
        _add_success_evidence(mock_handler)
        seen: list[str] = []

        original_bad_json = mock_handler.do_bad_json

        def do_bad_json(args, response):
            seen.append(args["msg"])
            return (yield from original_bad_json(args, response))

        mock_handler.do_bad_json = do_bad_json
        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(name="echo", arguments=arguments),
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

        terminal = _exhaust(loop.run("system prompt", "task"))

        assert terminal.status is TerminalStatus.COMPLETED
        assert seen == [f"function.arguments must decode to a JSON object, {expected}"]

    def test_native_malformed_marker_routes_to_bad_json(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _add_success_evidence(mock_handler)
        seen: list[str] = []
        original_bad_json = mock_handler.do_bad_json

        def do_bad_json(args, response):
            seen.append(args["msg"])
            return (yield from original_bad_json(args, response))

        mock_handler.do_bad_json = do_bad_json
        client = _make_mock_client([
            MockResponse(
                content="",
                tool_calls=[
                    MockToolCall(
                        function=MockFunction(
                            name="echo",
                            arguments=(
                                '{"_malformed": true, "_raw": "[]", '
                                '"_error": "tool arguments must be a JSON object"}'
                            ),
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

        terminal = _exhaust(loop.run("system prompt", "task"))

        assert terminal.status is TerminalStatus.COMPLETED
        assert seen == ["tool arguments must be a JSON object"]


    def test_multi_tool_results_are_sent_as_separate_messages(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """多工具调用后的 loop payload 保持自定义 tool_results."""
        _add_success_evidence(mock_handler)
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
            _complete_response("Both done.", [1]),
        ])
        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
        )

        terminal = _exhaust(loop.run("system prompt", "task"))

        assert terminal.status is TerminalStatus.COMPLETED
        assert len(client.calls[1]) == 1
        msg = client.calls[1][0]
        assert msg["role"] == "user"
        assert "### [WORKING MEMORY]" in msg["content"]
        assert "[USER]: task" in msg["content"]
        assert msg["tool_results"] == [
            {"tool_use_id": "call_1", "content": '{"result": "a"}'},
            {"tool_use_id": "call_2", "content": '{"result": "b"}'},
        ]

    def test_completion_request_without_certificate_is_protocol_error(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        def do_finish(self, args, response):
            yield "finishing\n"
            return StepOutcome({}, action=StepAction.REQUEST_COMPLETION)

        mock_handler.do_finish = do_finish.__get__(mock_handler)
        client = _make_mock_client([
            MockResponse(
                tool_calls=[MockToolCall(
                    function=MockFunction(name="finish", arguments="{}"),
                    id="call_1",
                )],
            ),
        ])
        loop = AgentLoop(client, mock_handler, [], max_turns=2, verbose=False)

        terminal = _exhaust(loop.run("sp", "task"))

        assert terminal.status is TerminalStatus.PROTOCOL_ERROR
        assert terminal.reason == "invalid_step_outcome"

    def test_invalid_fail_status_is_protocol_error(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        def do_fail(self, args, response):
            yield "failing\n"
            return StepOutcome(
                {},
                action=StepAction.FAIL,
                reason="bad",
                terminal_status=TerminalStatus.CANCELLED,
            )

        mock_handler.do_fail = do_fail.__get__(mock_handler)
        client = _make_mock_client([
            MockResponse(
                tool_calls=[MockToolCall(
                    function=MockFunction(name="fail", arguments="{}"),
                    id="call_1",
                )],
            ),
        ])
        loop = AgentLoop(client, mock_handler, [], max_turns=2, verbose=False)

        terminal = _exhaust(loop.run("sp", "task"))

        assert terminal.status is TerminalStatus.PROTOCOL_ERROR
        assert terminal.reason == "invalid_step_outcome"

    def test_unhandled_exception_returns_failed_terminal(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        class ExplodingClient:
            system = ""
            name = "exploding"
            last_tools = ""

            def chat(self, messages, tools=None):
                raise RuntimeError("boom")
                yield

        loop = AgentLoop(ExplodingClient(), mock_handler, [], max_turns=2, verbose=False)

        terminal = _exhaust(loop.run("sp", "task"))

        assert terminal.status is TerminalStatus.FAILED
        assert terminal.reason == "RuntimeError"

    def test_done_hook_extends_loop(self, mock_handler: BaseHandler) -> None:
        """_done_hooks 在任务声明完成时追加额外轮次."""
        _set_open_contract(mock_handler)
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
        terminal = _exhaust(gen)
        assert terminal.status is TerminalStatus.COMPLETED
        assert terminal.certificate is not None
        assert client._call_count == 2

    def test_reload_keeps_current_handler_workspace_and_schema(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_open_contract(mock_handler)
        original_cwd = mock_handler.cwd
        _add_success_evidence(mock_handler)
        responses = []
        for index in range(4):
            responses.append(MockResponse(
                content="",
                tool_calls=[MockToolCall(
                    function=MockFunction(name="echo", arguments=f'{{"message":"{index}"}}'),
                    id=f"call_{index}",
                )],
            ))
        responses.append(_complete_response("Final answer.", [1]))
        client = _make_mock_client(responses)

        class ReloadAgent:
            def __init__(self):
                self.client = client
                self.config = type("Config", (), {"workspace_dir": "/new/workspace"})()
                self.registry = type("Registry", (), {
                    "generate_openai_schema": lambda _self: (_ for _ in ()).throw(
                        AssertionError("runtime schema must remain deferred")
                    ),
                })()

            def reload_config(self):
                return True

        loop = AgentLoop(
            client=client,
            handler=mock_handler,
            tools_schema=[{"stable": True}],
            max_turns=5,
            verbose=False,
            agent=ReloadAgent(),
        )

        terminal = _exhaust(loop.run("system", "hello"))

        assert terminal.status is TerminalStatus.COMPLETED
        assert mock_handler.cwd == original_cwd
        assert loop.tools_schema == [{"stable": True}]


def _exhaust(gen: Generator) -> Any:
    """消费 generator 并返回最终值."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def _drain(gen: Generator) -> tuple[list[Any], Any]:
    chunks: list[Any] = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as stop:
        return chunks, stop.value



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
