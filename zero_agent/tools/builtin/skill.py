"""Model-facing skill invocation over the pinned SkillRuntime."""

from __future__ import annotations

from typing import Any, Dict, Generator

from zero_agent.context.errors import ContextError
from zero_agent.core.config import AgentConfig
from zero_agent.skills.runtime import SkillInvocation
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def register_skill_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    lang = config.resolved_tool_language
    registry.register(ToolDefinition(
        name="skill_route",
        description=(
            "在任务完成或使用操作工具前声明已触发的内置规则/技能；每项提供用户请求中的原文片段，无触发项时传空数组。"
            if lang == "zh"
            else "Declare triggered built-in rules or skills before completing the task or using action tools. Quote matching user-request text for each item; use an empty array when none apply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "skills": {
                    "type": "array",
                    "maxItems": 29,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                },
                "trigger_evidence": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 500},
                },
            },
            "required": ["skills", "trigger_evidence"],
        },
        handler=_skill_route,
        category="skill",
        evidence_kind="read",
        promotes_task_state=False,
        contract_version="v1",
    ))
    description = (
        "按精确 skill URI 和修订加载只读技能，或在授权能力范围内执行已声明入口；不会搜索宿主全局脚本。"
        if lang == "zh"
        else "Load a pinned skill by URI and revision, or execute its declared entrypoint with granted capabilities."
    )
    registry.register(ToolDefinition(
        name="skill_run",
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "skill_uri": {"type": "string"},
                "revision": {"type": "string", "minLength": 64, "maxLength": 64},
                "entrypoint": {"type": "string", "minLength": 1, "maxLength": 80},
                "arguments": {"type": "object"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            "required": ["skill_uri", "revision", "entrypoint"],
        },
        handler=_skill_run,
        category="skill",
        evidence_kind="execute",
        promotes_task_state=True,
        contract_version="v1",
        skill_action="skill.execute",
    ))


def _skill_route(args: Dict[str, Any], _response: Any, handler: Any) -> dict:
    gate = getattr(handler, "skill_action_gate", None)
    if gate is None:
        return {
            "status": "error",
            "code": "BACKEND_UNAVAILABLE",
            "message": "The task skill router is not initialized.",
        }
    try:
        return gate.route(args.get("skills"), args.get("trigger_evidence"))
    except ContextError as exc:
        return {"status": "error", "code": exc.code, "message": str(exc)}


def _skill_run(args: Dict[str, Any], _response: Any, handler: Any) -> Generator[str, None, dict]:
    runtime = getattr(handler, "skill_runtime", None)
    if runtime is None:
        return {
            "status": "error",
            "code": "BACKEND_UNAVAILABLE",
            "message": "The task skill runtime is not initialized.",
        }
    try:
        invocation = SkillInvocation(
            skill_uri=args.get("skill_uri"),
            revision=args.get("revision"),
            entrypoint=args.get("entrypoint"),
            arguments=args.get("arguments", {}),
            timeout_seconds=args.get("timeout_seconds", 60),
        )
        result = yield from runtime.run(invocation, handler=handler)
        if result.get("status") == "loaded":
            gate = getattr(handler, "skill_action_gate", None)
            if gate is not None:
                gate.mark_loaded(result.get("skill_uri"), result.get("skill_revision"))
        return result
    except ContextError as exc:
        return {"status": "error", "code": exc.code, "message": str(exc)}
    except Exception:
        return {
            "status": "error",
            "code": "INVALID_SOURCE",
            "message": "The skill request could not be processed.",
        }
    yield ""
