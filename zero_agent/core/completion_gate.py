"""Completion gates for no-tool LLM turns.

The agent loop calls ``do_no_tool`` when a provider response has no native tool
calls.  This module owns the stop/correction decision so ``do_no_tool`` remains
small and does not accumulate unrelated protocol heuristics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from zero_agent.core.interruption import classify_interruption


class CompletionGateAction(str, Enum):
    """Allowed outcomes for a no-tool completion decision."""

    ALLOW = "allow"
    RETRY = "retry"
    EXIT = "exit"


@dataclass(frozen=True)
class CompletionGateDecision:
    """Result returned by ``CompletionGate.evaluate``."""

    action: CompletionGateAction
    reason: str = ""
    prompt: Optional[str] = None
    message_zh: str = ""
    message_en: str = ""
    allow_messages: tuple[tuple[str, str], ...] = ()
    data: Any = None
    metadata: dict[str, Any] | None = None

    @classmethod
    def allow(
        cls,
        *,
        messages: tuple[tuple[str, str], ...] = (),
    ) -> "CompletionGateDecision":
        return cls(CompletionGateAction.ALLOW, allow_messages=messages)

    @classmethod
    def retry(
        cls,
        *,
        reason: str,
        prompt: str,
        message_zh: str,
        message_en: str,
        data: Any = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "CompletionGateDecision":
        return cls(
            CompletionGateAction.RETRY,
            reason=reason,
            prompt=prompt,
            message_zh=message_zh,
            message_en=message_en,
            data={} if data is None else data,
            metadata=metadata,
        )

    @classmethod
    def exit(
        cls,
        *,
        reason: str,
        message_zh: str,
        message_en: str,
        data: Any = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "CompletionGateDecision":
        return cls(
            CompletionGateAction.EXIT,
            reason=reason,
            message_zh=message_zh,
            message_en=message_en,
            data={} if data is None else data,
            metadata=metadata,
        )


class CompletionGate:
    """Decide whether a no-tool LLM response may stop the loop."""

    def __init__(
        self,
        *,
        retry_prompt_factory: Callable[[], str],
        large_code_prompt_factory: Callable[[], str],
        plan_verification_prompt_factory: Callable[[], str],
        in_plan_mode: Callable[[], bool],
        check_plan_completion: Callable[[], Optional[int]],
        exit_plan_mode: Callable[[], None],
        protocol_retry_limit: int = 2,
        action_retry_limit: int = 1,
    ) -> None:
        self._retry_prompt_factory = retry_prompt_factory
        self._large_code_prompt_factory = large_code_prompt_factory
        self._plan_verification_prompt_factory = plan_verification_prompt_factory
        self._in_plan_mode = in_plan_mode
        self._check_plan_completion = check_plan_completion
        self._exit_plan_mode = exit_plan_mode
        self._protocol_retry_limit = protocol_retry_limit
        self._action_retry_limit = action_retry_limit
        self._retry_counts: dict[str, int] = {}

    def reset(self) -> None:
        """Clear per-task retry budgets."""

        self._retry_counts.clear()

    def evaluate(self, response: Any) -> CompletionGateDecision:
        """Return the stop/correction decision for a no-tool response."""

        content = getattr(response, "content", "") or ""
        thinking = getattr(response, "thinking", "") or ""

        if not response or (not content.strip() and not thinking.strip()):
            return CompletionGateDecision.retry(
                reason="blank_response",
                prompt="[System] Blank response, regenerate and tooluse",
                message_zh="[Warn] LLM 返回空响应，重试...\n",
                message_en="[Warn] LLM returned empty response, retrying...\n",
                metadata={"budgeted": False},
            )

        interruption = classify_interruption(response)
        if interruption:
            return CompletionGateDecision.retry(
                reason=f"interruption:{interruption.kind}",
                prompt=interruption.retry_prompt,
                message_zh="[Warn] LLM 响应中断，重试...\n",
                message_en="[Warn] LLM response was interrupted; retrying...\n",
                metadata={
                    "interruption": interruption.kind,
                    "budgeted": False,
                },
            )

        combined = "\n".join(part for part in (content, thinking) if part)
        if self._has_text_tool_protocol(combined):
            return self._budgeted_native_retry(
                reason="text_tool_protocol",
                limit=self._protocol_retry_limit,
                message_zh=(
                    "[Warn] 检测到旧文本工具协议，但没有原生 tool_calls，要求重试.\n"
                ),
                message_en=(
                    "[Warn] Obsolete text tool protocol detected without native "
                    "tool_calls; retrying.\n"
                ),
                exhausted_zh=(
                    "[Warn] 旧文本工具协议纠正已达到上限，停止本轮以避免无效循环.\n"
                ),
                exhausted_en=(
                    "[Warn] Text tool protocol correction limit reached; stopping "
                    "this run to avoid an ineffective loop.\n"
                ),
            )

        if self._looks_like_unexecuted_action(content):
            return self._budgeted_native_retry(
                reason="promissory_action",
                limit=self._action_retry_limit,
                message_zh=(
                    "[Warn] 检测到未执行的动作意图，但没有原生 tool_calls，要求重试.\n"
                ),
                message_en=(
                    "[Warn] Unexecuted action intent detected without native "
                    "tool_calls; retrying.\n"
                ),
                exhausted_zh=(
                    "[Warn] 未执行动作意图纠正已达到上限，停止本轮以避免无效循环.\n"
                ),
                exhausted_en=(
                    "[Warn] Promissory action correction limit reached; stopping "
                    "this run to avoid an ineffective loop.\n"
                ),
            )

        if (
            self._in_plan_mode()
            and self._has_unverified_plan_completion_claim(content)
        ):
            return CompletionGateDecision.retry(
                reason="plan_completion_unverified",
                prompt=self._plan_verification_prompt_factory(),
                message_zh="[Warn] Plan 模式完成声明拦截.\n",
                message_en="[Warn] Plan mode completion claim intercepted.\n",
                metadata={"budgeted": False},
            )

        if self._has_large_code_block_without_tool(content):
            return CompletionGateDecision.retry(
                reason="large_code_block_without_tool",
                prompt=self._large_code_prompt_factory(),
                message_zh="[Info] 检测到大代码块未调用工具，提示 LLM 调用工具.\n",
                message_en=(
                    "[Info] Large code block without tool call detected, "
                    "prompting LLM.\n"
                ),
                metadata={"budgeted": False},
            )

        messages: tuple[tuple[str, str], ...] = ()
        if self._in_plan_mode():
            remaining = self._check_plan_completion()
            if remaining == 0:
                self._exit_plan_mode()
                messages = (
                    (
                        "[Info] Plan 完成：plan.md 中 0 个 [ ] 残留，退出 plan 模式.\n",
                        "[Info] Plan complete: 0 unchecked [ ] in plan.md, "
                        "exiting plan mode.\n",
                    ),
                )

        self.reset()
        return CompletionGateDecision.allow(messages=messages)

    def _budgeted_native_retry(
        self,
        *,
        reason: str,
        limit: int,
        message_zh: str,
        message_en: str,
        exhausted_zh: str,
        exhausted_en: str,
    ) -> CompletionGateDecision:
        count = self._retry_counts.get(reason, 0) + 1
        self._retry_counts[reason] = count
        metadata = {
            "attempt": count,
            "limit": limit,
            "budgeted": True,
        }
        if count > limit:
            return CompletionGateDecision.exit(
                reason=f"{reason}_limit",
                message_zh=exhausted_zh,
                message_en=exhausted_en,
                metadata=metadata,
            )
        return CompletionGateDecision.retry(
            reason=reason,
            prompt=self._retry_prompt_factory(),
            message_zh=message_zh,
            message_en=message_en,
            metadata=metadata,
        )

    @staticmethod
    def _has_text_tool_protocol(text: str) -> bool:
        """Detect obsolete executable text-tool protocol markers."""

        return bool(re.search(
            r"<\s*(?:tool_use|tool_call|function_call|file_content)\b",
            text,
            flags=re.IGNORECASE,
        ))

    @staticmethod
    def _looks_like_unexecuted_action(text: str) -> bool:
        """Detect explicit future action intent, not ordinary result prose."""

        clean = re.sub(
            r"<\s*(?:thinking|summary)[^>]*>[\s\S]*?<\s*/\s*(?:thinking|summary)\s*>",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        if not clean:
            return False
        clean = clean.lstrip(" \t\r\n-*>#")

        zh_patterns = [
            r"^(?:好的[，,。\s]*)?"
            r"(?:让我|我来|我先|我会|我将|接下来我(?:会|来)|现在我(?:会|来)|继续)"
            r".{0,24}(?:检查|查看|读取|运行|执行|搜索|打开|写入|修改|分析)",
            r"^(?:好的[，,。\s]*)?"
            r"(?:需要|准备|将要).{0,18}(?:检查|查看|读取|运行|执行|搜索|打开|写入|修改|分析)",
        ]
        en_patterns = [
            r"^(?:sure|ok(?:ay)?|right|got it)[,.\s:;-]*"
            r"(?:let me|i(?:'ll| will| am going to|'m going to| need to)|"
            r"next i(?:'ll| will)|i(?:'ll| will) now)\s+"
            r"(?:check|inspect|read|run|execute|search|open|write|modify|analy[sz]e)\b",
            r"^(?:let me|i(?:'ll| will| am going to|'m going to| need to)|"
            r"next i(?:'ll| will)|i(?:'ll| will) now)\s+"
            r"(?:check|inspect|read|run|execute|search|open|write|modify|analy[sz]e)\b",
        ]
        return any(
            re.search(pattern, clean, flags=re.IGNORECASE)
            for pattern in [*zh_patterns, *en_patterns]
        )

    @staticmethod
    def _has_unverified_plan_completion_claim(content: str) -> bool:
        plan_complete_kw = [
            "任务完成", "全部完成", "已完成所有",
            "🏁", "All tasks complete", "All done", "finished all",
        ]
        if not any(kw in content for kw in plan_complete_kw):
            return False
        return (
            "VERDICT" not in content
            and "[VERIFY]" not in content
            and "验证subagent" not in content
        )

    @staticmethod
    def _has_large_code_block_without_tool(content: str) -> bool:
        code_block_pattern = r"```[a-zA-Z0-9_]*\n[\s\S]{50,}?```"
        blocks = re.findall(code_block_pattern, content)
        if len(blocks) != 1:
            return False

        match = re.search(code_block_pattern, content)
        if not match:
            return False
        after_block = content[match.end():]
        if after_block.strip():
            return False

        residual = content.replace(match.group(0), "")
        residual = re.sub(
            r"<thinking>[\s\S]*?</thinking>",
            "",
            residual,
            flags=re.IGNORECASE,
        )
        residual = re.sub(
            r"<summary>[\s\S]*?</summary>",
            "",
            residual,
            flags=re.IGNORECASE,
        )
        clean_residual = re.sub(r"\s+", "", residual)
        return len(clean_residual) <= 30
