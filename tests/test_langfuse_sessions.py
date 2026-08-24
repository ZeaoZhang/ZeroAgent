"""Tests for Langfuse observations around concrete LiteLLM calls."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from zero_agent.core.config import LLMBackendConfig
from zero_agent.core.exceptions import LLMError
from zero_agent.llm.sessions import LiteLLMSession


class RecordingTracer:
    enabled = True

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []

    def start_generation(self, **kwargs: Any) -> object:
        self.started.append(kwargs)
        return object()

    def finish_generation(self, observation: object, **kwargs: Any) -> None:
        self.finished.append(kwargs)


def _config(*, stream: bool, vision: bool = False) -> LLMBackendConfig:
    return LLMBackendConfig(
        name="default",
        provider="openai",
        api_key="sk-secret-value",
        api_base="https://api.example/v1",
        model="gpt-test",
        stream=stream,
        vision=vision,
        vision_model="gpt-vision-test" if vision else None,
    )


def _drain_chat(generator):
    chunks = []
    try:
        while True:
            chunks.append(next(generator))
    except StopIteration as exc:
        return chunks, exc.value


def _response(*, content: str, usage: Any) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    reasoning_content="",
                    tool_calls=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=usage,
    )


def test_sync_call_creates_one_generation_with_safe_request_metadata(monkeypatch) -> None:
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=4)
    response = _response(content="hello", usage=usage)
    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: response,
    )
    tracer = RecordingTracer()
    session = LiteLLMSession(_config(stream=False), tracer=tracer)

    chunks, mock = _drain_chat(
        session.chat([{"role": "user", "content": "private prompt"}], tools=[])
    )

    assert chunks == ["hello"]
    assert mock.content == "hello"
    assert len(tracer.started) == 1
    assert len(tracer.finished) == 1
    assert tracer.started[0]["model"] == "gpt-test"
    assert tracer.started[0]["input"] == {
        "messages": [{"role": "user", "content": "private prompt"}],
        "tools": [],
    }
    assert "api_key" not in tracer.started[0]["input"]
    assert tracer.finished[0]["output"]["content"] == "hello"
    assert tracer.finished[0]["usage"] is usage
    assert tracer.finished[0].get("level") is None


def test_stream_call_creates_one_generation_after_stream_is_consumed(monkeypatch) -> None:
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=6)

    def chunk(content=None, finish_reason=None, chunk_usage=None):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=content,
                        reasoning_content="",
                        tool_calls=None,
                    )
                    if content is not None
                    else None,
                    message=None,
                    finish_reason=finish_reason,
                )
            ],
            usage=chunk_usage,
        )

    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: iter(
            [
                chunk("hi "),
                chunk("there"),
                chunk(finish_reason="stop", chunk_usage=usage),
            ]
        ),
    )
    tracer = RecordingTracer()
    session = LiteLLMSession(_config(stream=True), tracer=tracer)

    streamed, mock = _drain_chat(
        session.chat([{"role": "user", "content": "hi"}], tools=[])
    )

    assert streamed == ["hi ", "there"]
    assert mock.content == "hi there"
    assert len(tracer.started) == 1
    assert len(tracer.finished) == 1
    assert tracer.finished[0]["output"]["content"] == "hi there"
    assert tracer.finished[0]["usage"] is usage
    assert tracer.finished[0].get("level") is None


def test_vision_call_creates_one_generation_with_vision_model(monkeypatch) -> None:
    response = _response(content="image understood", usage=None)
    captured: dict[str, Any] = {}

    def completion(**kwargs):
        captured.update(kwargs)
        return response

    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        completion,
    )
    monkeypatch.setattr(
        "zero_agent.llm.sessions._image_to_data_url",
        lambda image, max_pixels: "data:image/png;base64,AAAA",
    )
    tracer = RecordingTracer()
    session = LiteLLMSession(_config(stream=False, vision=True), tracer=tracer)

    assert session.vision("screen.png", "describe this") == "image understood"

    assert captured["model"] == "gpt-vision-test"
    assert len(tracer.started) == 1
    assert tracer.started[0]["model"] == "gpt-vision-test"
    assert tracer.finished[0]["output"] == {"content": "image understood"}


def test_sync_failure_finishes_generation_and_preserves_llm_error(monkeypatch) -> None:
    def fail(**kwargs):
        raise RuntimeError("upstream rejected key sk-secret-value")

    monkeypatch.setattr("zero_agent.llm.sessions.litellm.completion", fail)
    tracer = RecordingTracer()
    session = LiteLLMSession(_config(stream=False), tracer=tracer)

    with pytest.raises(LLMError):
        _drain_chat(session.chat([{"role": "user", "content": "private"}], tools=[]))

    assert len(tracer.started) == 1
    assert len(tracer.finished) == 1
    assert tracer.finished[0]["level"] == "ERROR"
    assert "sk-secret-value" not in tracer.finished[0]["status_message"]


def test_stream_interruption_finishes_generation_as_error(monkeypatch) -> None:
    def interrupted_stream():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="partial",
                        reasoning_content="",
                        tool_calls=None,
                    ),
                    message=None,
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: interrupted_stream(),
    )
    tracer = RecordingTracer()
    session = LiteLLMSession(_config(stream=True), tracer=tracer)

    streamed, mock = _drain_chat(
        session.chat([{"role": "user", "content": "hi"}], tools=[])
    )

    assert streamed == ["partial"]
    assert "流异常中断" in mock.content
    assert len(tracer.finished) == 1
    assert tracer.finished[0]["level"] == "ERROR"


def test_session_without_tracer_keeps_existing_behavior(monkeypatch) -> None:
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
    monkeypatch.setattr(
        "zero_agent.llm.sessions.litellm.completion",
        lambda **kwargs: _response(content="ok", usage=usage),
    )
    session = LiteLLMSession(_config(stream=False), tracer=None)

    chunks, mock = _drain_chat(session.chat([{"role": "user", "content": "hi"}], tools=[]))

    assert chunks == ["ok"]
    assert mock.content == "ok"
