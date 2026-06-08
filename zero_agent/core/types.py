"""Agent 循环的共享数据结构.

StepOutcome: 工具 handler 返回，决定下一轮行为.
TurnResult: 单轮 agent 执行的聚合结果.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class StepOutcome:
    """工具执行结果，由 handler.dispatch() 返回.

    Attributes:
        data: 工具返回的结构化数据（dict / str / 任意值）.
        next_prompt: 下一轮 LLM 调用的 prompt.
            None/"" → 任务完成，循环退出 (CURRENT_TASK_DONE).
            其他   → 作为下一轮 user message 的 content.
        should_exit: True 则立即硬退出（用于 ask_user 等需要中断的场景）.
    """

    data: Any
    next_prompt: Optional[str] = None
    should_exit: bool = False


@dataclass
class TurnResult:
    """单轮 agent 执行的聚合结果.

    Attributes:
        turn: 当前轮次编号（从 1 开始）.
        tool_calls: 本轮 LLM 返回的工具调用列表.
        tool_results: 工具的返回结果列表（json 序列化后）.
        exit_reason: 退出原因，如 {'result': 'CURRENT_TASK_DONE', 'data': ...}.
    """

    turn: int
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    exit_reason: Optional[dict] = None
