"""工作记忆管理工具.

update_working_checkpoint: 保存任务执行中的关键信息和 SOP 引用到工作记忆.
start_long_term_update: 触发长期记忆蒸馏结算流程.
"""

from __future__ import annotations

from typing import Any, Dict, Generator

from zero_agent.core.config import AgentConfig
from zero_agent.core.types import StepAction, StepOutcome
from zero_agent.tools.registry import ToolRegistry


def _t(zh: str, en: str, lang: str) -> str:
    """根据语言选择中文或英文文本."""
    return zh if lang == "zh" else en


def register_memory_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """注册记忆管理工具到 ToolRegistry.

    Args:
        registry: 工具注册中心.
        config: Agent 配置.
    """
    from zero_agent.tools.registry import ToolDefinition

    lang = config.resolved_tool_language

    registry.register(ToolDefinition(
        name="update_working_checkpoint",
        description=_t(
            "短期工作便签，每轮自动注入上下文，防长任务信息丢失。前中期调用，非结束时。"
            "何时调用：(1)任务开始读SOP后，存用户需求和关键约束/参数（简单1-2步任务除外）；"
            "(2)子任务切换或上下文即将被冲刷前；(3)多次重试失败后，重读SOP并必须调用存储新发现；"
            "(4)切换新任务时更新内容，清旧进度但保留仍有效的约束。\n\n"
            "何时不调用：简单任务（1-2步且无严重约束）、任务已完成时（应当用长期结算工具）",
            "Short-term working notepad, auto-injected each turn to prevent info loss "
            "in long tasks. Call during early/mid stages, not at end. When: "
            "(1) after reading SOP, store user needs & key constraints (skip for simple "
            "1-2 step tasks); (2) before subtask switch or context flush; "
            "(3) after repeated failures, re-read SOP and must store new findings; "
            "(4) on new task, update content, clear old progress but keep valid constraints.\n\n"
            "Don't call: simple tasks (1-2 steps), task completed (use long-term memory tool)",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "key_info": {
                    "type": "string",
                    "description": _t(
                        "替换当前便签（<200 tokens）。增量更新：先回顾现有内容，保留仍有效的，再增删改。"
                        "存：要避的坑、用户原始需求、关键参数/发现、文件路径、当前进度、下一步计划。"
                        "不存：马上要用用完即丢的、上下文中显而易见的、用户已换全新任务时的旧任务信息。"
                        "宁多更新不丢关键",
                        "Replaces current notepad (<200 tokens). Incremental update: review "
                        "existing, keep valid, add/remove/modify. Store: pitfalls, user "
                        "requirements, key params/findings, file paths, progress, next steps. "
                        "Don't store: ephemeral info, obvious context, old task info when user "
                        "switched tasks. Prefer over-updating over losing key info",
                        lang,
                    ),
                },
                "related_sop": {
                    "type": "string",
                    "description": _t(
                        "相关sop名称，可以多个，必要时需要再读",
                        "Related SOP names, tips for further re-read",
                        lang,
                    ),
                },
            },
        },
        handler=_make_update_working_checkpoint_handler(config),
        evidence_kind="memory",
        category="memory",
        promotes_task_state=False,
    ))

    long_term_tool = ToolDefinition(
        name="start_long_term_update",
        description=_t(
            "旧入口仅返回迁移提示，不会写入记忆。请改用 memory_update，并提供有效来源回执。",
            "Compatibility name only; it does not write memory. Use memory_update with valid source receipts.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=_make_start_long_term_update_handler(config),
        evidence_kind="memory",
        category="memory",
        promotes_task_state=False,
    )
    registry.register(long_term_tool)


def _make_update_working_checkpoint_handler(config: AgentConfig):
    """创建 update_working_checkpoint 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        key_info = args.get("key_info", "")
        related_sop = args.get("related_sop", "")

        if hasattr(handler, "working"):
            if "key_info" in args:
                handler.working["key_info"] = key_info
            if "related_sop" in args:
                handler.working["related_sop"] = related_sop
            handler.working["passed_sessions"] = 0

        yield f"[Info] Updated key_info and related_sop.\n"
        return {"result": "working key_info updated"}
    return _handler


def _make_start_long_term_update_handler(config: AgentConfig):
    """Keep the old call name as a no-write pointer to memory_update."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        yield "[Info] Long-term memory is managed through memory_update.\n"
        return StepOutcome(
            {
                "status": "error",
                "code": "MEMORY_UPDATE_REQUIRES_SERVICE",
                "msg": (
                    "Use memory_update with source-bound evidence to create or revise "
                    "managed memory. This compatibility tool does not read or write files."
                ),
            },
            next_prompt="\n",
            action=StepAction.CONTINUE,
        )
    return _handler
