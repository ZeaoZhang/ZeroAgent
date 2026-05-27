"""Tests for llm/converters.py — message format converters."""

from zero_agent.llm.converters import (
    msgs_claude_to_openai,
    openai_tools_to_claude,
    to_responses_input,
)


class TestOpenaiToolsToClaude:
    """openai_tools_to_claude() 测试."""

    def test_converts_standard_format(self) -> None:
        tools = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        result = openai_tools_to_claude(tools)
        assert result[0]["name"] == "read_file"
        assert result[0]["description"] == "Read a file"
        assert "input_schema" in result[0]
        assert "function" not in result[0]

    def test_passthrough_claude_format(self) -> None:
        tools = [{"name": "test", "input_schema": {"type": "object"}}]
        result = openai_tools_to_claude(tools)
        assert result == tools

    def test_mixed_formats(self) -> None:
        tools = [
            {"name": "already_claude", "input_schema": {}},
            {"type": "function", "function": {"name": "new_one", "parameters": {}}},
        ]
        result = openai_tools_to_claude(tools)
        assert len(result) == 2
        assert result[0]["name"] == "already_claude"
        assert result[1]["name"] == "new_one"


class TestMsgsClaudeToOpenAI:
    """msgs_claude_to_openai() 测试."""

    def test_text_only_assistant(self) -> None:
        msgs = [{
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
        }]
        result = msgs_claude_to_openai(msgs)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == [{"type": "text", "text": "hello"}]

    def test_tool_use_conversion(self) -> None:
        msgs = [{
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read_file",
                "input": {"path": "test.py"},
            }],
        }]
        result = msgs_claude_to_openai(msgs)
        assert result[0]["role"] == "assistant"
        assert "tool_calls" in result[0]
        assert result[0]["tool_calls"][0]["function"]["name"] == "read_file"

    def test_thinking_to_reasoning(self) -> None:
        msgs = [{
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "let me think"},
                {"type": "text", "text": "answer"},
            ],
        }]
        result = msgs_claude_to_openai(msgs)
        assert result[0]["reasoning_content"] == "let me think"
        assert result[0]["content"] == [{"type": "text", "text": "answer"}]

    def test_tool_result_to_tool_role(self) -> None:
        msgs = [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "result text",
            }],
        }]
        result = msgs_claude_to_openai(msgs)
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "toolu_1"
        assert result[0]["content"] == "result text"

    def test_string_content_fallback(self) -> None:
        msgs = [{"role": "assistant", "content": "plain text"}]
        result = msgs_claude_to_openai(msgs)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == [{"type": "text", "text": "plain text"}]

    def test_empty_tool_calls_gets_dot(self) -> None:
        """空 assistant 消息（无 text 无 tool）补 '.'."""
        msgs = [{"role": "assistant", "content": []}]
        result = msgs_claude_to_openai(msgs)
        assert result[0]["content"] == "."


class TestToResponsesInput:
    """to_responses_input() 测试."""

    def test_user_message(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        result = to_responses_input(msgs)
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["type"] == "input_text"

    def test_assistant_message(self) -> None:
        msgs = [{"role": "assistant", "content": "response"}]
        result = to_responses_input(msgs)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"][0]["type"] == "output_text"

    def test_system_to_developer(self) -> None:
        msgs = [{"role": "system", "content": "instructions"}]
        result = to_responses_input(msgs)
        assert result[0]["role"] == "developer"

    def test_tool_message(self) -> None:
        msgs = [{
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "function": {"name": "f1", "arguments": "{}"},
            }],
        }, {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "tool result",
        }]
        result = to_responses_input(msgs)
        # assistant: function_call item
        fc_item = [r for r in result if r.get("type") == "function_call"]
        assert len(fc_item) == 1
        assert fc_item[0]["call_id"] == "call_1"
        # tool: function_call_output
        output_item = [r for r in result if r.get("type") == "function_call_output"]
        assert len(output_item) == 1
        assert output_item[0]["call_id"] == "call_1"
        assert output_item[0]["output"] == "tool result"

    def test_invalid_role_defaults_to_user(self) -> None:
        msgs = [{"role": "unknown_role", "content": "test"}]
        result = to_responses_input(msgs)
        assert result[0]["role"] == "user"
