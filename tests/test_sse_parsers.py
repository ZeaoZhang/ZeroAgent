"""Tests for llm/sse_parsers.py — SSE stream parsers."""

import json

import pytest

from zero_agent.llm.sse_parsers import (
    _build_content_blocks,
    _try_parse_tool_args,
    parse_claude_sse,
    parse_openai_sse,
)


# ---- helpers ----

def _sse_line(data: dict | str) -> bytes:
    """Build an SSE data line."""
    if isinstance(data, str):
        return f"data: {data}\n\n".encode()
    return f"data: {json.dumps(data)}\n\n".encode()


def _stream(*events: dict | str) -> list:
    """Build a list of SSE lines from events. str values become raw lines."""
    lines = []
    for e in events:
        if isinstance(e, str):
            lines.append(e.encode())
        else:
            lines.append(_sse_line(e))
    return lines


# ---- parse_claude_sse tests ----

class TestParseClaudeSSE:
    """parse_claude_sse() 测试."""

    def test_text_only(self) -> None:
        gen = parse_claude_sse(iter(_stream(
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "world"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        )))
        # consumer generator, capturing return value
        chunks = []
        blocks = None
        while True:
            try:
                chunks.append(next(gen))
            except StopIteration as e:
                blocks = e.value
                break
        assert chunks == ["Hello ", "world"]
        assert blocks is not None
        assert len(blocks) == 1
        assert blocks[0] == {"type": "text", "text": "Hello world"}

    def test_tool_use(self) -> None:
        gen = parse_claude_sse(iter(_stream(
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_123", "name": "read_file"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"test.py"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )))
        try:
            next(gen)
        except StopIteration as e:
            blocks = e.value
            assert len(blocks) == 1
            assert blocks[0]["type"] == "tool_use"
            assert blocks[0]["name"] == "read_file"
            assert blocks[0]["id"] == "toolu_123"
            assert blocks[0]["input"] == {"path": "test.py"}

    def test_thinking_block(self) -> None:
        gen = parse_claude_sse(iter(_stream(
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me think..."}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig123"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        )))
        try:
            next(gen)
        except StopIteration as e:
            blocks = e.value
            assert len(blocks) == 1
            assert blocks[0]["type"] == "thinking"
            assert blocks[0]["thinking"] == "Let me think..."
            assert blocks[0]["signature"] == "sig123"

    def test_stream_interruption_warning(self) -> None:
        """未收到 message_stop 时插入警告."""
        gen = parse_claude_sse(iter(_stream(
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}},
        )))
        # Should yield the text and a warning
        chunks = list(gen)
        assert "partial" in "".join(chunks)
        assert any("流异常中断" in c for c in chunks)

    def test_max_tokens_warning(self) -> None:
        gen = parse_claude_sse(iter(_stream(
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "abc"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
        )))
        chunks = list(gen)
        assert any("max_tokens" in c for c in chunks)

    def test_error_event(self) -> None:
        gen = parse_claude_sse(iter(_stream(
            {"type": "message_start", "message": {}},
            {"type": "error", "error": {"message": "overloaded"}},
        )))
        chunks = list(gen)
        assert any("overloaded" in c for c in chunks)

    def test_json_input_error_stores_raw(self) -> None:
        """非法 JSON 存入 _raw."""
        gen = parse_claude_sse(iter(_stream(
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "test"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{invalid"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        )))
        try:
            next(gen)
        except StopIteration as e:
            blocks = e.value
            assert blocks[0]["input"] == {"_raw": "{invalid"}

    def test_empty_line_skipped(self) -> None:
        """空行和非法行被跳过."""
        gen = parse_claude_sse(iter(_stream(
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        )))
        try:
            next(gen)
        except StopIteration as e:
            blocks = e.value
            assert len(blocks) == 1
            assert blocks[0]["text"] == "ok"

    def test_multiple_content_blocks(self) -> None:
        gen = parse_claude_sse(iter(_stream(
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "text1"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "f1"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"k":"v"}'}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_stop"},
        )))
        try:
            next(gen)
        except StopIteration as e:
            blocks = e.value
            assert len(blocks) == 2
            assert blocks[0]["type"] == "text"
            assert blocks[1]["type"] == "tool_use"


# ---- _try_parse_tool_args tests ----

class TestTryParseToolArgs:
    """_try_parse_tool_args() 测试."""

    def test_valid_json(self) -> None:
        assert _try_parse_tool_args('{"a":1}') == [{"a": 1}]

    def test_empty_string(self) -> None:
        assert _try_parse_tool_args("") == [{}]

    def test_concatenated_json(self) -> None:
        result = _try_parse_tool_args('{"a":1}{"b":2}')
        assert len(result) == 2
        assert result[0] == {"a": 1}
        assert result[1] == {"b": 2}

    def test_invalid_stores_raw(self) -> None:
        assert _try_parse_tool_args("{not valid") == [{"_raw": "{not valid"}]


# ---- parse_openai_sse tests ----

class TestParseOpenaiSSE:
    """parse_openai_sse() 测试."""

    def test_chat_completions_text(self) -> None:
        gen = parse_openai_sse(iter(_stream(
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )))
        chunks = list(gen)
        assert chunks == ["Hello", " world"]

    def test_chat_completions_tool_call(self) -> None:
        gen = parse_openai_sse(iter(_stream(
            {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_123",
                            "function": {"name": "read_file", "arguments": '{"path":"f.py"}'},
                        }],
                    },
                }],
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )))
        try:
            next(gen)
        except StopIteration as e:
            blocks = e.value
            assert len(blocks) == 1
            assert blocks[0]["type"] == "tool_use"
            assert blocks[0]["name"] == "read_file"
            assert blocks[0]["input"] == {"path": "f.py"}

    def test_chat_completions_reasoning(self) -> None:
        gen = parse_openai_sse(iter(_stream(
            {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )))
        try:
            next(gen)
        except StopIteration as e:
            blocks = e.value
            assert len(blocks) == 2
            assert blocks[0]["type"] == "thinking"
            assert blocks[0]["thinking"] == "thinking..."
            assert blocks[1]["type"] == "text"

    def test_responses_api_text(self) -> None:
        gen = parse_openai_sse(iter(_stream(
            {"type": "response.output_text.delta", "delta": "Hello responses"},
            {"type": "response.output_text.done", "text": "Hello responses"},
            {"type": "response.completed", "response": {"usage": {}}},
        )), api_mode="responses")
        chunks = list(gen)
        assert "Hello responses" in "".join(chunks)

    def test_responses_error(self) -> None:
        gen = parse_openai_sse(iter(_stream(
            {"type": "error", "error": {"message": "rate limited"}},
        )), api_mode="responses")
        chunks = list(gen)
        assert any("rate limited" in c for c in chunks)


# ---- _build_content_blocks tests ----

class TestBuildContentBlocks:
    """_build_content_blocks() 测试."""

    def test_text_only(self) -> None:
        blocks = _build_content_blocks("hello", {})
        assert blocks == [{"type": "text", "text": "hello"}]

    def test_function_calls(self) -> None:
        fc = {0: {"id": "fc_1", "name": "do_thing", "args": '{"k":"v"}'}}
        blocks = _build_content_blocks("", fc)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "tool_use"
        assert blocks[0]["name"] == "do_thing"

    def test_multiple_function_calls(self) -> None:
        fc = {
            0: {"id": "a", "name": "f1", "args": "{}"},
            1: {"id": "b", "name": "f2", "args": "{}"},
        }
        blocks = _build_content_blocks("", fc)
        assert len(blocks) == 2
