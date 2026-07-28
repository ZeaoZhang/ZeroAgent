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
                        "实际执行任务所依赖的证据记录编号；纯回答任务留空",
                        "Evidence record numbers supporting an executed task; empty for answer-only tasks",
                        lang,
                    ),
                },
            },
            "required": ["answer"],
        },
        handler=lambda _args, _response, _handler: None,
        category="control",
    ))
