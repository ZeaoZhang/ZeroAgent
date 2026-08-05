"""Agent 轮次执行循环.

AgentLoop: generator-based 的 agent 执行循环，编排 LLM 调用 → 工具分发 → 结果聚合的完整流程。
由 ZeroAgent 编排器驱动，每次 yield 返回状态信息供 UI 消费。
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any, Dict, Generator, List, Optional

from zero_agent.core.hooks import HookSystem
from zero_agent.core.interfaces import LLMClient, ToolDispatcher
from zero_agent.core.types import StepAction, StepOutcome, TerminalEvent, TerminalStatus
from zero_agent.llm.base import extract_usage_metrics, usage_has_cache_metrics
from zero_agent.utils.text import smart_format

if TYPE_CHECKING:
    from zero_agent.core.agent import ZeroAgent


class AgentLoop:
    """Generator-based 的 agent 执行循环.

    编排 LLM 调用 → 工具分发 → 结果聚合的完整流程。
    不持有 session 以外的状态，handler 负责维护工作记忆和 done hooks.

    Attributes:
        client: LLM 会话（LiteLLMSession）.
        handler: 工具分发器（BaseHandler 子类）.
        tools_schema: LLM 工具 schema 列表（OpenAI 格式）.
        max_turns: 最大轮次限制.
        verbose: 是否输出详细日志（工具参数、代码块包裹等）.
    """

    def __init__(
        self,
        client: LLMClient,
        handler: ToolDispatcher,
        tools_schema: List[Dict[str, Any]],
        max_turns: int = 80,
        verbose: bool = True,
        hooks: Optional[HookSystem] = None,
        agent: Optional[ZeroAgent] = None,  # type: ignore[name-defined]  # TYPE_CHECKING
    ) -> None:
        """初始化 AgentLoop.

        Args:
            client: LLM 会话，需提供 chat(messages, tools) → Generator 接口.
            handler: 工具分发器，需提供 dispatch(tool_name, args, response) → Generator 接口.
            tools_schema: LLM 工具 schema 列表（OpenAI function-calling 格式）.
            max_turns: 最大轮次限制.
            verbose: True 时输出详细工具调用信息.
            hooks: 可选的 HookSystem 实例，用于事件钩子.
            agent: 可选的 ZeroAgent 引用，用于配置热重载.
        """
        self.client = client
        self.handler = handler
        self.tools_schema = tools_schema
        self.max_turns = max_turns
        self.verbose = verbose
        self.hooks = hooks
        self.handler.loop = self
        self._agent = agent

    def run(
        self,
        system_prompt: str,
        user_input: str,
        initial_user_content: Optional[str] = None,
    ) -> Generator[Any, None, TerminalEvent]:
        """Execute the agent loop and return one typed terminal event."""

        initial_content = (
            initial_user_content if initial_user_content is not None else user_input
        )
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": initial_content},
        ]
        self.client.system = system_prompt

        turn = 0
        terminal: Optional[TerminalEvent] = None
        response: Any = None
        self.handler.max_turns = self.max_turns
        try:
            self.handler.last_user_request = initial_content
        except Exception:
            pass
        reset_session = getattr(self.handler, "reset_session_state", None)
        if callable(reset_session):
            reset_session()
        self._record_user_history(initial_content)

        self._trigger_hook("agent_before", {
            "task": user_input,
            "user_input": user_input,
            "model": self._model_name(),
            "messages": messages,
            "tools": self.tools_schema,
            "max_turns": self.handler.max_turns,
        })

        try:
            while turn < self.handler.max_turns:
                turn += 1
                yield {"turn": turn}

                if self._agent is not None and turn % 5 == 0:
                    if self._agent.reload_config():
                        self.client = self._agent.client
                        self.handler.client = self.client

                if self.verbose:
                    yield f"\n\n**LLM Running (Turn {turn}) ...**\n\n"
                else:
                    yield f"\nTurn {turn} ...\n"

                if turn % 10 == 0:
                    self._clear_tool_cache()

                self._trigger_hook("turn_before", {
                    "turn": turn,
                    "messages": messages,
                    "tools": self.tools_schema,
                    "model": self._model_name(),
                })
                self._trigger_hook("llm_before", {
                    "turn": turn,
                    "messages": messages,
                    "tools": self.tools_schema,
                    "model": self._model_name(),
                })
                response_gen = self.client.chat(
                    messages=messages, tools=self.tools_schema,
                )

                if self.verbose:
                    response = yield from response_gen
                    yield "\n\n"
                else:
                    response = self._exhaust(response_gen)
                    cleaned = self._clean_content(response.content)
                    if cleaned:
                        yield cleaned + "\n"

                if not response.tool_calls:
                    tool_calls = [{"tool_name": "no_tool", "args": {}}]
                else:
                    tool_calls = []
                    for i, tc in enumerate(response.tool_calls):
                        raw_args = tc.function.arguments
                        raw_args_text = (
                            raw_args if isinstance(raw_args, str)
                            else json.dumps(raw_args, ensure_ascii=False, default=self._json_default)
                        )
                        tool_call_id = tc.id or f"call_{i}"
                        try:
                            args = json.loads(raw_args_text)
                        except json.JSONDecodeError as exc:
                            args = {
                                "msg": (
                                    "Failed to parse tool call JSON arguments: "
                                    f"{exc}. Raw: {raw_args_text[:200]}"
                                )
                            }
                            tool_calls.append({
                                "tool_name": "bad_json",
                                "args": args,
                                "id": tool_call_id,
                            })
                            continue
                        if not isinstance(args, dict):
                            args = {
                                "msg": (
                                    "function.arguments must decode to a JSON object, "
                                    f"got {self._json_type_name(args)}"
                                )
                            }
                            tool_calls.append({
                                "tool_name": "bad_json",
                                "args": args,
                                "id": tool_call_id,
                            })
                            continue
                        if args.get("_malformed") is True:
                            args = {
                                "msg": str(
                                    args.get("_error")
                                    or "function.arguments contained malformed tool arguments"
                                )
                            }
                            tool_calls.append({
                                "tool_name": "bad_json",
                                "args": args,
                                "id": tool_call_id,
                            })
                            continue
                        tool_calls.append({
                            "tool_name": tc.function.name,
                            "args": args,
                            "id": tool_call_id,
                        })

                self._trigger_hook("llm_after", {
                    "turn": turn,
                    "response": response,
                    "tool_calls": tool_calls,
                    "usage": self._usage_from_response(response),
                    "stop_reason": getattr(response, "stop_reason", ""),
                    "model": self._model_name(),
                })

                tool_results: List[Dict[str, Any]] = []
                next_prompts: set[str] = set()
                turn_terminal: Optional[TerminalEvent] = None

                for ii, tc in enumerate(tool_calls):
                    tool_name: str = tc["tool_name"]
                    args: Dict[str, Any] = tc["args"]
                    tid: str = tc.get("id", "")

                    if tool_name not in {"no_tool", "complete_task"}:
                        if self.verbose:
                            yield (
                                f"Tool: `{tool_name}`  "
                                f"args:\n```text\n{self._pretty_json(args)}\n```\n"
                            )
                        else:
                            yield f"{tool_name}({self._compact_args(tool_name, args)})\n\n"

                    self.handler.current_turn = turn
                    if tool_name == "no_tool":
                        self.handler.completion_certificate = None
                    gen = self.handler.dispatch(
                        tool_name, args, response,
                        index=ii, tool_num=len(tool_calls),
                    )
                    outcome = yield from self._consume_dispatch(
                        gen,
                        wrap_output=tool_name != "complete_task",
                    )

                    if not self._valid_step_outcome(outcome):
                        turn_terminal = TerminalEvent(
                            status=TerminalStatus.PROTOCOL_ERROR,
                            reason="invalid_step_outcome",
                            data=outcome,
                            turn=turn,
                        )
                        break

                    if outcome.action == StepAction.WAIT_FOR_USER:
                        turn_terminal = TerminalEvent(
                            status=TerminalStatus.WAITING,
                            reason="human_intervention",
                            text=str(getattr(response, "content", "") or ""),
                            data=outcome.data,
                            turn=turn,
                        )
                        break

                    if outcome.action == StepAction.FAIL:
                        turn_terminal = TerminalEvent(
                            status=outcome.terminal_status or TerminalStatus.FAILED,
                            reason=outcome.reason,
                            text=str(getattr(response, "content", "") or ""),
                            data=outcome.data,
                            turn=turn,
                        )
                        break

                    if outcome.action == StepAction.REQUEST_COMPLETION:
                        certificate = getattr(self.handler, "completion_certificate", None)
                        if certificate is None:
                            turn_terminal = TerminalEvent(
                                status=TerminalStatus.PROTOCOL_ERROR,
                                reason="invalid_step_outcome",
                                data=outcome.data,
                                turn=turn,
                            )
                        else:
                            turn_terminal = TerminalEvent(
                                status=TerminalStatus.COMPLETED,
                                reason="completion_certificate",
                                text=certificate.final_text,
                                data=outcome.data,
                                turn=turn,
                                certificate=certificate,
                            )
                        break

                    if self._is_unknown_tool_prompt(outcome.next_prompt or ""):
                        self._clear_tool_cache()

                    if outcome.data is not None and tool_name != "no_tool":
                        datastr = (
                            json.dumps(
                                outcome.data,
                                ensure_ascii=False,
                                default=self._json_default,
                            )
                            if isinstance(outcome.data, (dict, list))
                            else str(outcome.data)
                        )
                        tool_results.append({
                            "tool_use_id": tid,
                            "content": datastr,
                        })

                    next_prompts.add(outcome.next_prompt or "")

                if (
                    turn_terminal is not None
                    and turn_terminal.status == TerminalStatus.COMPLETED
                    and self.handler._done_hooks
                ):
                    self.handler.completion_certificate = None
                    next_prompts.add(self.handler._done_hooks.pop(0))
                    turn_terminal = None

                if turn_terminal is not None:
                    self.handler.turn_end_callback(
                        response, tool_calls, tool_results, turn,
                        "", turn_terminal,
                    )
                    self._trigger_hook("turn_after", {
                        "turn": turn,
                        "response": response,
                        "tool_calls": tool_calls,
                        "tool_results": tool_results,
                        "next_prompt": "",
                        "terminal": turn_terminal,
                        "model": self._model_name(),
                    })
                    terminal = turn_terminal
                    break

                if not next_prompts:
                    terminal = TerminalEvent(
                        status=TerminalStatus.PROTOCOL_ERROR,
                        reason="invalid_step_outcome",
                        turn=turn,
                    )
                    self.handler.turn_end_callback(
                        response, tool_calls, tool_results, turn, "", terminal,
                    )
                    self._trigger_hook("turn_after", {
                        "turn": turn,
                        "response": response,
                        "tool_calls": tool_calls,
                        "tool_results": tool_results,
                        "next_prompt": "",
                        "terminal": terminal,
                        "model": self._model_name(),
                    })
                    break

                next_prompt = "\n".join(next_prompts)
                next_prompt = self.handler.turn_end_callback(
                    response, tool_calls, tool_results, turn,
                    next_prompt, None,
                )
                messages = self._build_next_messages(next_prompt, tool_results)

                self._trigger_hook("turn_after", {
                    "turn": turn,
                    "response": response,
                    "tool_calls": tool_calls,
                    "tool_results": tool_results,
                    "next_prompt": next_prompt,
                    "terminal": None,
                    "model": self._model_name(),
                })
        except Exception as exc:
            terminal = TerminalEvent(
                status=TerminalStatus.FAILED,
                reason=exc.__class__.__name__,
                text=str(getattr(response, "content", "") or ""),
                data={"error": str(exc)},
                turn=turn,
            )

        if terminal is None:
            terminal = TerminalEvent(
                status=TerminalStatus.BUDGET_EXHAUSTED,
                reason="max_turns",
                text=str(getattr(response, "content", "") or ""),
                turn=turn,
            )

        self._trigger_hook("agent_after", {
            "turns": turn,
            "terminal": terminal,
            "model": self._model_name(),
        })
        return terminal

    # ---- dispatch 消费 ----

    @staticmethod
    def _valid_step_outcome(outcome: Any) -> bool:
        """Return whether a handler outcome satisfies the control invariants."""

        if not isinstance(outcome, StepOutcome):
            return False
        if outcome.action == StepAction.CONTINUE:
            return bool(outcome.next_prompt)
        if outcome.action == StepAction.REQUEST_COMPLETION:
            return outcome.next_prompt is None and outcome.terminal_status is None
        if outcome.action == StepAction.WAIT_FOR_USER:
            return outcome.next_prompt is None and outcome.terminal_status is None
        if outcome.action == StepAction.FAIL:
            return (
                outcome.next_prompt is None
                and bool(outcome.reason)
                and outcome.terminal_status in {
                    TerminalStatus.FAILED,
                    TerminalStatus.BUDGET_EXHAUSTED,
                    TerminalStatus.PROTOCOL_ERROR,
                }
            )
        return False

    def _record_user_history(self, content: Any) -> None:
        """Record a compact [USER] entry in handler.history_info."""
        if not isinstance(getattr(self.handler, "history_info", None), list):
            return
        text = self._message_text(content).replace("\n", " ").strip()
        if text:
            self.handler.history_info.append(
                f"[USER]: {smart_format(text, max_str_len=200)}"
            )

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text is not None:
                        parts.append(str(text))
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def _is_unknown_tool_prompt(prompt: str) -> bool:
        stripped = (prompt or "").lstrip().lower()
        return (
            stripped.startswith("未知工具")
            or stripped.startswith("unknown tool")
            or "不存在的工具" in stripped
            or "unknown tool" in stripped
        )

    def _clear_tool_cache(self) -> None:
        """Force full tool protocol resend after tool routing failures."""
        reset = getattr(self.client, "reset_tool_protocol_cache", None)
        if callable(reset):
            reset()
            return
        if hasattr(self.client, "last_tools"):
            self.client.last_tools = ""
        if hasattr(self.client, "_last_tools_json"):
            self.client._last_tools_json = ""

    @staticmethod
    def _build_next_messages(
        next_prompt: str,
        tool_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build the next-turn message payload.

        LiteLLMSession normalizes this custom field into provider-native
        ``role=tool`` messages before sending the API request.
        """
        return [
            {
                "role": "user",
                "content": next_prompt,
                "tool_results": tool_results,
            }
        ]

    def _consume_dispatch(
        self,
        gen: Generator,
        *,
        wrap_output: bool = True,
    ) -> Generator[Any, None, StepOutcome]:
        """Consume dispatch output, optionally wrapping verbose tool output."""

        try:
            value = next(gen)
        except StopIteration as stop:
            return stop.value

        if not self.verbose:
            return self._exhaust(gen)
        if not wrap_output:
            yield value
            return (yield from gen)

        yield "```\n"
        yield value
        outcome = yield from gen
        yield "```\n"
        return outcome

    # ---- hook 辅助 ----

    def _trigger_hook(self, event: str, context: dict) -> None:
        """触发 hook 并隔离 hook 失败."""
        if self.hooks:
            self.hooks.trigger(event, context)

    def _model_name(self) -> str:
        """获取当前 LLM model 名称."""
        config = getattr(self.client, "config", None)
        if config is not None and getattr(config, "model", None):
            return config.model
        return getattr(self.client, "name", "unknown")

    @staticmethod
    def _usage_from_response(response: Any) -> dict:
        """Return canonical token usage for llm_after hooks."""
        raw = getattr(response, "raw", None)
        usage = getattr(raw, "usage", None) if raw is not None else None
        if usage is None:
            usage = getattr(response, "usage", None)
        if usage is None:
            return {}

        metrics = extract_usage_metrics(usage)
        canonical = {
            "input_tokens": metrics["input_tokens"],
            "output_tokens": metrics["output_tokens"],
            "cache_read_input_tokens": metrics["cache_read_tokens"],
            "cache_creation_input_tokens": metrics["cache_creation_tokens"],
            "cache_miss_input_tokens": metrics["cache_miss_tokens"],
            "cache_metrics_available": usage_has_cache_metrics(usage),
        }
        if isinstance(usage, dict):
            result = dict(usage)
            result.update(canonical)
            return result
        return canonical

    # ---- 静态工具方法 ----

    @staticmethod
    def _exhaust(gen: Generator) -> Any:
        """消费 generator 的所有 yield 并返回最终值.

        Args:
            gen: 任意 generator.

        Returns:
            generator 的 return 值.
        """
        try:
            while True:
                next(gen)
        except StopIteration as e:
            return e.value

    @staticmethod
    def _json_default(o: Any) -> Any:
        """JSON 序列化的 fallback 处理.

        Args:
            o: 无法直接序列化的对象.

        Returns:
            可序列化的表示.
        """
        if isinstance(o, set):
            return list(o)
        return str(o)

    @staticmethod
    def _json_type_name(value: Any) -> str:
        """Return the JSON type name used in native tool argument errors."""

        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, list):
            return "list"
        if isinstance(value, str):
            return "string"
        if isinstance(value, (int, float)):
            return "number"
        return type(value).__name__

    @staticmethod
    def _pretty_json(data: Any) -> str:
        """美化 JSON 输出，对 script 字段做换行处理.

        Args:
            data: 待序列化的数据.

        Returns:
            格式化后的 JSON 字符串.
        """
        if isinstance(data, dict) and "script" in data:
            data = data.copy()
            data["script"] = data["script"].replace("; ", ";\n  ")
        return json.dumps(data, indent=2, ensure_ascii=False).replace(
            "\\n", "\n"
        )

    @staticmethod
    def _clean_content(text: str) -> str:
        """清理 LLM 文本内容用于非 verbose 显示.

        缩略长代码块、去除 file_content/tool_use 标签、压缩空行.

        Args:
            text: 原始 LLM 文本内容.

        Returns:
            清理后的展示文本.
        """
        if not text:
            return ""

        def _shrink_code(m: re.Match) -> str:
            lines = m.group(0).split("\n")
            lang = lines[0].replace("```", "").strip()
            body = [l for l in lines[1:-1] if l.strip()]
            if len(body) <= 6:
                return m.group(0)
            preview = "\n".join(body[:5])
            return f"```{lang}\n{preview}\n  ... ({len(body)} lines)\n```"

        text = re.sub(r"```[\s\S]*?```", _shrink_code, text)
        for pattern in [
            r"<file_content>[\s\S]*?</file_content>",
            r"<tool_(?:use|call)>[\s\S]*?</tool_(?:use|call)>",
            r"(\r?\n){3,}",
        ]:
            text = re.sub(pattern, "\n\n" if "\\n" in pattern else "", text)
        return text.strip()

    @staticmethod
    def _compact_args(name: str, args: Dict[str, Any]) -> str:
        """精简工具参数用于非 verbose 显示.

        Args:
            name: 工具名称.
            args: 工具参数字典.

        Returns:
            精简后的参数字符串.
        """
        a = {k: v for k, v in args.items() if k != "_index"}
        for k in ("path",):
            if k in a:
                a[k] = os.path.basename(a[k])
        if name == "update_working_checkpoint":
            s = a.get("key_info", "")
            return (s[:60] + "...") if len(s) > 60 else s
        if name == "ask_user":
            q = str(a.get("question", ""))
            cs = a.get("candidates") or []
            if cs:
                q += "\ncandidates:\n" + "\n".join(f"- {c}" for c in cs)
            return q
        s = json.dumps(a, ensure_ascii=False)
        return (s[:120] + "...") if len(s) > 120 else s
