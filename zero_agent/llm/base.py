"""LLM 层基础类型与抽象.

MockResponse / MockToolCall / MockFunction: 协议无关的响应包装，
用于统一不同 LLM 后端的返回格式，供 agent loop 消费.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional


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
    """

    thinking: str = ""
    content: str = ""
    tool_calls: List[MockToolCall] = field(default_factory=list)
    raw: Any = None
    stop_reason: str = "end_turn"

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
        stop_reason = "end_turn"

        if response is None:
            return cls(content=content, stop_reason=stop_reason)

        try:
            choice = response.choices[0]
            msg = choice.message if hasattr(choice, "message") and choice.message else None
            finish_reason = choice.finish_reason if hasattr(choice, "finish_reason") else "stop"

            if finish_reason:
                stop_reason = finish_reason

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
                        stop_reason = "tool_use"
        except (AttributeError, IndexError, TypeError):
            pass

        return cls(
            thinking=thinking,
            content=content,
            tool_calls=tool_calls,
            raw=response,
            stop_reason=stop_reason,
        )
