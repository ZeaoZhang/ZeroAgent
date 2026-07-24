"""LLM 层基础类型与抽象.

MockResponse / MockToolCall / MockFunction: 协议无关的响应包装，
用于统一不同 LLM 后端的返回格式，供 agent loop 消费.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Literal, Optional


@dataclass
class MockFunction:
    """工具调用中的 function 块.

    Attributes:
        name: 工具名称.
        arguments: JSON 字符串形式的工具参数.
    """

    name: str
    arguments: str


@dataclass
class MockToolCall:
    """单个工具调用.

    Attributes:
        function: 工具函数信息（name + arguments）.
        id: 工具调用唯一标识，用于关联 tool_result.
    """

    function: MockFunction
    id: str = ""


@dataclass
class MockResponse:
    """协议无关的 LLM 响应包装.

    Attributes:
        thinking: 模型的思考/推理内容（thinking/reasoning 块）.
        content: 模型的文本回复内容.
        tool_calls: 模型请求的工具调用列表.
        raw: 原始响应数据，用于调试.
        stop_reason: 停止原因 "end_turn" | "tool_use" | "max_tokens" 等.
        usage: provider usage/cost metadata, when available.
        tool_protocol: "native" for provider tool calls, "text" for text protocol fallback.
    """

    thinking: str = ""
    content: str = ""
    tool_calls: List[MockToolCall] = field(default_factory=list)
    raw: Any = None
    stop_reason: str = "end_turn"
    usage: Any = None
    tool_protocol: Literal["native", "text"] = "native"

    def __repr__(self) -> str:
        return (
            f"<MockResponse thinking={bool(self.thinking)}, "
            f"content='{self.content[:50]}...' if len(self.content) > 50 else "
            f"content='{self.content}', "
            f"tools={len(self.tool_calls)}>"
        )

    @classmethod
    def from_litellm_response(cls, response: Any, streamed_text: str = "") -> "MockResponse":
        """从 litellm ModelResponse 构建 MockResponse.

        Args:
            response: litellm 返回的 ModelResponse 对象.
            streamed_text: 流式模式下累积的文本内容.

        Returns:
            MockResponse 实例.
        """
        thinking = ""
        content = streamed_text
        tool_calls: List[MockToolCall] = []
        usage = None
        stop_reason = "end_turn"

        if response is None:
            return cls(content=content, stop_reason=stop_reason, usage=usage)

        try:
            choice = response.choices[0]
            msg = choice.message if hasattr(choice, "message") and choice.message else None
            finish_reason = choice.finish_reason if hasattr(choice, "finish_reason") else "stop"

            if finish_reason:
                stop_reason = normalize_stop_reason(finish_reason, has_tool_calls=False)

            usage = _extract_usage(response)
            if msg:
                # 文本内容
                if hasattr(msg, "content") and msg.content:
                    content = msg.content

                # thinking/reasoning 内容（OpenAI o1/o3 等）
                if hasattr(msg, "reasoning_content") and msg.reasoning_content:
                    thinking = msg.reasoning_content

                # 工具调用
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        func = tc.function if hasattr(tc, "function") else None
                        if func:
                            tool_calls.append(MockToolCall(
                                function=MockFunction(
                                    name=func.name if hasattr(func, "name") else "",
                                    arguments=func.arguments if hasattr(func, "arguments") else "{}",
                                ),
                                id=tc.id if hasattr(tc, "id") else "",
                            ))
                    if tool_calls:
                        stop_reason = normalize_stop_reason(stop_reason, has_tool_calls=True)
        except (AttributeError, IndexError, TypeError):
            pass

        return cls(
            thinking=thinking,
            content=content,
            tool_calls=tool_calls,
            raw=response,
            stop_reason=normalize_stop_reason(stop_reason, has_tool_calls=bool(tool_calls)),
            usage=usage,
            tool_protocol="native",
        )


_PROTECTED_STOP_REASONS = {
    "max_tokens",
    "stream_interrupted",
    "interrupted",
    "cancelled",
    "content_filter",
    "error",
}


def normalize_stop_reason(raw_reason: Any, *, has_tool_calls: bool) -> str:
    """Normalize provider finish reasons into the ZeroAgent stop taxonomy."""
    if raw_reason is None or raw_reason == "":
        return "tool_use" if has_tool_calls else "end_turn"

    reason = str(raw_reason)
    normalized = reason.lower()
    if normalized in {"stop", "end_turn"}:
        return "tool_use" if has_tool_calls else "end_turn"
    if normalized in {"tool_calls", "tool_use"}:
        return "tool_use"
    if normalized in {"length", "max_tokens"}:
        return "max_tokens"
    if normalized in _PROTECTED_STOP_REASONS:
        return reason
    if has_tool_calls:
        return "tool_use"
    return f"unknown:{reason}"


def merge_mock_responses(
    parsed: MockResponse,
    backend: Optional[MockResponse],
    *,
    raw_text: str,
    protocol: Literal["native", "text"],
) -> MockResponse:
    """Merge parsed text-protocol fields with backend metadata losslessly."""
    backend = backend if isinstance(backend, MockResponse) else None

    content = parsed.content or (backend.content if backend else "")
    thinking = parsed.thinking or (backend.thinking if backend else "")
    tool_calls = parsed.tool_calls or (backend.tool_calls if backend else [])

    parsed_stop = normalize_stop_reason(parsed.stop_reason, has_tool_calls=bool(parsed.tool_calls))
    backend_stop = (
        normalize_stop_reason(backend.stop_reason, has_tool_calls=bool(backend.tool_calls))
        if backend
        else ""
    )
    if _is_protected_stop_reason(backend_stop) or backend_stop.startswith("unknown:"):
        stop_reason = backend_stop
    elif parsed.tool_calls:
        stop_reason = "tool_use"
    elif backend_stop:
        stop_reason = backend_stop
    else:
        stop_reason = parsed_stop

    usage = (
        (backend.usage if backend else None)
        or (_extract_usage(backend.raw) if backend else None)
        or parsed.usage
        or _extract_usage(parsed.raw)
    )

    if protocol == "text":
        raw = {
            "protocol": "text",
            "backend_raw": backend.raw if backend else None,
            "text_raw": raw_text,
        }
    else:
        raw = backend.raw if backend else parsed.raw

    return MockResponse(
        thinking=thinking,
        content=content,
        tool_calls=tool_calls,
        raw=raw,
        stop_reason=stop_reason,
        usage=usage,
        tool_protocol=protocol,
    )


def _is_protected_stop_reason(stop_reason: str) -> bool:
    return stop_reason in _PROTECTED_STOP_REASONS or stop_reason.lower() in _PROTECTED_STOP_REASONS


def _extract_usage(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw.get("usage")
    return getattr(raw, "usage", None)
