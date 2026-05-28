"""Agent 轮次执行循环.

AgentLoop: generator-based 的 agent 执行循环，编排 LLM 调用 → 工具分发 → 结果聚合的完整流程。
由 ZeroAgent 编排器驱动，每次 yield 返回状态信息供 UI 消费。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Generator, List, Optional

from zero_agent.core.types import StepOutcome


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
        client: Any,
        handler: Any,
        tools_schema: List[Dict[str, Any]],
        max_turns: int = 80,
        verbose: bool = True,
        hooks: Any = None,
    ) -> None:
        """初始化 AgentLoop.

        Args:
            client: LLM 会话，需提供 chat(messages, tools) → Generator 接口.
            handler: 工具分发器，需提供 dispatch(tool_name, args, response) → Generator 接口.
            tools_schema: LLM 工具 schema 列表（OpenAI function-calling 格式）.
            max_turns: 最大轮次限制.
            verbose: True 时输出详细工具调用信息.
            hooks: 可选的 HookSystem 实例，用于事件钩子.
        """
        self.client = client
        self.handler = handler
        self.tools_schema = tools_schema
        self.max_turns = max_turns
        self.verbose = verbose
        self.hooks = hooks
        self.handler.loop = self

    def run(
        self,
        system_prompt: str,
        user_input: str,
        initial_user_content: Optional[str] = None,
    ) -> Generator[Any, None, dict]:
        """执行 agent 循环.

        Generator 协议:
            - yield str → 状态信息，供 UI 实时展示.
            - yield dict → 结构化信息（如 {"turn": 3}）.
            - return dict → exit_reason，如 {"result": "CURRENT_TASK_DONE", "data": ...}.

        Args:
            system_prompt: 系统提示词.
            user_input: 用户输入（任务描述）.
            initial_user_content: 可选的初始用户消息内容，不为 None 时覆盖 user_input.

        Yields:
            状态信息字符串或结构化 dict.

        Returns:
            exit_reason 字典，可能的值:
                {"result": "CURRENT_TASK_DONE", "data": ...}  — 任务正常完成.
                {"result": "EXITED", "data": ...}             — 硬退出（如 ask_user）.
                {"result": "MAX_TURNS_EXCEEDED"}              — 超出轮次上限.
        """
        initial_content = (
            initial_user_content if initial_user_content is not None else user_input
        )
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": initial_content},
        ]
        # System prompt belongs to the session, not the message history. Keeping
        # it out of messages prevents duplicate system prompts in LiteLLMSession.
        self.client.system = system_prompt

        turn = 0
        self.handler.max_turns = self.max_turns

        self._trigger_hook("agent_before", {
            "task": user_input,
            "user_input": user_input,
            "model": self._model_name(),
            "messages": messages,
            "tools": self.tools_schema,
            "max_turns": self.handler.max_turns,
        })

        while turn < self.handler.max_turns:
            turn += 1
            yield {"turn": turn}

            if self.verbose:
                yield f"\n\n**LLM Running (Turn {turn}) ...**\n\n"
            else:
                yield f"\nTurn {turn} ...\n"

            # 每 10 轮重置工具描述缓存
            if turn % 10 == 0:
                self.client.last_tools = ""

            self._trigger_hook("turn_before", {
                "turn": turn,
                "messages": messages,
                "tools": self.tools_schema,
                "model": self._model_name(),
            })

            # ——— LLM 调用 ———
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

            # ——— 解析工具调用 ———
            if not response.tool_calls:
                tool_calls = [{"tool_name": "no_tool", "args": {}}]
            else:
                tool_calls = [
                    {
                        "tool_name": tc.function.name,
                        "args": json.loads(tc.function.arguments),
                        "id": tc.id or f"call_{i}",
                    }
                    for i, tc in enumerate(response.tool_calls)
                ]

            self._trigger_hook("llm_after", {
                "turn": turn,
                "response": response,
                "tool_calls": tool_calls,
                "usage": self._usage_from_response(response),
                "stop_reason": getattr(response, "stop_reason", ""),
                "model": self._model_name(),
            })

            # ——— 分发工具 ———
            tool_results: List[Dict[str, Any]] = []
            next_prompts: set[str] = set()
            exit_reason: dict = {}

            for ii, tc in enumerate(tool_calls):
                tool_name: str = tc["tool_name"]
                args: Dict[str, Any] = tc["args"]
                tid: str = tc.get("id", "")

                if tool_name != "no_tool":
                    if self.verbose:
                        yield (
                            f"Tool: `{tool_name}`  "
                            f"args:\n```text\n{self._pretty_json(args)}\n```\n"
                        )
                    else:
                        yield f"{tool_name}({self._compact_args(tool_name, args)})\n\n"

                self.handler.current_turn = turn
                gen = self.handler.dispatch(
                    tool_name, args, response,
                    index=ii, tool_num=len(tool_calls),
                )

                outcome = yield from self._consume_dispatch(gen)

                if outcome.should_exit:
                    exit_reason = {"result": "EXITED", "data": outcome.data}
                    break

                if outcome.next_prompt is None:
                    exit_reason = {
                        "result": "CURRENT_TASK_DONE",
                        "data": outcome.data,
                    }
                    break

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

                next_prompts.add(outcome.next_prompt)

            # ——— 退出判断 ———
            if not next_prompts or exit_reason:
                if (
                    self.handler._done_hooks
                    and exit_reason.get("result") != "EXITED"
                ):
                    next_prompts.add(self.handler._done_hooks.pop(0))
                else:
                    if exit_reason:
                        self.handler.turn_end_callback(
                            response, tool_calls, tool_results, turn,
                            "", exit_reason,
                        )
                    self._trigger_hook("turn_after", {
                        "turn": turn,
                        "response": response,
                        "tool_calls": tool_calls,
                        "tool_results": tool_results,
                        "next_prompt": "",
                        "exit_reason": exit_reason,
                        "model": self._model_name(),
                    })
                    break

            # ——— 构建下一轮 prompt ———
            next_prompt = "\n".join(next_prompts)
            next_prompt = self.handler.turn_end_callback(
                response, tool_calls, tool_results, turn,
                next_prompt, exit_reason,
            )

            # 下一轮只发工具结果和新的 user message（session 内部维护完整历史）
            messages = self._build_next_messages(next_prompt, tool_results)

            self._trigger_hook("turn_after", {
                "turn": turn,
                "response": response,
                "tool_calls": tool_calls,
                "tool_results": tool_results,
                "next_prompt": next_prompt,
                "exit_reason": exit_reason,
                "model": self._model_name(),
            })

        # agent_after 钩子
        final_reason = exit_reason or {"result": "MAX_TURNS_EXCEEDED"}
        self._trigger_hook("agent_after", {
            "turns": turn,
            "exit_reason": final_reason,
            "model": self._model_name(),
        })

        return final_reason

    # ---- dispatch 消费 ----

    @staticmethod
    def _build_next_messages(
        next_prompt: str,
        tool_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build provider-compatible messages for the next LLM turn.

        GenericAgent carried tool results on a custom ``tool_results`` field.
        LiteLLM expects standard chat messages, so each tool result becomes a
        ``role=tool`` message before the user continuation.
        """
        messages: List[Dict[str, Any]] = []
        fallback_results: List[str] = []

        for result in tool_results:
            tool_use_id = str(result.get("tool_use_id") or "")
            content = result.get("content", "")
            if not isinstance(content, str):
                content = str(content)
            if tool_use_id:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": content,
                })
            else:
                fallback_results.append(f"<tool_result>{content}</tool_result>")

        continuation = next_prompt or ""
        if fallback_results:
            continuation = "\n".join(fallback_results + [continuation])
        if continuation.strip() or not messages:
            messages.append({"role": "user", "content": continuation})

        return messages

    def _consume_dispatch(self, gen: Generator) -> Generator[Any, None, StepOutcome]:
        """消费 dispatch() 返回的 generator，获取 StepOutcome.

        处理两种模式:
            verbose: 将中间 yield 透传给调用方（包裹在 ``` 代码块中）.
            非 verbose: 静默消费所有 yield，仅返回最终值.

        Args:
            gen: handler.dispatch() 返回的 generator.

        Yields:
            工具执行过程中的状态字符串.

        Returns:
            StepOutcome 实例.
        """
        try:
            v = next(gen)
        except StopIteration as e:
            return e.value

        if self.verbose:
            yield "```\n"
            yield v
            outcome = yield from gen
            yield "```\n"
            return outcome
        else:
            return self._exhaust(gen)

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
        """从响应对象中提取 token usage(含 cache), 缺失时返回空字典."""
        raw = getattr(response, "raw", None)
        usage = getattr(raw, "usage", None) if raw is not None else None
        if usage is None:
            usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        if isinstance(usage, dict):
            return {
                "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                **usage,
            }
        input_tokens = getattr(
            usage,
            "input_tokens",
            getattr(usage, "prompt_tokens", 0),
        )
        output_tokens = getattr(
            usage,
            "output_tokens",
            getattr(usage, "completion_tokens", 0),
        )
        cache_create = getattr(usage, "cache_creation_input_tokens", 0)
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
        }

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
