"""工作记忆管理工具.

update_working_checkpoint: 保存任务执行中的关键信息和 SOP 引用到工作记忆.
start_long_term_update: 触发长期记忆蒸馏结算流程.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Generator

from zero_agent.core.config import AgentConfig
from zero_agent.core.types import StepAction, StepOutcome
from zero_agent.memory.manager import MemoryManager
from zero_agent.tools.registry import ToolRegistry
from zero_agent.tools.builtin.file import file_read


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
        effects=("memory",),
    ))

    registry.register(ToolDefinition(
        name="start_long_term_update",
        description=_t(
            "准备开始提炼记忆。发现值得长期记忆的信息（环境事实/用户偏好/避坑经验）时调用此工具。"
            "已记忆更新或在自主流程内时无需调用。超15轮完成的任务必须调用以沉淀经验",
            "Start distilling long-term memory. Call when discovering info worth "
            "remembering (env facts/user prefs/lessons learned). Skip if memory "
            "already updated or in autonomous flow. Must call when a task that took "
            "15+ turns is completed",
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
        effects=("memory",),
    ))

    registry.register(ToolDefinition(
        name="memory_profile",
        description=_t(
            "管理用户画像：记录/删除用户明确陈述的长期偏好、关系或承诺。"
            "不要写入一次性状态、猜测或密钥。",
            "Manage the user profile: add/remove durable preferences, relationships, "
            "or commitments the user explicitly stated.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove", "list"]},
                "section": {
                    "type": "string",
                    "enum": ["Preferences", "Relationships", "Commitments"],
                    "default": "Preferences",
                },
                "text": {"type": "string"},
            },
            "required": ["action"],
        },
        handler=_make_memory_profile_handler(config),
        evidence_kind="memory",
        category="memory",
        promotes_task_state=False,
        effects=("memory",),
    ))


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
    """创建 start_long_term_update 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        yield "[Info] Start distilling good memory for long-term storage.\n"

        sop_path = os.path.join(config.memory_dir, "sops", "memory_management_sop.md")
        if os.path.exists(sop_path):
            result = "This is L0:\n" + file_read(sop_path, show_linenos=False)
        else:
            result = "Memory Management SOP not found. Do not update memory."

        # 构建记忆蒸馏指令 prompt
        prompt = (
            "### [总结提炼经验] 既然你觉得当前任务有重要信息需要记忆，"
            "请提取最近一次任务中【事实验证成功且长期有效】的环境事实、用户偏好、重要步骤，更新记忆。\n"
            "本工具是标记开启结算过程，若已在更新记忆过程或没有值得记忆的点，忽略本次调用。\n"
            "**如果没有经验证的，未来能用上的信息，忽略本次调用！**\n"
            "**只能提取行动验证成功的信息**：\n"
            "- **环境事实**（路径/凭证/配置）→ `file_patch` 更新 L2，同步 L1\n"
            "- **复杂任务经验**（关键坑点/前置条件/重要步骤）→ L3 精简 SOP"
            "（只记你被坑得多次重试的核心要点）\n"
            "**禁止**：临时变量、具体推理过程、未验证信息、通用常识、"
            "你可以轻松复现的细节、只是做了但没有验证的信息\n"
            "**操作**：严格遵循提供的L0的记忆更新SOP。"
            "先 `file_read` 看现有 → 判断类型 → 最小化更新 → 无新内容跳过，"
            "保证对记忆库最小局部修改。\n"
        )
        memory_manager = getattr(getattr(handler, "parent", None), "memory", None)
        if memory_manager is not None and hasattr(memory_manager, "get_global_memory_context"):
            memory_ctx = memory_manager.get_global_memory_context()
        else:
            memory_ctx = MemoryManager(
                memory_dir=config.memory_dir,
                workspace_dir=config.workspace_dir,
                language=config.resolved_language,
            ).get_global_memory_context()
        prompt += memory_ctx

        return StepOutcome(result, next_prompt=prompt, action=StepAction.CONTINUE)
    return _handler


_PROFILE_SECTIONS = ("Preferences", "Relationships", "Commitments")


def _profile_manager(config: AgentConfig, handler: Any) -> MemoryManager:
    """Reuse the live agent's memory manager so paths and language match."""
    manager = getattr(getattr(handler, "parent", None), "memory", None)
    if isinstance(manager, MemoryManager):
        manager.init_memory()
        return manager
    fresh = MemoryManager(
        memory_dir=config.memory_dir,
        workspace_dir=config.workspace_dir,
        language=config.resolved_language,
    )
    fresh.init_memory()
    return fresh


def _profile_line(text: str) -> str:
    return f"- {text}"


def _insert_profile_line(profile: str, section: str, text: str) -> str:
    """Insert one bullet under ``## <section>`` without duplicating it there.

    The same line may legitimately appear under different sections (a
    preference and a commitment can share wording), so dedupe is scoped to the
    target block instead of the whole file.
    """
    line = _profile_line(text)
    lines = profile.splitlines()
    heading = f"## {section}"
    try:
        start = lines.index(heading)
    except ValueError:
        suffix = "" if profile.endswith("\n") or not profile else "\n"
        return f"{profile}{suffix}\n{heading}\n\n{line}\n"
    insert_at = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            insert_at = index
            break
    block = lines[start + 1:insert_at]
    if any(raw.strip() == line for raw in block):
        return profile
    while block and not block[-1].strip():
        block.pop()
    if not block:
        block = [""]
    block.append(line)
    rebuilt = lines[:start + 1] + block + [""] + lines[insert_at:]
    text_out = "\n".join(rebuilt).rstrip("\n") + "\n"
    return text_out


def _remove_profile_line(profile: str, section: str, text: str) -> str | None:
    """Remove the first matching bullet under ``## <section>``.

    Returns:
        Updated profile, or None when the section or the line is absent.
    """
    line = _profile_line(text)
    lines = profile.splitlines()
    heading = f"## {section}"
    try:
        start = lines.index(heading)
    except ValueError:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    for index in range(start + 1, end):
        if lines[index].strip() == line:
            del lines[index]
            return "\n".join(lines).rstrip("\n") + "\n"
    return None


def _make_memory_profile_handler(config: AgentConfig):
    """创建 memory_profile 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        action = str(args.get("action") or "").strip().lower()
        text = str(args.get("text") or "").strip()
        section = str(args.get("section") or "Preferences").strip()
        try:
            manager = _profile_manager(config, handler)
            if action == "list":
                yield "[Info] Read user profile.\n"
                return {"status": "success", "profile": manager.read_user_profile()}
            if action not in {"add", "remove"}:
                return {"status": "error", "msg": f"unknown action: {action}"}
            if section not in _PROFILE_SECTIONS:
                return {"status": "error", "msg": f"unknown section: {section}"}
            if not text:
                return {"status": "error", "msg": "text is required"}
            profile = manager.read_user_profile()
            if action == "add":
                updated = _insert_profile_line(profile, section, text)
                manager.write_user_profile(updated)
                yield f"[Info] Added to {section}.\n"
                return {"status": "success", "added": True}
            if action == "remove":
                updated = _remove_profile_line(profile, section, text)
                if updated is None:
                    return {"status": "error", "msg": "line not found"}
                manager.write_user_profile(updated)
                yield f"[Info] Removed from user profile.\n"
                return {"status": "success", "removed": True}
        except Exception as exc:  # noqa: BLE001 - 工具错误必须回报给模型
            return {"status": "error", "msg": str(exc)}
    return _handler
