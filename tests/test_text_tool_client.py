"""Tests for TextToolSession — text-based tool-call protocol fallback."""

import json

import pytest

from zero_agent.llm.text_tool_client import TextToolSession, _tryparse_json
from zero_agent.llm.base import MockFunction, MockResponse, MockToolCall


# ---- helpers ----

class FakeBackend:
    """A fake backend that returns predetermined text when .chat() is called."""

    def __init__(self, response_text="", name="fake"):
        self._response = response_text
        self.name = name
        self._history = []
        self._system = ""
        self.config = type("C", (), {"provider": "openai", "model": "fake"})()
        self.log_path = None
        self.temperature = 1.0
        self.max_tokens = None

    @property
    def history(self):
        return self._history

    @history.setter
    def history(self, value):
        self._history = value

    @property
    def system(self):
        return self._system

    @system.setter
    def system(self, value):
        self._system = value

    def chat(self, messages, tools=None):
        yield self._response
        return MockResponse(content=self._response)


def _make_tts(response="", name="fake"):
    return TextToolSession(FakeBackend(response, name), auto_save_tokens=True)


# ---- parse tests ----

def test_parse_single_tool_use_xml_block():
    """Parses one XML <tool_use> block into a MockToolCall."""
    text = (
        "<thinking>need to read</thinking>\n"
        '<summary>reading a.txt</summary>\n'
        'Let me check.\n'
        '<tool_use>{"name":"file_read","arguments":{"path":"a.txt"}}</tool_use>'
    )
    tts = _make_tts()
    result = tts._parse_mixed_response(text)

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.function.name == "file_read"
    args = json.loads(tc.function.arguments)
    assert args == {"path": "a.txt"}
    # Visible content excludes thinking and tool_use blocks
    assert "thinking" not in result.content.lower()
    assert "<tool_use>" not in result.content


def test_parse_multiple_tool_use_blocks():
    """Parses two tool_use blocks in a single response."""
    text = (
        '<tool_use>{"name":"file_read","arguments":{"path":"a"}}</tool_use>\n'
        '<tool_use>{"name":"file_write","arguments":{"path":"b","content":"hi"}}</tool_use>'
    )
    tts = _make_tts()
    result = tts._parse_mixed_response(text)

    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].function.name == "file_read"
    assert result.tool_calls[1].function.name == "file_write"


def test_parse_tool_call_tag():
    """<tool_call>...</tool_call> works the same as <tool_use>."""
    text = '<tool_call>{"name":"code_run","arguments":{"code":"1+1"}}</tool_call>'
    tts = _make_tts()
    result = tts._parse_mixed_response(text)

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].function.name == "code_run"


def test_parse_bare_json_with_name():
    """Fallback: bare JSON containing 'name' and 'arguments' keys."""
    text = 'Here is the result {"name":"web_scan","arguments":{"url":"x"}}'
    tts = _make_tts()
    result = tts._parse_mixed_response(text)

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.function.name == "web_scan"


def test_parse_json_array_of_tool_use():
    """JSON array of [{"type":"tool_use", "name":..., "input":...}]."""
    text = (
        'Done.\n'
        '[{"type":"tool_use","name":"ask_user","input":{"question":"ok?"}}]'
    )
    tts = _make_tts()
    result = tts._parse_mixed_response(text)

def test_bad_json_yields_bad_json_tool_call():
    """On parse failure, returns bad_json tool call with error."""
    # Use a format where the XML tag regex matches and produces a parse-able
    # JSON block, but the weak fallback tries to parse something invalid.
    # Text with <tool_use> containing a JSON-looking block that doesn't parse.
    text = '<tool_use>{"name":"ok","arguments":"broken}</tool_use>'
    tts = _make_tts()
    result = tts._parse_mixed_response(text)

    assert any(tc.function.name == "bad_json" for tc in result.tool_calls)


def test_no_tools_returns_empty():
    """No tool blocks → empty tool_calls."""
    tts = _make_tts()
    result = tts._parse_mixed_response("Just a plain reply. No tools involved.")
    assert result.tool_calls == []
    assert result.content == "Just a plain reply. No tools involved."


def test_thinking_extracted():
    """<thinking> content is extracted."""
    text = "<thinking>analyze</thinking>\nNormal reply"
    tts = _make_tts()
    result = tts._parse_mixed_response(text)
    assert result.thinking == "analyze"
    assert result.content == "Normal reply"


# ---- protocol prompt tests ----

def test_tool_instruction_emits_full_schema_first():
    tts = _make_tts()
    tools = [{"type": "function", "function": {"name": "read", "description": "desc", "parameters": {}}}]
    inst = tts._prepare_tool_instruction(tools)
    assert "交互协议" in inst or "Interaction Protocol" in inst
    assert "read" in inst


def test_repeated_tools_compress():
    """Repeated same schema compresses to 'tools still active'."""
    tts = _make_tts()
    tools = [{"type": "function", "function": {"name": "read"}}]
    inst1 = tts._prepare_tool_instruction(tools)
    inst2 = tts._prepare_tool_instruction(tools)
    # Second call is compressed
    assert len(inst2) < len(inst1)
    assert "持续有效" in inst2 or "still active" in inst2


def test_changed_tools_re_emit():
    """Changed tools re-emit full schema."""
    tts = _make_tts()
    tools1 = [{"type": "function", "function": {"name": "read"}}]
    tools2 = [{"type": "function", "function": {"name": "write"}}]
    tts._prepare_tool_instruction(tools1)
    inst2 = tts._prepare_tool_instruction(tools2)
    assert "write" in inst2


# ---- _tryparse_json ----

def test_tryparse_plain_json():
    assert _tryparse_json('{"a":1}') == {"a": 1}


def test_tryparse_fenced_json():
    assert _tryparse_json('```json\n{"a":1}\n```') == {"a": 1}


def test_tryparse_invalid():
    assert _tryparse_json("not json") is None


# ---- full chat flow ----

def test_chat_yields_and_returns_mock_response():
    text = (
        "<thinking>x</thinking>\n"
        "<summary>done</summary>\n"
        "The file is ready.\n"
        '<tool_use>{"name":"file_write","arguments":{"path":"out","content":"x"}}</tool_use>'
    )
    tts = _make_tts(text)
    gen = tts.chat([{"role": "user", "content": "Write file out.py"}], tools=[])

    chunks = []
    mock = None
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as e:
        mock = e.value

    assert len(chunks) > 0
    assert mock.content == "<summary>done</summary>\nThe file is ready."
    assert len(mock.tool_calls) == 1
    assert mock.tool_calls[0].function.name == "file_write"
