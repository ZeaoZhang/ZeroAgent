"""BaseHandler — 工具分发与工作状态管理.

通过 do_<tool_name> 方法约定实现工具分发，同时支持 ToolRegistry 的注册工具
作为 fallback。Handler 是 AgentLoop 与工具系统之间的桥梁。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Dict, Generator, Optional

from zero_agent.core.completion import evaluate_completion
from zero_agent.core.completion_gate import CompletionGate, CompletionGateAction
from zero_agent.core.types import (
    EvidenceLedger,
    EvidenceRecord,
    StepAction,
    StepOutcome,
    TaskContract,
    TaskMode,
    TerminalEvent,
    TerminalStatus,
)
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
        self.completion_certificate = None
        self.task_contract = TaskContract(
            task_id="handler-default",
            user_request="",
            mode=TaskMode.OPEN,
        )
        self.evidence_ledger = EvidenceLedger()
        self.plan_verify_status = "missing"
        self.completion_gate = CompletionGate(
            retry_prompt_factory=self._native_tool_retry_prompt,
            large_code_prompt_factory=self._large_code_block_retry_prompt,
            plan_verification_prompt_factory=self._plan_verification_retry_prompt,
            in_plan_mode=self._in_plan_mode,
            check_plan_completion=self._check_plan_completion,
        )


    # ---- Stop Signal ----

    def request_code_stop(self) -> None:
        """请求当前正在执行的 code_run 停止."""
        if not self.code_stop_signal:
            self.code_stop_signal.append(1)

    def reset_code_stop_signal(self) -> None:
        """清除上一任务遗留的 code_run 停止信号."""
        self.code_stop_signal.clear()

    def reset_session_state(self) -> None:
        """新任务开始时重置跨任务的循环内部状态.

        归零 ``_empty_ct``（连续空响应计数）并清空 CompletionGate 的
        retry 预算。避免前序任务遗留的计数在本任务内累积触发硬退出，
        也避免被误判的 retry 把可用余量用尽.
        """
        self._empty_ct = 0
        self.completion_certificate = None
        self.completion_gate.reset()

    # ---- Plan Mode ----

    def _in_plan_mode(self) -> bool:
        """Return whether the immutable task contract is in PLAN state."""

        return self.task_contract.mode is TaskMode.PLAN


    def enter_plan_mode(self, plan_path: str) -> str:
        """进入 plan mode，追踪 plan.md 清单完成进度.

        将 max_turns 提升到 120 给计划执行留足空间.

        Args:
            plan_path: plan.md 文件路径.

        Returns:
            plan_path.
        """
        self.task_contract = TaskContract(
            task_id=self.task_contract.task_id,
            user_request=self.task_contract.user_request,
            mode=TaskMode.PLAN,
            plan_path=plan_path,
        )
        self.plan_verify_status = "missing"
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

        plan_path = self.task_contract.plan_path or ""
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
        if tool_name != "no_tool":
            self.completion_gate.reset()
            self._mark_executing(tool_name)

        # tool_before 钩子
        self._trigger_hook("tool_before", {
            "tool_name": tool_name, "args": args, "response": response,
        })

        # 1. 优先查找 do_<tool_name> 方法
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            method = getattr(self, method_name)
            ret = yield from self._try_call_generator(method, args, response)
            self._record_evidence(tool_name, args, ret)
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
                ret = StepOutcome(data, next_prompt=next_prompt, action=StepAction.CONTINUE)
            self._record_evidence(tool_name, args, ret)
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
            next_prompt=self._unknown_tool_retry_prompt(tool_name),
            action=StepAction.CONTINUE,
        )
        self._trigger_hook("tool_after", {
            "tool_name": tool_name,
            "args": args,
            "outcome": ret,
            "result": ret.data,
        })
        return ret

    def _mark_executing(self, tool_name: str) -> None:
        """Promote OPEN to EXECUTING after an observable external operation."""

        if tool_name in {"complete_task", "ask_user", "bad_json"}:
            return
        if not hasattr(self, f"do_{tool_name}") and self.registry.get(tool_name) is None:
            return
        if self.task_contract.mode is not TaskMode.OPEN:
            return
        self.task_contract = TaskContract(
            task_id=self.task_contract.task_id,
            user_request=self.task_contract.user_request,
            mode=TaskMode.EXECUTING,
            plan_path=self.task_contract.plan_path,
        )

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
        """Return a corrective prompt after malformed native tool arguments."""
        msg = str(args.get("msg") or "Invalid tool arguments")
        yield self._tl(
            "[Warn] 工具参数 JSON 无效，要求重新生成。\n",
            "[Warn] Invalid tool argument JSON; requesting regeneration.\n",
        )
        return StepOutcome(
            {},
            next_prompt=self._bad_json_retry_prompt(msg),
            action=StepAction.CONTINUE,
        )

    def _native_tool_retry_prompt(self) -> str:
        """Prompt the model to emit an explicit provider-native control action."""
        return self._tl(
            "[System] 上一轮没有产生有效的控制动作，没有任何工具被执行。"
            "需要操作时调用 provider-native tool_call；不要只重复计划，也不要在正文中写 "
            "<tool_use>、<tool_call>、<function_call>、<file_content>。"
            "任务完成时调用 complete_task；需要用户输入时调用 ask_user。",
            "[System] The previous reply produced no valid control action, so no tool was executed. "
            "Call a provider-native tool_call when work is needed; do not repeat a plan or write "
            "<tool_use>, <tool_call>, <function_call>, or <file_content> in prose. "
            "Call complete_task when finished, or ask_user when user input is required.",
        )

    def _bad_json_retry_prompt(self, msg: str) -> str:
        """Prompt the model to regenerate an invalid native tool call."""
        return self._tl(
            "[System] 上一轮 provider-native tool_call 的 arguments 不是合法 JSON，工具未执行。"
            f"错误：{msg}\n"
            "请重新发出合法的 provider-native tool_call：function.arguments 必须是合法 JSON object string；"
            "不要在正文中解释或写工具协议；如果不再需要工具，请给出最终结论和证据。",
            "[System] The previous provider-native tool_call arguments were not valid JSON, "
            f"so the tool was not executed. Error: {msg}\n"
            "Regenerate a valid provider-native tool_call: function.arguments must be a valid "
            "JSON object string; do not explain or write tool protocol in text; if no tool is "
            "needed anymore, provide the final conclusion and evidence.",
        )

    def _unknown_tool_retry_prompt(self, tool_name: str) -> str:
        """Prompt the model to regenerate a native call with an available tool."""
        return self._tl(
            f"[System] 上一轮调用了不存在的工具 `{tool_name}`，工具未执行。"
            "请根据当前可用 tools schema 重新选择可用工具并发出 provider-native tool_call；"
            "不要猜工具名，不要在正文写工具调用格式。如果不需要工具，请直接给出最终结论和证据。",
            f"[System] The previous turn called unknown tool `{tool_name}`, so no tool was executed. "
            "Select an available tool from the current tools schema and emit a provider-native "
            "tool_call; do not guess tool names or write tool-call syntax in text. If no tool is "
            "needed, provide the final conclusion and evidence directly.",
        )

    def _plan_verification_retry_prompt(self) -> str:
        """Prompt plan-mode agents to verify before claiming completion."""
        return self._tl(
            "⛔ [验证拦截] 检测到你在 plan 模式下声称完成，但未执行 [VERIFY] 验证步骤。"
            "请先按 plan_sop 启动验证 subagent，获得 VERDICT 后才能声称完成。",
            "⛔ [Verify Intercept] You claimed completion in plan mode "
            "without running [VERIFY]. Please run verification first.",
        )

    def _large_code_block_retry_prompt(self) -> str:
        """Prompt after a large bare code block is emitted without tool calls."""
        return self._tl(
            "[System] 检测到你在上一轮回复中主要内容是较大代码块，"
            "且本轮未调用任何工具。若代码需要执行或写入，请调用实际工具；"
            "若任务已经完成，请调用 complete_task。",
            "[System] Your last reply was mainly a large code block without a tool call. "
            "Call a real tool if it must be executed or written; call complete_task if finished.",
        )

    # ---- completion control ----

    def do_complete_task(
        self, args: Dict[str, Any], response: Any
    ) -> Generator[str, None, StepOutcome]:
        """Validate and apply the explicit task-completion transition."""

        from zero_agent.core.completion import load_plan_verify_status

        answer = str(args.get("answer") or "").strip()
        evidence_refs = args.get("evidence_refs")
        if evidence_refs is None:
            evidence_refs = []
        if not isinstance(evidence_refs, list):
            return StepOutcome(
                {},
                next_prompt="[System] complete_task.evidence_refs must be an array of record numbers.",
                action=StepAction.CONTINUE,
            )

        contract = self.task_contract
        plan_remaining = self._check_plan_completion() if contract.mode is TaskMode.PLAN else None
        if contract.mode is TaskMode.PLAN and self.plan_verify_status != "partial_accepted":
            self.plan_verify_status = load_plan_verify_status(contract)
        certificate, continuation_prompt = evaluate_completion(
            contract,
            self.evidence_ledger,
            response,
            final_text=answer,
            evidence_refs=evidence_refs,
            plan_remaining=plan_remaining,
            plan_verify_status=self.plan_verify_status,
        )
        if certificate is None:
            self.completion_certificate = None
            return StepOutcome(
                {},
                next_prompt=continuation_prompt,
                action=StepAction.CONTINUE,
            )

        self.completion_certificate = certificate
        yield answer + "\n"
        return StepOutcome(
            {"answer": answer},
            action=StepAction.REQUEST_COMPLETION,
        )

    def do_no_tool(
        self, args: Dict[str, Any], response: Any
    ) -> Generator[str, None, StepOutcome]:
        """Handle safe plain text only while the observed state is OPEN."""

        decision = self.completion_gate.evaluate(response)
        if decision.action != CompletionGateAction.ALLOW:
            self._annotate_completion_gate(args, decision)
            yield self._tl(decision.message_zh, decision.message_en)
            if decision.action == CompletionGateAction.EXIT:
                reason = decision.reason or "completion_gate_limit"
                status = (
                    TerminalStatus.PROTOCOL_ERROR
                    if reason == "text_tool_protocol_limit"
                    else TerminalStatus.BUDGET_EXHAUSTED
                )
                return StepOutcome(
                    decision.data,
                    action=StepAction.FAIL,
                    reason=reason,
                    terminal_status=status,
                )
            if decision.reason in {
                "blank_response",
                "interruption:incomplete",
                "interruption:max_tokens",
            }:
                limit_reason = (
                    "blank_response_limit"
                    if decision.reason == "blank_response"
                    else "interruption_retry_limit"
                )
                return self._retry_or_exit(decision.prompt or "", limit_reason)
            return StepOutcome(
                decision.data,
                next_prompt=decision.prompt,
                action=StepAction.CONTINUE,
            )

        if self.task_contract.mode is not TaskMode.OPEN:
            self.completion_certificate = None
            return StepOutcome(
                {},
                next_prompt=(
                    "[System] This task has executed real tools or entered plan mode. "
                    "Finish with provider-native complete_task and cite successful evidence_refs."
                ),
                action=StepAction.CONTINUE,
            )

        certificate, continuation_prompt = evaluate_completion(
            self.task_contract,
            self.evidence_ledger,
            response,
            plan_remaining=None,
            plan_verify_status=self.plan_verify_status,
        )
        if certificate is None:
            self.completion_certificate = None
            return StepOutcome(
                {},
                next_prompt=continuation_prompt or self._native_tool_retry_prompt(),
                action=StepAction.CONTINUE,
            )

        self.completion_certificate = certificate
        for zh, en in decision.allow_messages:
            yield self._tl(zh, en)
        yield self._tl(
            "[Info] Final response to user.\n",
            "[Info] Final response to user.\n",
        )
        return StepOutcome(response, action=StepAction.REQUEST_COMPLETION)

    @staticmethod
    def _annotate_completion_gate(
        args: Dict[str, Any],
        decision: Any,
    ) -> None:
        metadata = dict(decision.metadata or {})
        args["_completion_gate"] = {
            "action": decision.action.value,
            "reason": decision.reason,
            **metadata,
        }

    def _retry_or_exit(
        self,
        prompt: str,
        limit_reason: str = "blank_response_limit",
    ) -> StepOutcome:
        """Retry transient empty/interrupted responses within a fixed budget."""

        self._empty_ct = getattr(self, "_empty_ct", 0) + 1
        if self._empty_ct >= 3:
            return StepOutcome(
                {},
                action=StepAction.FAIL,
                reason=limit_reason,
                terminal_status=TerminalStatus.BUDGET_EXHAUSTED,
            )
        return StepOutcome({}, next_prompt=prompt, action=StepAction.CONTINUE)

    # ---- 内部辅助方法 ----

    def _record_evidence(
        self,
        tool_name: str,
        args: Dict[str, Any],
        outcome: Any,
    ) -> None:
        """Append one compact evidence record for real tool dispatches."""

        if tool_name in {"no_tool", "bad_json", "unknown", "judge", "complete_task"}:
            return
        if tool_name.startswith("judge") or tool_name.endswith("_judge"):
            return
        if not isinstance(getattr(self, "evidence_ledger", None), EvidenceLedger):
            self.evidence_ledger = EvidenceLedger()
        data = outcome.data if isinstance(outcome, StepOutcome) else outcome
        kind = self._evidence_kind(tool_name)
        status = self._evidence_status(tool_name, data)
        clean_args = {
            k: v for k, v in args.items()
            if not str(k).startswith("_")
        }
        summary = f"{tool_name}({self._summarize_evidence_args(clean_args)})"
        self.evidence_ledger.records.append(EvidenceRecord(
            turn=self.current_turn,
            tool_name=tool_name,
            status=status,
            kind=kind,
            summary=summary,
        ))

    @staticmethod
    def _evidence_kind(tool_name: str) -> str:
        if tool_name == "file_read":
            return "read"
        if tool_name in {"file_write", "file_patch"}:
            return "write"
        if tool_name == "code_run":
            return "execute"
        if tool_name in {"web_scan", "web_execute_js"}:
            return "web"
        if tool_name == "ask_user":
            return "user"
        if "verify" in tool_name:
            return "verify"
        if "memory" in tool_name or tool_name == "update_working_checkpoint":
            return "memory"
        return "system"

    @staticmethod
    def _evidence_status(tool_name: str, data: Any) -> str:
        if tool_name == "file_read":
            return (
                "success"
                if isinstance(data, str) and not data.startswith("Error:")
                else "error"
            )
        if tool_name in {"file_write", "file_patch", "web_scan", "web_execute_js"}:
            return "success" if isinstance(data, dict) and data.get("status") == "success" else "error"
        if tool_name == "code_run":
            return (
                "success"
                if isinstance(data, dict)
                and data.get("status") == "success"
                and int(data.get("exit_code", 1) or 0) == 0
                else "error"
            )
        if tool_name == "ask_user":
            if isinstance(data, dict) and data.get("status") == "INTERRUPT":
                return "interrupt"
            return "success"
        if "memory" in tool_name or tool_name == "update_working_checkpoint":
            return "unknown"
        if isinstance(data, dict):
            status = str(data.get("status") or "").lower()
            if status in {"success", "error", "interrupt"}:
                return status
        return "unknown" if data is None else "success"

    @staticmethod
    def _summarize_evidence_args(args: Dict[str, Any]) -> str:
        text = json.dumps(args, ensure_ascii=False, default=str)
        return text if len(text) <= 120 else text[:117] + "..."

    def _default_next_prompt(self, args: Dict[str, Any]) -> str:
        """为注册工具生成默认的 next_prompt.

        首个工具调用（_index == 0）时注入完整锚点上下文:
            压缩早期历史 + 最近 30 条摘要 + 工作记忆.
        后续工具调用仅返回空白续写提示.

        Args:
            args: 工具参数，可能包含 _index 等元信息.

        Returns:
            默认的 prompt 字符串.
        """
        skip = args.get("_index", 0) > 0

        if skip:
            return "\n"
        return self._build_anchor_prompt()

    def _contract_checkpoint(self) -> str:
        """Return compact task contract and recent evidence for anchor prompts."""

        contract = getattr(self, "task_contract", None)
        ledger = getattr(self, "evidence_ledger", None)
        if contract is None or ledger is None:
            return ""
        records = list(getattr(ledger, "records", []) or [])[-8:]
        lines = [
            "<task_contract>",
            f"objective: {getattr(contract, 'user_request', '')}",
            f"state: {getattr(contract, 'mode', '')}",
        ]
        plan_path = getattr(contract, "plan_path", None)
        if plan_path:
            lines.append(f"plan_path: {plan_path}")
        lines.append(f"plan_verify_status: {getattr(self, 'plan_verify_status', 'missing')}")
        if records:
            lines.append("recent_evidence:")
            for record in records:
                lines.append(
                    "- "
                    f"turn={getattr(record, 'turn', '?')} "
                    f"tool={getattr(record, 'tool_name', '?')} "
                    f"status={getattr(record, 'status', '?')} "
                    f"kind={getattr(record, 'kind', '?')} "
                    f"summary={getattr(record, 'summary', '')}"
                )
        else:
            lines.append("recent_evidence: none")
        lines.append("</task_contract>")
        return "\n".join(lines)

    def _build_anchor_prompt(self) -> str:
        """构建锚点 prompt：压缩早期历史 + 最近摘要 + 工作记忆.

        将 history_info 按窗口
        分割为 <earlier_context>（压缩）和 <history>（最近 30 条）.

        Returns:
            格式化的锚点 prompt 字符串.
        """
        WINDOW = 30
        h = self.history_info
        earlier = ""
        if len(h) > WINDOW:
            earlier = (
                f"<earlier_context>\n{self._fold_history(h[:-WINDOW])}"
                "\n</earlier_context>\n"
            )
        h_str = "\n".join(h[-WINDOW:])
        checkpoint = self._contract_checkpoint()
        prompt = f"\n### [WORKING MEMORY]\n{checkpoint}\n{earlier}<history>\n{h_str}\n</history>"
        prompt += f"\nCurrent turn: {self.current_turn}\n"
        if self.working.get("key_info"):
            prompt += f"\n<key_info>{self.working.get('key_info')}</key_info>"
        if self.working.get("related_sop"):
            prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"
        return prompt

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

        return "\n".join(parts[-70:])

    def turn_end_callback(
        self,
        response: Any,
        tool_calls: list,
        tool_results: list,
        turn: int,
        next_prompt: str,
        terminal: Optional[TerminalEvent],
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
            terminal: Terminal event for this turn, if one was produced.

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
            clean_args = {
                k: v for k, v in tc.get("args", {}).items()
                if not k.startswith("_")
            }
            if tool_name == "no_tool":
                gate = tc.get("args", {}).get("_completion_gate", {})
                reason = gate.get("reason") if isinstance(gate, dict) else None
                if reason:
                    summary = self._tl(
                        f"Completion gate 纠正/停止: {reason}",
                        f"Completion gate corrected/stopped: {reason}",
                    )
                else:
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
        plan_active = self._in_plan_mode()
        if turn % 75 == 0 and not plan_active:
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
                    next_prompt += "\n" + memory_ctx

        # ─── 3.5 Plan Mode 提示 ───
        if plan_active:
            _plan = self.task_contract.plan_path or ""
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
                    self.request_code_stop()
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
