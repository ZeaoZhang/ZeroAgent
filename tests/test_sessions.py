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
