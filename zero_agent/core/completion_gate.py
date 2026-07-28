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
        protocol_retry_limit: int = 2,
        action_retry_limit: int = 2,
    ) -> None:
        self._retry_prompt_factory = retry_prompt_factory
        self._large_code_prompt_factory = large_code_prompt_factory
        self._plan_verification_prompt_factory = plan_verification_prompt_factory
        self._in_plan_mode = in_plan_mode
        self._check_plan_completion = check_plan_completion
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
        protocol = self._response_protocol(response)

        if not response or (not content.strip() and not thinking.strip()):
            return CompletionGateDecision.retry(
                reason="blank_response",
                prompt="[System] Blank response, regenerate and tooluse",
                message_zh="[Warn] LLM 返回空响应，重试...\n",
                message_en="[Warn] LLM returned empty response, retrying...\n",
                metadata={"budgeted": False},
            )

        if not content.strip() and thinking.strip():
            return CompletionGateDecision.retry(
                reason="thinking_only_no_deliverable",
                prompt=self._protocol_retry_prompt(protocol),
                message_zh="[Warn] LLM 只返回 thinking，未给出可见交付内容或工具调用，重试...\n",
                message_en=(
                    "[Warn] LLM returned only thinking with no visible deliverable "
                    "or tool call; retrying...\n"
                ),
                metadata={"budgeted": False, "protocol": protocol},
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

        unsafe_stop_reason = self._unsafe_stop_reason(response)
        if unsafe_stop_reason:
            return CompletionGateDecision.retry(
                reason="unsafe_stop_reason",
                prompt=self._protocol_retry_prompt(protocol),
                message_zh=(
                    f"[Warn] LLM stop_reason={unsafe_stop_reason}，不能视为完成，重试...\n"
                ),
                message_en=(
                    f"[Warn] LLM stop_reason={unsafe_stop_reason} cannot be treated "
                    "as completion; retrying...\n"
                ),
                metadata={
                    "budgeted": False,
                    "protocol": protocol,
                    "stop_reason": unsafe_stop_reason,
                },
            )

        combined = "\n".join(part for part in (content, thinking) if part)
        executable_text_call = self._extract_executable_text_tool_call(combined)
        if executable_text_call:
            return self._budgeted_retry(
                reason="text_tool_protocol",
                limit=self._protocol_retry_limit,
                protocol=protocol,
                message_zh=(
                    "[Warn] 检测到正文中的可执行工具调用，但没有 tool_calls，要求重试.\n"
                ),
                message_en=(
                    "[Warn] Executable tool-call text was emitted without tool_calls; "
                    "retrying.\n"
                ),
                exhausted_zh=(
                    "[Warn] 文本工具协议纠正已达到上限，停止本轮以避免无效循环.\n"
                ),
                exhausted_en=(
                    "[Warn] Text tool protocol correction limit reached; stopping "
                    "this run to avoid an ineffective loop.\n"
                ),
            )

        if self._looks_like_unexecuted_action(content):
            return self._budgeted_retry(
                reason="promissory_action",
                limit=self._action_retry_limit,
                protocol=protocol,
                message_zh=(
                    "[Warn] 检测到未执行的动作意图，但没有工具调用，要求重试.\n"
                ),
                message_en=(
                    "[Warn] Unexecuted action intent detected without tool calls; "
                    "retrying.\n"
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

        self.reset()
        return CompletionGateDecision.allow(messages=messages)



    def _budgeted_retry(
        self,
        *,
        reason: str,
        limit: int,
        protocol: str,
        message_zh: str,
        message_en: str,
        exhausted_zh: str,
        exhausted_en: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> CompletionGateDecision:
        count = self._retry_counts.get(reason, 0) + 1
        self._retry_counts[reason] = count
        retry_metadata = {
            "attempt": count,
            "limit": limit,
            "budgeted": True,
            "protocol": protocol,
            **(metadata or {}),
        }
        if count > limit:
            return CompletionGateDecision.exit(
                reason=f"{reason}_limit",
                message_zh=exhausted_zh,
                message_en=exhausted_en,
                metadata=retry_metadata,
            )
        return CompletionGateDecision.retry(
            reason=reason,
            prompt=self._protocol_retry_prompt(protocol),
            message_zh=message_zh,
            message_en=message_en,
            metadata=retry_metadata,
        )

    def _budgeted_native_retry(
        self,
        **kwargs: Any,
    ) -> CompletionGateDecision:
        """Backward-compatible wrapper for older callers/tests."""

        kwargs.setdefault("protocol", "native")
        return self._budgeted_retry(**kwargs)

    def _protocol_retry_prompt(self, protocol: str) -> str:
        if protocol == "text":
            return self._text_tool_retry_prompt()
        return self._retry_prompt_factory()

    @staticmethod
    def _response_protocol(response: Any) -> str:
        return "text" if getattr(response, "tool_protocol", "native") == "text" else "native"

    @staticmethod
    def _unsafe_stop_reason(response: Any) -> str:
        raw = getattr(response, "stop_reason", "")
        reason = str(raw or "").strip()
        if not reason:
            return ""
        lowered = reason.lower()
        if lowered.startswith("unknown:") or lowered in {
            "error",
            "content_filter",
            "cancelled",
            "interrupted",
            "stream_interrupted",
        }:
            return reason
        return ""

    @staticmethod
    def _text_tool_retry_prompt() -> str:
        return (
            '[System] The previous reply did not produce an executable text tool call. '
            'If a tool is needed, emit exactly one legal text tool call in this form: '
            '<tool_use>{"name":"file_read","arguments":{"path":"example.txt"}}</tool_use>. '
            'The JSON inside <tool_use> must be an object with "name" and object '
            '"arguments" fields. Do not use arrays, strings, null, scalars, partial JSON, '
            'or prose as tool arguments. If the task is complete, provide the final '
            'answer with evidence directly.'
        )

    @staticmethod
    def _extract_executable_text_tool_call(text: str) -> bool:
        """Detect a standalone text-tool command, not quoted protocol documentation."""

        clean = CompletionGate._strip_non_executable_regions(text)
        return bool(re.search(
            r"(?:^|\n)\s*<\s*(?:tool_use|tool_call|function_call|file_content)\b[^>]*>"
            r"(?:\s*\{[\s\S]*\}|[\s\S]*?)\s*<\s*/\s*(?:tool_use|tool_call|function_call|file_content)\s*>\s*$",
            clean,
            flags=re.IGNORECASE,
        ))

    @staticmethod
    def _strip_non_executable_regions(text: str) -> str:
        clean = re.sub(r"```[\s\S]*?```", "", text or "")
        clean = re.sub(r"`[^`\n]*`", "", clean)
        clean = re.sub(r"^\s*>.*$", "", clean, flags=re.MULTILINE)
        clean = re.sub(
            r"<\s*(?:thinking|summary)[^>]*>[\s\S]*?<\s*/\s*(?:thinking|summary)\s*>",
            "",
            clean,
            flags=re.IGNORECASE,
        )
        return clean.strip()

    @staticmethod
    def _looks_like_unexecuted_action(text: str) -> bool:
        """Detect explicit future action intent, not ordinary result prose.

        Only triggers when the reply is *almost entirely* the action-intent
        statement — not when the model weaves an intent phrase into a longer
        narrative summary or final answer.
        """

        clean = CompletionGate._strip_non_executable_regions(text)
        if not clean:
            return False

        # Strip leading punctuation / markdown so we can anchor at ^.
        stripped = clean.lstrip(" \t\r\n-*>#")

        action_verbs = "检查|查看|读取|运行|执行|搜索|打开|写入|修改|分析|看|查|读|搜|找|验证|测试"
        action_targets = (
            r"(?:文件|核心文件|代码|实现|逻辑|配置|日志|测试|结果|状态|页面|接口|API|"
            r"[\w.-]+\.(?:py|ts|tsx|js|jsx|json|ya?ml|toml|md|txt|log)|/|：|:)"
        )
        zh_patterns = [
            r"^(?:好的[，,。\s]*)?"
            r"(?:让我|我来|我先|我会|我将|接下来我(?:会|来)|现在我(?:会|来)|继续|再|先|接着)"
            rf".{{0,24}}(?:{action_verbs})",
            r"^(?:好的[，,。\s]*)?"
            rf"(?:需要|准备|将要).{{0,18}}(?:{action_verbs})",
            rf"^(?:好的[，,。\s]*)?(?:继续|再|先|现在|接着)?(?:{action_verbs}).{{0,80}}{action_targets}",
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
        all_patterns = [*zh_patterns, *en_patterns]

        for pattern in all_patterns:
            m = re.search(pattern, stripped, flags=re.IGNORECASE)
            if not m:
                continue
            # The intent pattern matched.  Now verify this is a *bare* intent
            # statement — not a throwaway phrase inside a full narrative reply.
            after_match = stripped[m.end():].strip(" \t\r\n")
            # If there is substantial natural-language content *after* the
            # intent phrase, the model is narrating / summarizing, not just
            # promising future work.  Allow up to one short trailing sentence.
            if len(after_match) > 50:
                return False
            return True

        return False

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
