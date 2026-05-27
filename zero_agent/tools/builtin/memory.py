"""工作记忆管理工具.

update_working_checkpoint: 保存任务执行中的关键信息和 SOP 引用到工作记忆.
start_long_term_update: 触发长期记忆蒸馏结算流程.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Generator

from zero_agent.core.config import AgentConfig
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
            "更新当前任务的工作记忆检查点。用于记录任务执行过程中的关键信息、"
            "当前进度、重要发现和参考 SOP。这些信息会随后续轮次传递给 LLM，"
            "帮助模型保持上下文连贯性。一般在任务开始或关键阶段转换时调用。",
            "Update the working memory checkpoint for the current task. "
            "Records key information, current progress, important findings, "
            "and SOP references during task execution. This context is "
            "passed to the LLM in subsequent turns to maintain coherence. "
            "Typically called at task start or key phase transitions.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "key_info": {
                    "type": "string",
                    "description": _t(
                        "需要记住的关键信息摘要，会覆盖之前的工作记忆",
                        "Key information summary to remember; overwrites previous working memory",
                        lang,
                    ),
                },
                "related_sop": {
                    "type": "string",
                    "description": _t(
                        "关联的 SOP（标准操作流程）标识或描述",
                        "Associated SOP (Standard Operating Procedure) identifier or description",
                        lang,
                    ),
                },
            },
            "required": ["key_info"],
        },
        handler=_make_update_working_checkpoint_handler(config),
        category="memory",
    ))

    registry.register(ToolDefinition(
        name="start_long_term_update",
        description=_t(
            "开启长期记忆结算流程。当 Agent 认为当前任务完成后有重要信息"
            "（环境事实、用户偏好、关键步骤）需要持久记忆时调用此工具。"
            "调用后会返回记忆管理 SOP，Agent 需按 SOP 进行最小化记忆更新。"
            "若无经验证且未来可用的信息，忽略此工具。",
            "Start the long-term memory distillation process. "
            "Call this when the agent believes important information "
            "(environment facts, user preferences, key steps) from the "
            "completed task should be persisted. Returns the memory "
            "management SOP; the agent should follow it for minimal updates. "
            "Skip this tool if there is no verified, reusable information.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=_make_start_long_term_update_handler(config),
        category="memory",
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

        sop_path = os.path.join(config.memory_dir, "memory_management_sop.md")
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

        # 通过 _za_next_prompt 传递自定义 next_prompt，由 dispatch 提取
        return {"result": result, "_za_next_prompt": prompt}
    return _handler
