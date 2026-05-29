"""BaseHandler — 工具分发与工作状态管理.

通过 do_<tool_name> 方法约定实现工具分发，同时支持 ToolRegistry 的注册工具
作为 fallback。Handler 是 AgentLoop 与工具系统之间的桥梁。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, Generator, Optional

from zero_agent.core.types import StepOutcome
from zero_agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from zero_agent.core.agent import ZeroAgent


class BaseHandler:
    """工具分发基类.

    核心职责:
        1. dispatch(tool_name, args, response) — 两级 fallback 分发:
           a. 优先查找 self.do_<tool_name>() 方法
           b. 回退到 ToolRegistry 中注册的 handler
        2. 管理工作状态（working dict），供 update_working_checkpoint 使用.
        3. 管理 code_stop_signal，供 code_run 工具的外部终止信号.
        4. do_no_tool — 当 LLM 未调用任何工具时由引擎触发，处理空响应、
           大代码块未调用工具、任务完成声明等场景.

    Attributes:
        registry: 工具注册中心，用于 fallback 分发.
        working: 工作状态字典（key_info, related_sop 等）.
        code_stop_signal: 代码执行停止信号列表，[True] 表示终止.
        cwd: 当前工作目录.
        parent: 父级 ZeroAgent 引用，用于访问 session 等.
        max_turns: 最大轮次限制.
    """

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        cwd: str = "./workspace",
    ) -> None:
        """初始化 BaseHandler.

        Args:
            registry: 工具注册中心，None 时使用空注册中心.
            cwd: 默认工作目录.
        """
        self.registry = registry or ToolRegistry()
        self.working: Dict[str, Any] = {}
        self.code_stop_signal: list = []
        self.cwd = cwd
        self.parent: Optional[ZeroAgent] = None  # type: ignore[name-defined]  # TYPE_CHECKING
        self.max_turns: int = 80
        self.current_turn: int = 0
        self._done_hooks: list = []
        self._empty_ct: int = 0
        self.history_info: list = []  # 每轮摘要历史，用于上下文压缩

    # ---- Plan Mode ----

    def _in_plan_mode(self) -> bool:
        """检查当前是否处于 plan mode.

        Returns:
            True 如果 plan mode 激活.
        """
        return bool(self.working.get("in_plan_mode"))

    def _exit_plan_mode(self) -> None:
        """退出 plan mode，清除状态标记."""
        self.working.pop("in_plan_mode", None)

    def enter_plan_mode(self, plan_path: str) -> str:
        """进入 plan mode，追踪 plan.md 清单完成进度.

        将 max_turns 提升到 120 给计划执行留足空间.

        Args:
            plan_path: plan.md 文件路径.

        Returns:
            plan_path.
        """
        self.working["in_plan_mode"] = plan_path
        self.max_turns = 120
        print(f"[Info] Entered plan mode with plan file: {plan_path}")
        return plan_path

    def _check_plan_completion(self) -> Optional[int]:
        """检查 plan.md 中剩余未完成项的数量.

        Returns:
            剩余 `[ ]` 数量，如果计划文件不存在返回 None.
        """
        import os
        import re

        plan_path = self.working.get("in_plan_mode", "")
        if not plan_path or not os.path.isfile(plan_path):
            return None
        try:
            content = open(plan_path, encoding="utf-8", errors="replace").read()
            return len(re.findall(r"\[ \]", content))
        except Exception:
            return None

    def _lang(self) -> str:
        """获取当前解析后的语言代码.

        通过 parent.config.resolved_language 获取，
        parent 未设置时默认返回 "zh".

        Returns:
            "zh" 或 "en".
        """
        try:
            return self.parent.config.resolved_language
        except Exception:
            return "zh"

    def _tl(self, zh: str, en: str) -> str:
        """根据当前语言选择中文或英文文本.

        Args:
            zh: 中文文本.
            en: 英文文本.

        Returns:
            与 _lang() 匹配的文本.
        """
        return zh if self._lang() == "zh" else en

    def _trigger_hook(self, event: str, context: dict) -> None:
        """触发钩子事件（若 HookSystem 可用）.

        优先使用 AgentLoop 上的 HookSystem，回退到 _turn_end_hooks 字典.

        Args:
            event: 钩子事件名.
            context: 传递给钩子的上下文字典.
        """
        # 通过 parent.loop 访问 HookSystem
        try:
            loop = getattr(getattr(self, "parent", None), "loop", None)
            if loop is None:
                loop = getattr(self, "loop", None)
            if loop and hasattr(loop, "hooks") and loop.hooks:
                loop.hooks.trigger(event, context)
        except Exception:
            pass

    def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        response: Any,
        index: int = 0,
        tool_num: int = 1,
    ) -> Generator[str, None, StepOutcome]:
        """分发工具调用到对应的 handler.

        两级 fallback:
            1. 优先查找 self.do_<tool_name>() 方法.
            2. 回退到 ToolRegistry 中注册的 handler.

        Args:
            tool_name: 工具名称.
            args: 工具参数字典.
            response: LLM 响应对象（MockResponse）.
            index: 当前工具在多工具调用中的序号（从 0 开始）.
            tool_num: 本轮工具调用总数.

        Yields:
            工具执行过程中的状态字符串.

        Returns:
            StepOutcome 决定下一轮行为.
        """
        args["_index"] = index
        args["_tool_num"] = tool_num

        # tool_before 钩子
        self._trigger_hook("tool_before", {
            "tool_name": tool_name, "args": args, "response": response,
        })

        # 1. 优先查找 do_<tool_name> 方法
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            method = getattr(self, method_name)
            ret = yield from self._try_call_generator(method, args, response)
            self._trigger_hook("tool_after", {
                "tool_name": tool_name,
                "args": args,
                "outcome": ret,
                "result": ret.data if isinstance(ret, StepOutcome) else ret,
            })
            return ret

        # 2. 回退到 ToolRegistry
        tool_def = self.registry.get(tool_name)
        if tool_def is not None:
            data = yield from tool_def.handler(args, response, self)
            if isinstance(data, StepOutcome):
                ret = data
            else:
                next_prompt = (
                    data.pop("_za_next_prompt", None)
                    if isinstance(data, dict)
                    else None
                )
                if next_prompt is None:
                    next_prompt = self._default_next_prompt(args)
                ret = StepOutcome(data, next_prompt=next_prompt)
            self._trigger_hook("tool_after", {
                "tool_name": tool_name,
                "args": args,
                "outcome": ret,
                "result": ret.data,
            })
            return ret

        # 3. 未知工具
        yield self._tl(
            f"未知工具: {tool_name}\n",
            f"Unknown tool: {tool_name}\n",
        )
        ret = StepOutcome(
            None,
            next_prompt=self._tl(
                f"未知工具 {tool_name}，请检查可用工具列表",
                f"Unknown tool {tool_name}, please check available tools list",
            ),
        )
        self._trigger_hook("tool_after", {
            "tool_name": tool_name,
            "args": args,
            "outcome": ret,
            "result": ret.data,
        })
        return ret

    @staticmethod
    def _try_call_generator(
        func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Generator[Any, None, Any]:
        """调用函数，若返回 generator 则自动 yield from.

        Args:
            func: 待调用的函数.
            *args: 位置参数.
            **kwargs: 关键字参数.

        Yields:
            generator 的中间值.

        Returns:
            函数的最终返回值.
        """
        ret = func(*args, **kwargs)
        if hasattr(ret, "__iter__") and not isinstance(
            ret, (str, bytes, dict, list, tuple)
        ):
            return (yield from ret)
        return ret

    # ---- 内容提取辅助方法 ----

    @staticmethod
    def _extract_file_content(response: Any) -> Optional[str]:
        """从 LLM 响应中提取 <file_content> 标签内容.

        支持大小写变体、标签属性、以及 thinking/reasoning 内容中的标签.

        Args:
            response: LLM 响应对象（MockResponse）.

        Returns:
            提取的文件内容，未找到时返回 None.
        """
        import re as _re

        # 主搜索池：content + thinking
        sources = [getattr(response, "content", "") or ""]
        thinking = getattr(response, "thinking", "") or ""
        if thinking:
            sources.append(thinking)

        combined = "\n".join(sources)
        if not combined.strip():
            return None

        # 支持: <file_content>, <FILE_CONTENT>, <file_content attr="v"> 等
        match = _re.search(
            r"<\s*file_content[^>]*>(.*?)<\s*/\s*file_content\s*>",
            combined, _re.DOTALL | _re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()

        return None

    @staticmethod
    def _extract_code_block(
        response: Any,
        lang: str = "",
    ) -> Optional[str]:
        """从 LLM 响应中提取代码块内容.

        同时搜索 content 和 thinking 字段.

        Args:
            response: LLM 响应对象（MockResponse）.
            lang: 可选的语言标识 (e.g., "javascript", "python").

        Returns:
            提取的代码内容，未找到时返回 None.
        """
        import re as _re

        sources = [getattr(response, "content", "") or ""]
        thinking = getattr(response, "thinking", "") or ""
        if thinking:
            sources.append(thinking)
        combined = "\n".join(sources)
        if not combined.strip():
            return None

        # 优先匹配指定语言的代码块
        if lang:
            pattern = rf"```{lang}\s*\n(.*?)```"
            match = _re.search(pattern, combined, _re.DOTALL)
            if match:
                return match.group(1).strip()

        # 回退到任意代码块
        match = _re.search(r"```(?:\w+)?\s*\n(.*?)```", combined, _re.DOTALL)
        if match:
            return match.group(1).strip()

        return None

    # ---- do_bad_json ----

    def do_bad_json(
        self, args: Dict[str, Any], response: Any
    ) -> Generator[str, None, StepOutcome]:
        """处理 LLM 工具调用 JSON 格式错误.

        当 LLM 输出的工具调用参数无法解析为有效 JSON 时触发，
        将错误信息作为 next_prompt 反馈给 LLM 供其修正.

        Args:
            args: 包含 msg 等错误描述的字典.
            response: LLM 响应对象.

        Yields:
            状态信息字符串.

        Returns:
            StepOutcome 附带错误提示.
        """
        msg = args.get("msg", "bad_json")
        yield self._tl(
            f"[Warn] 工具调用 JSON 格式错误: {msg}\n",
            f"[Warn] Tool call JSON format error: {msg}\n",
        )
        return StepOutcome(None, next_prompt=f"[System] {msg}", should_exit=False)

    # ---- do_no_tool ----

    def do_no_tool(
        self, args: Dict[str, Any], response: Any
    ) -> Generator[str, None, StepOutcome]:
        """LLM 未调用任何工具时的处理.

        由 AgentLoop 检测到 response.tool_calls 为空时自动触发.
        处理以下场景:
            - 空响应 / 流异常中断 → 重试或退出.
            - 大代码块未调用工具 → 提示 LLM 调用工具.
            - 正常文本回复 → 任务完成（next_prompt=None）.

        Args:
            args: 空参数字典（由引擎注入）.
            response: LLM 响应对象（MockResponse）.

        Yields:
            状态信息字符串.

        Returns:
            StepOutcome.
        """
        content = getattr(response, "content", "") or ""
        thinking = getattr(response, "thinking", "") or ""

        # 空响应
        if not response or (not content.strip() and not thinking.strip()):
            yield self._tl(
                "[Warn] LLM 返回空响应，重试...\n",
                "[Warn] LLM returned empty response, retrying...\n",
            )
            return self._retry_or_exit(
                "[System] Blank response, regenerate and tooluse"
            )

        # 流异常中断
        if "[!!! 流异常中断" in content[-100:] or "!!!Error:" in content[-100:]:
            return self._retry_or_exit(
                "[System] Incomplete response. Regenerate and tooluse."
            )

        # max_tokens 截断
        if "max_tokens !!!]" in content[-100:]:
            return self._retry_or_exit(
                "[System] max_tokens limit reached. Use multi small steps to do it."
            )

        # Plan mode: 拦截未验证的完成声明
        plan_mode_complete_kw = [
            "任务完成", "全部完成", "已完成所有",
            "All tasks complete", "All done", "finished all",
        ]
        if self._in_plan_mode() and any(kw in content for kw in plan_mode_complete_kw):
            if (
                "VERDICT" not in content
                and "[VERIFY]" not in content
            ):
                yield self._tl(
                    "[Warn] Plan 模式完成声明拦截.\n",
                    "[Warn] Plan mode completion claim intercepted.\n",
                )
                return StepOutcome({}, next_prompt=self._tl(
                    "⛔ [验证拦截] 检测到你在 plan 模式下声称完成，但未执行 [VERIFY] 验证步骤。"
                    "请先按 plan_sop 启动验证 subagent，获得 VERDICT 后才能声称完成。",
                    "⛔ [Verify Intercept] You claimed completion in plan mode "
                    "without running [VERIFY]. Please run verification first.",
                ))

        # 检测包含较大代码块但未调用工具的情况
        code_block_pattern = r"```[a-zA-Z0-9_]*\n[\s\S]{50,}?```"
        blocks = re.findall(code_block_pattern, content)
        if len(blocks) == 1:
            m = re.search(code_block_pattern, content)
            after_block = content[m.end():]
            if not after_block.strip():
                # 移除 thinking/summary 标签后检查残留
                residual = content.replace(m.group(0), "")
                residual = re.sub(
                    r"<thinking>[\s\S]*?</thinking>", "", residual, flags=re.IGNORECASE
                )
                residual = re.sub(
                    r"<summary>[\s\S]*?</summary>", "", residual, flags=re.IGNORECASE
                )
                clean_residual = re.sub(r"\s+", "", residual)
                if len(clean_residual) <= 30:
                    yield self._tl(
                        "[Info] 检测到大代码块未调用工具，提示 LLM 调用工具.\n",
                        "[Info] Large code block without tool call detected, prompting LLM.\n",
                    )
                    next_prompt = self._tl(
                        "[System] 检测到你在上一轮回复中主要内容是较大代码块，"
                        "且本轮未调用任何工具。\n"
                        "如果这些代码需要执行、写入文件或进一步分析，请重新组织回复并显式调用相应工具"
                        "（例如：code_run、file_write、file_patch 等）；\n"
                        "如果只是向用户展示或讲解代码片段，请在回复中补充自然语言说明，"
                        "并明确是否还需要额外的实际操作。",
                        "[System] Your last reply consisted mainly of a large code block "
                        "without calling any tools.\n"
                        "If this code needs to be executed, written to a file, or further "
                        "analyzed, please reorganize your reply and explicitly call the "
                        "appropriate tool (e.g., code_run, file_write, file_patch);\n"
                        "If you are only showing or explaining the code to the user, "
                        "please add natural language explanation and clarify whether "
                        "any additional actions are needed.",
                    )
                    return StepOutcome({}, next_prompt=next_prompt)

        # Plan mode: 检查 plan.md 是否全部完成
        if self._in_plan_mode():
            remaining = self._check_plan_completion()
            if remaining == 0:
                self._exit_plan_mode()
                yield self._tl(
                    "[Info] Plan 完成：plan.md 中 0 个 [ ] 残留，退出 plan 模式.\n",
                    "[Info] Plan complete: 0 unchecked [ ] in plan.md, exiting plan mode.\n",
                )

        yield self._tl(
            "[Info] Final response to user.\n",
            "[Info] Final response to user.\n",
        )
        return StepOutcome(response, next_prompt=None)

    def _retry_or_exit(self, prompt: str) -> StepOutcome:
        """重试计数，连续 3 次空响应则硬退出.

        Args:
            prompt: 重试提示词.

        Returns:
            StepOutcome. 若 _empty_ct >= 3 则 should_exit=True.
        """
        self._empty_ct = getattr(self, "_empty_ct", 0) + 1
        if self._empty_ct >= 3:
            return StepOutcome({}, should_exit=True)
        return StepOutcome({}, next_prompt=prompt)

    # ---- 内部辅助方法 ----

    def _default_next_prompt(self, args: Dict[str, Any]) -> str:
        """为注册工具生成默认的 next_prompt.

        首个工具调用（_index == 0）时注入完整锚点上下文:
            压缩早期历史 + 最近 30 条摘要 + 工作记忆.
        后续工具调用仅注入工作记忆.

        Args:
            args: 工具参数，可能包含 _index 等元信息.

        Returns:
            默认的 prompt 字符串.
        """
        skip = args.get("_index", 0) > 0

        if not skip:
            # 完整锚点：压缩历史 + 最近摘要 + 工作记忆
            anchor = self._build_anchor_prompt()
            return anchor + self._tl(
                "\n\n[System] Continue with the next step.",
                "\n\n[System] Continue with the next step.",
            )

        # 后续工具调用：仅工作记忆
        parts: list[str] = []
        if self.working.get("key_info"):
            parts.append(
                f"<key_info>{self.working['key_info']}</key_info>"
            )
        if self.working.get("related_sop"):
            parts.append(
                self._tl(
                    f"有不清晰的地方请再次读取{self.working['related_sop']}",
                    f"If unclear, please re-read {self.working['related_sop']}",
                )
            )
        return "\n\n".join(parts) if parts else "\n"

    def _build_anchor_prompt(self) -> str:
        """构建锚点 prompt：压缩早期历史 + 最近摘要 + 工作记忆.

        类似 GenericAgent 的 _get_anchor_prompt，将 history_info 按窗口
        分割为 <earlier_context>（压缩）和 <history>（最近 30 条）.

        Returns:
            格式化的锚点 prompt 字符串.
        """
        WINDOW = 30
        parts: list[str] = []
        h = self.history_info

        if len(h) > WINDOW:
            earlier = self._fold_history(h[:-WINDOW])
            if earlier:
                parts.append(
                    f"<earlier_context>\n{earlier}\n</earlier_context>"
                )

        if h:
            recent = "\n".join(h[-WINDOW:])
            parts.append(f"<history>\n{recent}\n</history>")

        parts.append(
            self._tl(
                f"Current turn: {self.current_turn}",
                f"Current turn: {self.current_turn}",
            )
        )

        if self.working.get("key_info"):
            parts.append(
                f"<key_info>{self.working['key_info']}</key_info>"
            )
        if self.working.get("related_sop"):
            parts.append(
                self._tl(
                    f"有不清晰的地方请再次读取{self.working['related_sop']}",
                    f"If unclear, please re-read {self.working['related_sop']}",
                )
            )

        return "\n".join(parts)

    @staticmethod
    def _fold_history(lines: list) -> str:
        """压缩更早期的历史条目.

        将连续的非 [USER] 行合并为 "(N turns)" 格式.
        保留 [USER] 行原样输出，限制最多 100 行.

        Args:
            lines: history_info 条目的列表.

        Returns:
            压缩后的字符串.
        """
        FALLBACK_DIRECT = "直接回答了用户问题"
        parts: list[str] = []
        cnt = 0
        last = ""

        def flush() -> None:
            if cnt:
                if FALLBACK_DIRECT in last:
                    parts.append(f"[Agent]（{cnt} turns）")
                else:
                    parts.append(f"{last}（{cnt} turns）")

        for line in lines:
            if line.startswith("[USER]"):
                flush()
                parts.append(line)
                cnt = 0
                last = ""
            else:
                cnt += 1
                last = line
        flush()

        return "\n".join(parts[-100:])

    def turn_end_callback(
        self,
        response: Any,
        tool_calls: list,
        tool_results: list,
        turn: int,
        next_prompt: str,
        exit_reason: Optional[dict],
    ) -> str:
        """轮次结束回调，增强 next_prompt 并记录摘要历史.

        处理以下事项:
            1. Summary 提取 — 从响应中提取 <summary> 或从首个工具调用构造.
            2. 分级轮次警告 — turn%7、turn%75 时注入干预提示.
            3. 定期记忆注入 — turn%10 时注入全局记忆上下文.
            4. 文件干预 — 检查 task_dir 下的 _keyinfo / _intervene 信号文件.

        Args:
            response: LLM 响应对象.
            tool_calls: 本轮工具调用列表.
            tool_results: 工具结果列表.
            turn: 当前轮次编号.
            next_prompt: 拼接后的下一轮 prompt.
            exit_reason: 退出原因.

        Returns:
            增强后的 next_prompt 字符串.
        """
        content = getattr(response, "content", "") or ""

        # ——— 1. Summary 提取 ———
        # 去除代码块和 thinking 标签后搜索 <summary>
        clean_content = re.sub(
            r"```.*?```|<thinking>.*?</thinking>",
            "", content, flags=re.DOTALL,
        )
        summary_match = re.search(
            r"<summary>(.*?)</summary>", clean_content, re.DOTALL,
        )
        if summary_match:
            summary = summary_match.group(1).strip()
        else:
            # 从第一个工具调构造摘要
            tc = tool_calls[0] if tool_calls else {"tool_name": "no_tool", "args": {}}
            tool_name = tc["tool_name"]
            clean_args = {k: v for k, v in tc.get("args", {}).items()
                          if not k.startswith("_")}
            if tool_name == "no_tool":
                summary = self._tl("直接回答了用户问题", "Answered the user directly")
            else:
                args_str = str(clean_args)
                if len(args_str) > 40:
                    args_str = args_str[:40] + "..."
                summary = self._tl(
                    f"调用工具{tool_name}, args: {args_str}",
                    f"Called {tool_name}, args: {args_str}",
                )
            # 提醒 LLM 在回复中加上 <summary>
            next_prompt += self._tl(
                "\n\n\n[SYSTEM] 必须在回复文本中包含<summary>！\n\n",
                "\n\n\n[SYSTEM] You must include a <summary> in your reply!\n\n",
            )
        # 压缩摘要长度
        summary = summary.replace("\n", "")
        if len(summary) > 80:
            summary = summary[:80]
        self.history_info.append(f"[Agent] {summary}")

        # ——— 2. 分级轮次警告 ———
        if turn % 75 == 0:
            next_prompt += self._tl(
                f"\n\n[DANGER] 已连续执行第 {turn} 轮。"
                "必须总结情况进行ask_user，不允许继续重试。",
                f"\n\n[DANGER] Already executed {turn} consecutive turns. "
                "You must summarize and call ask_user. No further retries allowed.",
            )
        elif turn % 7 == 0:
            next_prompt += self._tl(
                f"\n\n[DANGER] 已连续执行第 {turn} 轮。"
                "禁止无效重试。若无有效进展，必须切换策略："
                "1. 探测物理边界 2. 请求用户协助。"
                "如有需要，可调用 update_working_checkpoint 保存关键上下文。",
                f"\n\n[DANGER] Already executed {turn} consecutive turns. "
                "Stop ineffective retries. If no real progress, switch strategy: "
                "1. Probe physical boundaries 2. Request user assistance. "
                "Call update_working_checkpoint if needed to save key context.",
            )

        # ——— 3. 定期记忆注入 ———
        elif turn % 10 == 0:
            if self.parent is not None and hasattr(self.parent, "memory"):
                memory_ctx = self.parent.memory.get_global_memory_context()
                if memory_ctx:
                    next_prompt += f"\n\n[Memory Refresh]\n{memory_ctx}"

        # ─── 3.5 Plan Mode 提示 ───
        if self._in_plan_mode():
            _plan = self.working.get("in_plan_mode", "")
            remaining = self._check_plan_completion()
            if remaining is not None and remaining > 0:
                # 每 5 轮（从第 10 轮起）注入计划文件路径，强制 agent 重读
                if turn >= 10 and turn % 5 == 0:
                    next_prompt = self._tl(
                        f"[Plan Hint] 正在计划模式。必须 file_read({_plan}) "
                        "确认当前步骤，回复开头引用：📌 当前步骤：...\n\n",
                        f"[Plan Hint] In plan mode. Must file_read({_plan}) "
                        "to confirm current step, start reply with: 📌 Current step: ...\n\n",
                    ) + next_prompt
                else:
                    next_prompt += self._tl(
                        f"\n[Plan Mode] plan.md 剩余 {remaining} 个 [ ] 未完成项。"
                        "继续按计划执行，完成后调用 [VERIFY] 验证。",
                        f"\n[Plan Mode] {remaining} unchecked items in plan.md. "
                        "Continue executing, then run [VERIFY].",
                    )

        # ——— 4. 文件干预 ———
        if self.parent is not None:
            task_dir = getattr(self.parent, "task_dir", None)
            if task_dir:
                from zero_agent.utils.files import consume_file

                inj_keyinfo = consume_file(task_dir, "_keyinfo")
                if inj_keyinfo:
                    self.working["key_info"] = (
                        self.working.get("key_info", "")
                        + f"\n[MASTER] {inj_keyinfo}"
                    )

                inj_prompt = consume_file(task_dir, "_intervene")
                if inj_prompt:
                    next_prompt += f"\n\n[MASTER] {inj_prompt}\n"

                # _stop 信号文件：触发任务终止
                stop_signal = consume_file(task_dir, "_stop")
                if stop_signal:
                    extra = (
                        f"\n\n[MASTER STOP] {stop_signal}"
                        if stop_signal.strip()
                        else ""
                    )
                    next_prompt += extra
                    self.code_stop_signal.append(1)
                    if self.parent is not None:
                        try:
                            self.parent.abort()
                        except Exception:
                            pass

        # ─── 5. Execute _turn_end_hooks ───
        if self.parent is not None:
            hooks_dict = getattr(self.parent, '_turn_end_hooks', {})
            for hook in list(hooks_dict.values()):
                try:
                    hook(locals())
                except Exception:
                    pass

        return next_prompt
