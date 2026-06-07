"""Tests for LiteLLMSession message normalization."""

from zero_agent.core.config import LLMBackendConfig
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


def test_sanitize_file_write_required_matches_properties(mock_config) -> None:
    from zero_agent.tools.registry import ToolRegistry

    registry = ToolRegistry.with_builtins(mock_config)
    schema = registry.generate_openai_schema()

    sanitized = _make_session()._sanitize_tools(schema)
    file_write = next(
        tool for tool in sanitized if tool["function"]["name"] == "file_write"
    )
    parameters = file_write["function"]["parameters"]

    assert "content" not in parameters["properties"]
    assert "content" not in parameters.get("required", [])


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


def test_completion_kwargs_inject_tool_instruction_when_tools_mounted(monkeypatch) -> None:
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
    assert "system prompt" in system
    assert "Interaction Protocol" in system
    assert "user's request is not yet complete" in system
    assert "<tool_use>" in system
    assert '"name":"file_read"' in system
    assert kwargs["tools"] == tools


def test_completion_kwargs_repeats_short_tool_instruction_for_same_tools(monkeypatch) -> None:
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

    system = kwargs["messages"][0]["content"]
    assert "Tools: still active" in system
    assert "tool calls are required" in system
    assert '"name":"code_run"' not in system


def test_parse_text_tool_calls_accepts_bare_nested_json_object() -> None:
    calls = LiteLLMSession._parse_text_tool_calls(
        '先读取文件\n{"name":"file_read","arguments":{"path":"a/b.json",'
        '"options":{"encoding":"utf-8"}}}'
    )

    assert calls == [
        {
            "tool_name": "file_read",
            "args": {"path": "a/b.json", "options": {"encoding": "utf-8"}},
            "id": "text_fallback_0",
        }
    ]


def test_compress_history_tags_matches_ga_tag_replacement() -> None:
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


def test_trim_history_matches_ga_character_budget_and_user_boundary() -> None:
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
