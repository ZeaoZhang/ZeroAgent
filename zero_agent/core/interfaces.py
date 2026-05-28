"""核心接口的 Protocol 定义.

轻量级 Protocol 类用于改进类型安全，替代 core/loop.py 和 core/handler.py 中的 Any 参数.
Protocol 在运行时无开销（structural subtyping），仅用于静态类型检查.

使用方式:
    from zero_agent.core.interfaces import LLMClient, ToolDispatcher
"""

from __future__ import annotations

from typing import Any, Dict, Generator, List, Optional, Protocol


class LLMClient(Protocol):
    """LLM 会话的协议接口.

    由 LiteLLMSession 和 AutoFailoverSession 实现.
    """

    system: Optional[str]
    temperature: float
    max_tokens: Optional[int]
    name: str
    last_tools: str

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Generator[str, None, Any]:
        """发送消息到 LLM 并获取流式响应."""
        ...


class ToolDispatcher(Protocol):
    """工具分发器的协议接口.

    由 BaseHandler 实现.
    """

    loop: Any  # AgentLoop 循环引用
    parent: Any  # ZeroAgent 循环引用
    cwd: str

    def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        response: Any,
    ) -> Generator[str, None, Any]:
        """分发工具调用."""
        ...

    def turn_end_callback(
        self,
        next_prompts: List[str],
        ctx: Dict[str, Any],
    ) -> List[str]:
        """轮次结束回调."""
        ...
