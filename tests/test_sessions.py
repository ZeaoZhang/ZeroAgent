"""Tests for LiteLLMSession message normalization."""

from types import SimpleNamespace

from zero_agent.core.config import LLMBackendConfig
from zero_agent.llm.base import extract_usage_metrics, usage_has_cache_metrics
from zero_agent.llm.sessions import LiteLLMSession


def _make_session() -> LiteLLMSession:
    return LiteLLMSession(
        LLMBackendConfig(
            name="default",
            provider="openai",
            api_key="sk-test",
            api_base="https://api.openai.com/v1",
            model="gpt-test",
        )
    )


def test_build_messages_does_not_duplicate_session_system() -> None:
    session = _make_session()
    session.system = "system prompt"
    session.history = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
    ]

    messages = session._build_messages()

    assert messages == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
    ]


def test_normalize_incoming_tool_results_to_tool_messages() -> None:
    session = _make_session()

    messages = session._normalize_incoming_messages([
        {
            "role": "user",
            "content": "continue",
            "tool_results": [
                {"tool_use_id": "call_1", "content": '{"status":"ok"}'},
            ],
        }
    ])

    assert messages == [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"status":"ok"}',
        },
        {"role": "user", "content": "continue"},
    ]


def test_fix_messages_preserves_consecutive_tool_messages() -> None:
    session = _make_session()

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "a", "arguments": "{}"},
                },
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "b", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "r0"},
        {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
        {"role": "user", "content": "continue"},
    ]

    fixed = session._fix_messages(messages)

    tool_messages = [m for m in fixed if m["role"] == "tool"]
    assert tool_messages == [
        {"role": "tool", "tool_call_id": "call_0", "content": "r0"},
        {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
    ]


def test_fix_messages_fills_missing_tool_results_after_assistant_tool_calls() -> None:
    session = _make_session()

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "a", "arguments": "{}"},
                },
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "b", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "r0"},
        {"role": "user", "content": "continue"},
    ]

    fixed = session._fix_messages(messages)

    assistant_index = next(
        i for i, m in enumerate(fixed)
        if m["role"] == "assistant" and m.get("tool_calls")
    )
    following = fixed[assistant_index + 1:assistant_index + 3]
    assert [m["role"] for m in following] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in following] == ["call_0", "call_1"]
    assert "missing" in following[1]["content"]


def test_sanitize_tools_keeps_native_file_write_content_schema(mock_config) -> None:
    from zero_agent.tools.registry import ToolRegistry

    registry = ToolRegistry.with_builtins(mock_config)
    schema = registry.generate_openai_schema()

    sanitized = _make_session()._sanitize_tools(schema)
    file_write = next(
        tool for tool in sanitized if tool["function"]["name"] == "file_write"
    )
    parameters = file_write["function"]["parameters"]

    assert "content" in parameters["properties"]
    assert "content" in parameters.get("required", [])
    assert "<file_content>" not in file_write["function"]["description"]


def test_completion_kwargs_include_provider_for_openai_compatible_backend() -> None:
    session = LiteLLMSession(
        LLMBackendConfig(
            name="deepseek",
            provider="openai",
            api_key="sk-test",
            api_base="https://api.deepseek.com",
            model="deepseek-v4-flash",
        )
    )

    kwargs = session._build_completion_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        stream=True,
    )

    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["custom_llm_provider"] == "openai"
    assert kwargs["api_base"] == "https://api.deepseek.com"


def test_completion_kwargs_use_native_tools_without_text_protocol(monkeypatch) -> None:
    monkeypatch.delenv("ZA_LANG", raising=False)
    monkeypatch.setenv("ZA_LANG", "en")
    session = _make_session()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "file_read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    kwargs = session._build_completion_kwargs(
        messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "inspect repo"},
        ],
        tools=tools,
        stream=True,
    )

    system = kwargs["messages"][0]["content"]
    assert system == "system prompt"
    assert "Interaction Protocol" not in system
    assert "<tool_use>" not in system
    assert '"name":"file_read"' not in system
    assert kwargs["tools"] == tools


def test_completion_kwargs_do_not_cache_text_tool_protocol(monkeypatch) -> None:
    monkeypatch.delenv("ZA_LANG", raising=False)
    monkeypatch.setenv("ZA_LANG", "en")
    session = _make_session()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "code_run",
                "description": "Run code",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    session._build_completion_kwargs(
        messages=[{"role": "user", "content": "first"}],
        tools=tools,
        stream=True,
    )
    kwargs = session._build_completion_kwargs(
        messages=[{"role": "user", "content": "second"}],
        tools=tools,
        stream=True,
    )

    assert len(kwargs["messages"]) == 1
    assert kwargs["messages"][0]["role"] == "user"
    assert "second" in str(kwargs["messages"][0]["content"])
    assert "<tool_use>" not in str(kwargs["messages"])
    assert kwargs["tools"] == tools
    assert session.last_tools == ""


def test_text_tool_syntax_is_not_parsed_as_native_tool_call(monkeypatch) -> None:
    class FakeMessage:
        content = (
            '先读取文件\n<tool_use>{"name":"file_read",'
            '"arguments":{"path":"a/b.json"}}</tool_use>'
        )
        tool_calls = None

    class FakeChoice:
        message = FakeMessage()
        finish_reason = "stop"

    class FakeResponse:
        choices = [FakeChoice()]

    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: FakeResponse(),
    )

    session = _make_session()
    session.config.stream = False
    gen = session.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "file_read",
                    "description": "Read",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    assert next(gen) == FakeMessage.content
    try:
        next(gen)
    except StopIteration as exc:
        response = exc.value
    else:
        raise AssertionError("chat generator should finish")

    assert response.tool_calls == []
    assert response.stop_reason == "end_turn"


def test_compress_history_tags_replaces_old_tags() -> None:
    long_history = "<history>\n" + ("x" * 2000) + "\n</history>"
    messages = [
        {"role": "user", "content": f"old\n### [WORKING MEMORY]\n{long_history}"},
        {"role": "assistant", "content": f"<thinking>{'y' * 2000}</thinking>"},
        {"role": "user", "content": f"recent\n### [WORKING MEMORY]\n{long_history}"},
    ]

    compressed = LiteLLMSession._compress_history_tags(
        messages,
        keep_recent=1,
        force=True,
    )

    assert "<history>[...]</history>" in compressed[0]["content"]
    assert "...[Truncated]..." in compressed[1]["content"]
    assert "<history>[...]</history>" not in compressed[2]["content"]


def test_trim_history_respects_character_budget_and_user_boundary() -> None:
    session = _make_session()
    session._context_window = 200
    session._trim_keep_rate = 0.5
    session.history = [
        {"role": "assistant", "content": "orphan old assistant"},
        {"role": "user", "content": "old user " + ("x" * 900)},
        {"role": "assistant", "content": "old assistant " + ("x" * 900)},
        {"role": "user", "content": "middle user " + ("x" * 900)},
        {"role": "assistant", "content": "middle assistant " + ("x" * 900)},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
    ]

    session._trim_history()

    assert len(session.history) <= 9
    assert session.history[0]["role"] == "user"
    assert all("old " not in str(msg.get("content")) for msg in session.history)


def test_openai_completion_kwargs_convert_claude_tool_use_blocks() -> None:
    session = _make_session()

    kwargs = session._build_completion_kwargs(
        messages=[
            {"role": "user", "content": "read config"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "file_read",
                        "input": {"path": "config.yaml"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "ok",
                    }
                ],
            },
        ],
        tools=None,
        stream=True,
    )

    assert "content" not in kwargs["messages"][1]
    assert kwargs["messages"][1]["tool_calls"][0]["function"]["name"] == "file_read"
    assert kwargs["messages"][2] == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": "ok",
    }


def test_openai_completion_kwargs_fill_missing_tool_result_after_conversion() -> None:
    session = _make_session()

    kwargs = session._build_completion_kwargs(
        messages=[
            {"role": "user", "content": "read config"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "file_read",
                        "input": {"path": "config.yaml"},
                    }
                ],
            },
            {"role": "user", "content": "continue"},
        ],
        tools=None,
        stream=True,
    )

    assistant_index = next(
        i for i, msg in enumerate(kwargs["messages"])
        if msg["role"] == "assistant" and msg.get("tool_calls")
    )
    following = kwargs["messages"][assistant_index + 1]
    assert following["role"] == "tool"
    assert following["tool_call_id"] == "toolu_1"
    assert "missing" in following["content"]


def test_build_completion_kwargs_forwards_api_mode_responses() -> None:
    """_build_completion_kwargs forwards api_mode=responses."""
    session = LiteLLMSession(
        LLMBackendConfig(
            name="default",
            provider="openai",
            api_key="sk-test",
            api_base="https://api.openai.com/v1",
            model="gpt-test",
            api_mode="responses",
        )
    )
    session.system = "hi"
    kwargs = session._build_completion_kwargs(
        messages=session._build_messages(),
        tools=[],
        stream=True,
    )
    assert kwargs.get("api_mode") == "responses"


def test_build_completion_kwargs_default_chat_completions_omits_api_mode() -> None:
    """Default api_mode (chat_completions) omits api_mode from kwargs."""
    session = _make_session()
    session.system = "hi"
    kwargs = session._build_completion_kwargs(
        messages=session._build_messages(),
        tools=[],
        stream=True,
    )
    assert "api_mode" not in kwargs


def _drain_chat(gen):
    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as exc:
        return chunks, exc.value


def test_sync_chat_preserves_native_usage_protocol_and_normalizes_stop(monkeypatch) -> None:
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=4)

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="hello", reasoning_content="", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=usage,
    )
    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: response,
    )

    session = _make_session()
    session.config.stream = False
    chunks, mock = _drain_chat(session.chat([{"role": "user", "content": "hi"}], tools=[]))

    assert chunks == ["hello"]
    assert mock.usage is usage
    assert mock.tool_protocol == "native"
    assert mock.stop_reason == "end_turn"
def test_chat_logs_redacted_request_and_response_metadata(monkeypatch, caplog) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="hello", reasoning_content="", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: response,
    )

    session = _make_session()
    session.config.stream = False
    session.config.api_key = "sk-secret-value"
    with caplog.at_level("INFO", logger="zero_agent.llm.sessions"):
        _drain_chat(session.chat([{"role": "user", "content": "do not log me"}], tools=[]))

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "llm_request" in messages
    assert "llm_response" in messages
    assert "gpt-test" in messages
    assert "do not log me" not in messages
    assert "sk-secret-value" not in messages


def test_chat_logs_redacted_failure_and_preserves_cause(monkeypatch, caplog) -> None:
    def fail(**kwargs):
        raise RuntimeError("upstream rejected key sk-secret-value")

    monkeypatch.setattr("zero_agent.llm.sessions.litellm.completion", fail)
    session = _make_session()
    session.config.stream = False
    session.config.api_key = "sk-secret-value"

    with caplog.at_level("ERROR", logger="zero_agent.llm.sessions"):
        try:
            _drain_chat(session.chat([{"role": "user", "content": "private prompt"}], tools=[]))
        except Exception as exc:
            error = exc

    assert type(error).__name__ == "LLMError"
    assert isinstance(error.__cause__, RuntimeError)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "llm_failure" in messages
    assert "RuntimeError" in messages
    assert "private prompt" not in messages
    assert "sk-secret-value" not in messages


def test_stream_chat_preserves_accumulated_fields_when_final_chunk_has_no_message(monkeypatch) -> None:
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=6)

    def make_chunk(delta=None, finish_reason=None, chunk_usage=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, message=None, finish_reason=finish_reason)],
            usage=chunk_usage,
        )

    chunks = [
        make_chunk(SimpleNamespace(content="hi ", reasoning_content="think ", tool_calls=None)),
        make_chunk(SimpleNamespace(content="there", reasoning_content="more", tool_calls=None)),
        make_chunk(None, finish_reason="stop", chunk_usage=usage),
    ]
    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: iter(chunks),
    )

    session = _make_session()
    session.config.stream = True
    streamed, mock = _drain_chat(session.chat([{"role": "user", "content": "hi"}], tools=[]))

    assert streamed == ["hi ", "there"]
    assert mock.content == "hi there"
    assert mock.thinking == "think more"
    assert mock.usage is usage
    assert mock.tool_protocol == "native"
    assert mock.stop_reason == "end_turn"


def test_stream_chat_picks_last_non_empty_usage_and_normalizes_tool_stop(monkeypatch) -> None:
    usage = SimpleNamespace(prompt_tokens=7, completion_tokens=8)
    tool_delta = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="file_read", arguments='{"path":"a"}'),
    )

    def make_chunk(delta=None, finish_reason=None, chunk_usage=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, message=None, finish_reason=finish_reason)],
            usage=chunk_usage,
        )

    chunks = [
        make_chunk(SimpleNamespace(content=None, reasoning_content="", tool_calls=[tool_delta]), chunk_usage=usage),
        make_chunk(None, finish_reason="stop", chunk_usage=None),
    ]
    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: iter(chunks),
    )

    session = _make_session()
    session.config.stream = True
    streamed, mock = _drain_chat(session.chat([{"role": "user", "content": "read"}], tools=[]))

    assert streamed == []
    assert len(mock.tool_calls) == 1
    assert mock.tool_calls[0].function.name == "file_read"
    assert mock.usage is usage
    assert mock.tool_protocol == "native"
    assert mock.stop_reason == "tool_use"


def test_extract_usage_metrics_supports_anthropic_and_deepseek_aliases() -> None:
    assert extract_usage_metrics({
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 5,
    }) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 80,
        "cache_creation_tokens": 5,
        "cache_miss_tokens": 20,
    }
    assert extract_usage_metrics(SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_cache_hit_tokens=80,
        prompt_cache_miss_tokens=20,
    ))["cache_read_tokens"] == 80


def test_extract_usage_metrics_reads_nested_object_cache_details() -> None:
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
    )
    assert usage_has_cache_metrics(usage)
    assert extract_usage_metrics(usage)["cache_miss_tokens"] == 20


def test_extract_usage_metrics_uses_anthropic_input_as_cache_misses() -> None:
    usage = {
        "input_tokens": 20,
        "output_tokens": 5,
        "cache_read_input_tokens": 80,
    }

    assert extract_usage_metrics(usage)["cache_miss_tokens"] == 20


def test_close_response_log_prevents_late_worker_write(tmp_path) -> None:
    session = LiteLLMSession(
        LLMBackendConfig(
            name="default",
            provider="openai",
            api_key="sk-test",
            api_base="https://api.openai.com/v1",
            model="gpt-test",
        ),
        session_log_path=str(tmp_path / "owned.log"),
    )

    session.close_response_log()
    session._write_model_response_log(
        [{"role": "user", "content": "private"}],
        SimpleNamespace(content="private", tool_calls=[], thinking=""),
    )

    assert not (tmp_path / "owned.log").exists()


def test_chat_writes_session_response_log(monkeypatch, tmp_path) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="hello", reasoning_content="", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: response,
    )
    session = LiteLLMSession(
        LLMBackendConfig(
            name="default",
            provider="openai",
            api_key="sk-test",
            api_base="https://api.openai.com/v1",
            model="gpt-test",
        ),
        sessions_dir=str(tmp_path),
    )
    session.config.stream = False

    _drain_chat(session.chat([{"role": "user", "content": "hi"}], tools=[]))

    response_logs = list(tmp_path.glob("model_responses_*.txt"))
    assert len(response_logs) == 1
    assert "=== Prompt ===" in response_logs[0].read_text(encoding="utf-8")

    session.close_response_log()
    session._write_model_response_log(
        [{"role": "user", "content": "private"}],
        SimpleNamespace(content="private", tool_calls=[], thinking=""),
    )

    assert not (tmp_path / "owned.log").exists()



def test_non_deepseek_session_uses_standard_history_trim_policy() -> None:
    session = _make_session()

    assert session._cut_msg_interval == 25
    assert session._trim_keep_rate == 0.3


def test_malformed_cache_metrics_remain_unavailable() -> None:
    usage = {"prompt_tokens": 100, "cache_read": "bad"}

    assert not usage_has_cache_metrics(usage)


def test_fractional_cache_metrics_remain_unavailable() -> None:
    usage = {"prompt_tokens": 100, "cache_read": 0.5}

    assert not usage_has_cache_metrics(usage)
    assert extract_usage_metrics(usage)["cache_read_tokens"] == 0


def test_close_response_log_serializes_with_late_write(tmp_path) -> None:
    session = LiteLLMSession(
        LLMBackendConfig(
            name="default", provider="openai", api_key="sk-test",
            api_base="https://api.openai.com/v1", model="gpt-test",
        ),
        session_log_path=str(tmp_path / "owned.log"),
    )
    session.close_response_log()

    session._write_model_response_log(
        [{"role": "user", "content": "private"}],
        SimpleNamespace(content="private", tool_calls=[], thinking=""),
    )

    assert not (tmp_path / "owned.log").exists()

def test_extract_usage_metrics_rejects_malformed_and_negative_values() -> None:
    assert not usage_has_cache_metrics(None)
    assert not usage_has_cache_metrics(SimpleNamespace(prompt_tokens_details=SimpleNamespace()))
    assert extract_usage_metrics(None) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_miss_tokens": 0,
    }
    assert extract_usage_metrics({
        "prompt_tokens": "bad",
        "completion_tokens": -1,
        "cache_read": -4,
        "cache_creation": object(),
    }) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_miss_tokens": 0,
    }


def test_session_usage_stats_separates_cache_creation_and_miss() -> None:
    session = _make_session()
    session._record_usage(SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_cache_hit_tokens=80,
        prompt_cache_miss_tokens=20,
        cache_creation_input_tokens=10,
    ))
    stats = session.usage_stats
    assert stats["total_cache_read_tokens"] == 80
    assert stats["total_cache_creation_tokens"] == 10
    assert stats["total_cache_miss_tokens"] == 20
    assert stats["total_cached_tokens"] == 80
    assert stats["cache_hit_rate"] == 80.0
    assert stats["cache_metrics_available"] is True
