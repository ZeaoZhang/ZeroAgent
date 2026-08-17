"""Tests for config-driven multimodal backend calls."""

from types import SimpleNamespace

import pytest

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.exceptions import LLMError
from zero_agent.llm import sessions as sessions_module
from zero_agent.llm.sessions import LiteLLMSession
from zero_agent.tools.registry import ToolRegistry


def _response(text: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason="stop",
            )
        ],
        usage=None,
    )


def test_vision_uses_backend_connection_and_image_content(monkeypatch) -> None:
    config = LLMBackendConfig(
        name="gpt-vision",
        provider="openai",
        api_key="configured-key",
        api_base="https://relay.example/v1",
        model="gpt-text",
        vision=True,
        vision_model="gpt-vision-model",
        vision_max_tokens=1024,
        stream=True,
    )
    session = LiteLLMSession(config)
    captured = {}

    monkeypatch.setattr(
        sessions_module,
        "_image_to_data_url",
        lambda _image, _max_pixels: "data:image/jpeg;base64,encoded",
    )

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _response("image understood")

    monkeypatch.setattr(sessions_module.litellm, "completion", fake_completion)

    assert session.vision("screen.png", "describe this") == "image understood"
    assert captured["model"] == "gpt-vision-model"
    assert captured["api_key"] == "configured-key"
    assert captured["api_base"] == "https://relay.example/v1"
    assert captured["max_tokens"] == 1024
    content = captured["messages"][0]["content"]
    assert {part["type"] for part in content} == {"text", "image_url"}
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,encoded"


def test_vision_rejects_backend_without_vision_capability() -> None:
    session = LiteLLMSession(
        LLMBackendConfig(
            name="text-only",
            provider="openai",
            api_key="key",
            api_base="https://relay.example/v1",
            model="text-model",
            vision=False,
        )
    )

    with pytest.raises(LLMError, match="vision"):
        session.vision("screen.png", "describe this")


def test_openai_compatible_backend_forwards_explicit_thinking_config() -> None:
    session = LiteLLMSession(
        LLMBackendConfig(
            name="deepseek",
            provider="openai",
            api_key="key",
            api_base="https://relay.example/v1",
            model="deepseek-text",
            thinking_type="enabled",
            thinking_budget_tokens=4096,
        )
    )

    kwargs = session._build_completion_kwargs(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        stream=False,
    )

    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 4096}

def test_vision_tool_rejects_text_only_default_backend() -> None:
    config = AgentConfig(
        default_backend="text",
        llm_backends={
            "text": LLMBackendConfig(
                name="text",
                provider="openai",
                api_key="key",
                api_base="https://relay.example/v1",
                model="deepseek-text",
                vision=False,
            ),
            "visual": LLMBackendConfig(
                name="visual",
                provider="openai",
                api_key="key",
                api_base="https://relay.example/v1",
                model="gpt-vision",
                vision=True,
            ),
        },
    )
    tool = ToolRegistry.with_builtins(config).get("vision")
    assert tool is not None

    class Handler:
        parent = type("Parent", (), {"_sessions": {}})()

    result = tool.handler({"image_path": "screen.png"}, None, Handler())
    with pytest.raises(StopIteration) as stopped:
        next(result)
    assert stopped.value.value.data["status"] == "error"
    assert "does not support vision" in stopped.value.value.data["msg"]

def test_anthropic_vision_uses_base64_image_source(monkeypatch) -> None:
    session = LiteLLMSession(
        LLMBackendConfig(
            name="anthropic-vision",
            provider="anthropic",
            api_key="key",
            api_base="https://api.anthropic.com",
            model="claude-vision",
            vision=True,
        )
    )
    monkeypatch.setattr(
        sessions_module,
        "_image_to_data_url",
        lambda *_: "data:image/jpeg;base64,encoded",
    )
    captured = {}
    monkeypatch.setattr(
        sessions_module.litellm,
        "completion",
        lambda **kwargs: (captured.update(kwargs) or _response("ok")),
    )

    assert session.vision("screen.png") == "ok"
    image = captured["messages"][0]["content"][1]
    assert image == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "encoded"},
    }


def test_vision_invalid_image_raises_llm_error() -> None:
    session = LiteLLMSession(
        LLMBackendConfig(
            name="vision",
            provider="openai",
            api_key="key",
            api_base="https://relay.example/v1",
            model="vision-model",
            vision=True,
        )
    )

    with pytest.raises(LLMError, match="image preparation"):
        session.vision("missing-image.png")

def test_failover_session_delegates_vision_to_active_backend() -> None:
    from zero_agent.llm.failover import AutoFailoverSession

    class Client:
        name = "visual"
        config = LLMBackendConfig(
            name="visual",
            provider="openai",
            api_key="key",
            api_base="https://relay.example/v1",
            model="vision-model",
            vision=True,
        )

        def vision(self, image_input, prompt, **kwargs):
            return "delegated"

    session = AutoFailoverSession(Client(), [])
    assert session.vision("screen.png", "describe") == "delegated"
