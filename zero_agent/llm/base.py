"""LLM 层基础类型与抽象.

MockResponse / MockToolCall / MockFunction: 协议无关的响应包装，
用于统一不同 LLM 后端的返回格式，供 agent loop 消费.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
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
    if isinstance(raw, Mapping):
        return raw.get("usage")
    return getattr(raw, "usage", None)

_USAGE_MISSING = object()
_USAGE_ZERO = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "cache_miss_tokens": 0,
}


def _usage_value(usage: Any, path: tuple[str, ...]) -> Any:
    """Read one usage field from either mappings or attribute objects."""
    current = usage
    for key in path:
        if isinstance(current, Mapping):
            current = current.get(key, _USAGE_MISSING)
        else:
            current = getattr(current, key, _USAGE_MISSING)
        if current is _USAGE_MISSING:
            return _USAGE_MISSING
    return current


def _first_usage_value(usage: Any, aliases: tuple[tuple[str, ...], ...]) -> Any:
    for path in aliases:
        value = _usage_value(usage, path)
        if value is not _USAGE_MISSING and value is not None:
            return value
    return None


def _first_usage_value_with_path(
    usage: Any, aliases: tuple[tuple[str, ...], ...]
) -> tuple[Any, tuple[str, ...] | None]:
    for path in aliases:
        value = _usage_value(usage, path)
        if value is not _USAGE_MISSING and value is not None:
            return value, path
    return None, None


def _usage_int(value: Any) -> int:
    """Return a non-negative integral token count, or zero when invalid."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, str):
        value = value.strip()
        if not value.isdecimal():
            return 0
        return int(value)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    try:
        return number if number >= 0 and value == number else 0
    except Exception:
        return 0


_INPUT_USAGE_ALIASES = (("prompt_tokens",), ("input_tokens",))
_OUTPUT_USAGE_ALIASES = (("completion_tokens",), ("output_tokens",))
_CACHE_READ_USAGE_ALIASES = (
    ("cache_read_input_tokens",),
    ("prompt_cache_hit_tokens",),
    ("cache_read",),
    ("prompt_tokens_details", "cached_tokens"),
    ("input_tokens_details", "cached_tokens"),
)
_CACHE_CREATION_USAGE_ALIASES = (
    ("cache_creation_input_tokens",),
    ("cache_creation",),
    ("cache_write",),
    ("prompt_tokens_details", "cache_creation_tokens"),
    ("prompt_tokens_details", "cache_write_tokens"),
    ("input_tokens_details", "cache_creation_tokens"),
    ("input_tokens_details", "cache_write_tokens"),
)
_CACHE_MISS_USAGE_ALIASES = (
    ("prompt_cache_miss_tokens",),
    ("cache_miss_input_tokens",),
    ("cache_miss",),
)


def _has_valid_cache_value(
    usage: Any, aliases: tuple[tuple[str, ...], ...]
) -> bool:
    value = _first_usage_value(usage, aliases)
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return value.strip().isdecimal()
    try:
        number = int(value)
        return number >= 0 and value == number
    except (TypeError, ValueError, OverflowError):
        return False


def usage_has_cache_metrics(usage: Any) -> bool:
    """Return whether provider usage exposed valid cache metadata."""
    if usage is None:
        return False
    return any(
        _has_valid_cache_value(usage, aliases)
        for aliases in (
            _CACHE_READ_USAGE_ALIASES,
            _CACHE_CREATION_USAGE_ALIASES,
            _CACHE_MISS_USAGE_ALIASES,
        )
    )


def extract_usage_metrics(usage: Any) -> dict[str, int]:
    """Normalize provider/LiteLLM usage into canonical token metrics."""
    result = dict(_USAGE_ZERO)
    if usage is None or isinstance(usage, (str, bytes, int, float, bool)):
        return result

    input_value, input_path = _first_usage_value_with_path(usage, _INPUT_USAGE_ALIASES)
    output_value = _first_usage_value(usage, _OUTPUT_USAGE_ALIASES)
    read_value, read_path = _first_usage_value_with_path(usage, _CACHE_READ_USAGE_ALIASES)
    creation_value = _first_usage_value(usage, _CACHE_CREATION_USAGE_ALIASES)
    miss_value = _first_usage_value(usage, _CACHE_MISS_USAGE_ALIASES)
    result["input_tokens"] = _usage_int(input_value)
    result["output_tokens"] = _usage_int(output_value)
    result["cache_read_tokens"] = _usage_int(read_value)
    result["cache_creation_tokens"] = _usage_int(creation_value)
    if miss_value is None and usage_has_cache_metrics(usage):
        # Anthropic reports regular input separately from cache reads; OpenAI's
        # nested cached_tokens sits inside an inclusive input total.
        result["cache_miss_tokens"] = (
            result["input_tokens"]
            if input_path == ("input_tokens",)
            and read_path == ("cache_read_input_tokens",)
            else max(result["input_tokens"] - result["cache_read_tokens"], 0)
        )
    else:
        result["cache_miss_tokens"] = _usage_int(miss_value)
    return result
