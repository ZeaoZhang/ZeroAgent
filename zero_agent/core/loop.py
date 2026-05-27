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
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": initial_content},
        ]
        # 将 system prompt 写入 session，使其在历史裁剪时持久保留
        self.client.system = system_prompt

        turn = 0
        self.handler.max_turns = self.max_turns

        # agent_before 钩子
        if self.hooks:
            self.hooks.trigger("agent_before", locals())

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

            # turn_before 钩子
            if self.hooks:
                self.hooks.trigger("turn_before", locals())

            # ——— LLM 调用 ———
            if self.hooks:
                self.hooks.trigger("llm_before", locals())
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
            if self.hooks:
                self.hooks.trigger("llm_after", locals())

            if not response.tool_calls:
                tool_calls = [{"tool_name": "no_tool", "args": {}}]
            else:
                tool_calls = [
                    {
                        "tool_name": tc.function.name,
                        "args": json.loads(tc.function.arguments),
                        "id": tc.id,
                    }
                    for tc in response.tool_calls
                ]

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
                    break

            # ——— 构建下一轮 prompt ———
            next_prompt = "\n".join(next_prompts)
            next_prompt = self.handler.turn_end_callback(
                response, tool_calls, tool_results, turn,
                next_prompt, exit_reason,
            )

            # 下一轮只发新的 user message（session 内部维护完整历史）
            messages = [
                {
                    "role": "user",
                    "content": next_prompt,
                    "tool_results": tool_results,
                }
            ]

            # turn_after 钩子
            if self.hooks:
                self.hooks.trigger("turn_after", locals())

        # 最终回调（退出时）
        if exit_reason:
            self.handler.turn_end_callback(
                response, tool_calls, tool_results, turn, "", exit_reason,
            )

        # agent_after 钩子
        if self.hooks:
            self.hooks.trigger("agent_after", locals())

        return exit_reason or {"result": "MAX_TURNS_EXCEEDED"}

    # ---- dispatch 消费 ----

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
