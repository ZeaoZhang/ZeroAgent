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
