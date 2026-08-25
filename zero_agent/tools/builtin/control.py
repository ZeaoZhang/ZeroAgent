"""Explicit task completion control tool."""

from __future__ import annotations

from zero_agent.core.config import AgentConfig
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def _t(zh: str, en: str, lang: str) -> str:
    return zh if lang == "zh" else en


def register_control_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """Register the completion schema; BaseHandler owns its state transition."""

    lang = config.resolved_tool_language
    registry.register(ToolDefinition(
        name="complete_task",
        description=_t(
            "当前用户请求已经完整回答或执行完毕时必须调用。若任务调用过实际工具，evidence_refs 必须引用本任务证据账本中的成功记录编号。",
            "Must be called when the current user request is fully answered or executed. If real tools were used, evidence_refs must reference successful record numbers in this task's evidence ledger.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": _t("直接交付给用户的最终回答", "Final answer delivered directly to the user", lang),
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "description": _t(
                        "证据记录编号数组；纯回答任务传空数组，执行过实际工具的任务必须引用本任务证据账本中的成功记录编号",
                        "Evidence record numbers; use [] for answer-only tasks, and reference successful records from this task's evidence ledger after real tool execution",
                        lang,
                    ),
                },
            },
            "required": ["answer", "evidence_refs"],
        },
        handler=lambda _args, _response, _handler: None,
        category="control",
    ))
