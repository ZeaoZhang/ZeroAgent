"""ask_user — 中断 agent 循环请求用户输入.

返回 INTERRUPT 状态，包含问题和可选候选项，
由 AgentLoop 检测 should_exit 后将控制权交还给调用方.
"""

from __future__ import annotations

from typing import Any, Dict, Generator, List, Optional

from zero_agent.core.config import AgentConfig
from zero_agent.core.types import StepOutcome
from zero_agent.tools.registry import ToolRegistry


def _t(zh: str, en: str, lang: str) -> str:
    """根据语言选择中文或英文文本."""
    return zh if lang == "zh" else en


def ask_user(question: str, candidates: Optional[List[str]] = None) -> dict:
    """构建用户中断请求.

    此函数不包含副作用，仅构造返回数据结构。
    由 AgentLoop 在检测到 should_exit=True 时中断循环。

    Args:
        question: 向用户提出的问题.
        candidates: 可选的候选项列表，供用户选择.

    Returns:
        {"status": "INTERRUPT", "intent": "HUMAN_INTERVENTION",
         "data": {"question": ..., "candidates": [...]}}
    """
    return {
        "status": "INTERRUPT",
        "intent": "HUMAN_INTERVENTION",
        "data": {"question": question, "candidates": candidates or []},
    }


def register_user_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """注册用户交互工具到 ToolRegistry.

    Args:
        registry: 工具注册中心.
        config: Agent 配置.
    """
    from zero_agent.tools.registry import ToolDefinition

    lang = config.resolved_tool_language

    registry.register(ToolDefinition(
        name="ask_user",
        description=_t(
            "向用户提问以获取信息、确认操作或做出选择。"
            "当任务需要人工判断、补充信息或确认敏感操作时调用。"
            "调用后 agent 循环将中断，等待用户回复后继续。",
            "Ask the user a question to get information, confirm an action, "
            "or make a choice. Call this when the task requires human judgment, "
            "additional input, or confirmation of sensitive operations. "
            "The agent loop will pause and wait for the user's reply.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": _t(
                        "向用户提出的问题",
                        "The question to ask the user",
                        lang,
                    ),
                },
                "candidates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": _t(
                        "可选的候选项列表，供用户从中选择",
                        "Optional list of choices for the user to pick from",
                        lang,
                    ),
                },
            },
        },
        handler=_make_ask_user_handler(config),
        category="user",
    ))


def _make_ask_user_handler(config: AgentConfig):
    """创建 ask_user 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, StepOutcome]:
        question = args.get("question", "请提供输入：")
        candidates = args.get("candidates", [])
        yield f"Waiting for your answer ...\n"
        return StepOutcome(
            ask_user(question, candidates),
            next_prompt="",
            should_exit=True,
        )
    return _handler
