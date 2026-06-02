"""基于 litellm 的统一 LLM Session.

LiteLLMSession 封装 litellm.completion()，用一套代码处理所有 LLM 提供商。
支持流式/非流式、工具调用、thinking/reasoning、历史管理等。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Generator, List, Optional

import litellm

from zero_agent.core.config import LLMBackendConfig
from zero_agent.core.exceptions import LLMError
from zero_agent.llm.base import MockResponse


class LiteLLMSession:
    """基于 litellm 的统一 LLM 会话.

    封装 litellm.completion()，自动处理:
    - 提供商路由（Anthropic / OpenAI / DeepSeek / 等）
    - 流式和非流式响应
    - 原生工具调用
    - 消息历史管理
    - 上下文窗口裁剪

    Attributes:
        config: LLM 后端配置.
        history: 对话历史列表（OpenAI 消息格式）.
        system: 系统提示词.
        name: 会话名称（后端别名）.
    """

    def __init__(
        self,
        config: LLMBackendConfig,
        log_dir: Optional[str] = None,
        sessions_dir: Optional[str] = None,
    ) -> None:
        """初始化 LLM 会话.

        Args:
            config: 单个 LLM 后端的配置.
            log_dir: LLM 调用日志输出目录，None 时不记录.
            sessions_dir: 会话历史日志目录，None 时不记录.
        """
        self.config = config
        self.history: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.system = ""
        self.name = config.name or config.model
        self._context_window = config.context_window
        self._cut_msg_interval = 25
        self._trim_keep_rate = 0.3
        self._log_dir = log_dir
        self._sessions_dir = sessions_dir
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens
        self.tools: Optional[List[Dict[str, Any]]] = None

        # 资源使用追踪
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_cached_tokens: int = 0
        self._total_requests: int = 0

        # DeepSeek 模型有更大的上下文窗口
        if "deepseek" in config.model.lower():
            self._context_window = max(self._context_window, 70000)
            self._cut_msg_interval = 25
            self._trim_keep_rate = 0.3

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Generator[str, None, MockResponse]:
        """发送消息到 LLM 并获取流式响应.

        Generator 协议:
            - yield: 流式文本块（str），供 UI 实时展示.
            - return: MockResponse，包含完整内容和工具调用.

        Args:
            messages: 消息列表，OpenAI 格式 [{"role": ..., "content": ...}].
            tools: 工具 schema 列表，OpenAI 格式 [{"type": "function", "function": {...}}].

        Yields:
            文本内容块（流式时逐块 yield，非流式时一次性 yield 全部内容）.

        Returns:
            MockResponse 包含 content / tool_calls / thinking / stop_reason.
        """
        with self.lock:
            # 追加标准化后的消息到历史。AgentLoop may pass GenericAgent-style
            # tool_results; normalize them before they hit provider payloads.
            normalized_messages = self._normalize_incoming_messages(messages)
            self.history.extend(normalized_messages)
            self._trim_history()
            full_messages = self._build_messages()

        # 对 file_write 工具做特殊处理：content 参数不进入 schema，
        # 防止 LLM 把大段文件内容放进 tool call arguments
        tools = self._sanitize_tools(tools)

        # 记录 prompt 日志
        self._write_llm_log(
            "Prompt",
            json.dumps(normalized_messages, ensure_ascii=False, indent=2),
        )

        try:
            if self.config.stream:
                mock = yield from self._stream_chat(full_messages, tools)
            else:
                mock = yield from self._sync_chat(full_messages, tools)

            # 记录 response 日志
            resp_log = {
                "content": mock.content[:2000] if mock.content else "",
                "tool_calls": [
                    {"name": tc.function.name, "arguments": tc.function.arguments[:500]}
                    for tc in (mock.tool_calls or [])
                ],
                "thinking": mock.thinking[:500] if mock.thinking else "",
                "stop_reason": mock.stop_reason,
            }
            self._write_llm_log("Response", json.dumps(resp_log, ensure_ascii=False, indent=2))

            # Write model_responses log (compatible with continue_cmd / session history)
            self._write_model_response_log(messages, mock)

            return mock
        except Exception as e:
            raise LLMError(f"LLM 调用失败 [{self.name}]: {e}") from e

    def _stream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Generator[str, None, MockResponse]:
        """流式调用 LLM.

        Yields:
            逐块文本内容.

        Returns:
            聚合后的 MockResponse.
        """
        kwargs = self._build_completion_kwargs(messages, tools, stream=True)
        response = litellm.completion(**kwargs)

        collected_content = ""
        collected_thinking = ""
        collected_tool_calls: Dict[int, Dict[str, Any]] = {}
        final_response = None
        stream_interrupted = False

        try:
            for chunk in response:
                final_response = chunk
                try:
                    choice = chunk.choices[0]
                    delta = choice.delta if hasattr(choice, "delta") and choice.delta else None

                    if delta:
                        # 文本内容
                        if hasattr(delta, "content") and delta.content:
                            collected_content += delta.content
                            yield delta.content

                        # reasoning/thinking 内容
                        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                            collected_thinking += delta.reasoning_content

                        # 工具调用 delta
                        if hasattr(delta, "tool_calls") and delta.tool_calls:
                            for tc_delta in delta.tool_calls:
                                idx = tc_delta.index if hasattr(tc_delta, "index") else 0
                                if idx not in collected_tool_calls:
                                    collected_tool_calls[idx] = {
                                        "id": "",
                                        "name": "",
                                        "arguments": "",
                                    }
                                tc = collected_tool_calls[idx]
                                if hasattr(tc_delta, "id") and tc_delta.id:
                                    tc["id"] = tc_delta.id
                                if hasattr(tc_delta, "function") and tc_delta.function:
                                    if hasattr(tc_delta.function, "name") and tc_delta.function.name:
                                        tc["name"] = tc_delta.function.name
                                    if hasattr(tc_delta.function, "arguments") and tc_delta.function.arguments:
                                        tc["arguments"] += tc_delta.function.arguments
                except (AttributeError, IndexError):
                    continue
        except Exception:
            # 流中断：标记部分内容，使上层感知并处理
            stream_interrupted = True
            if collected_content:
                collected_content += "\n[!!! 流异常中断"

        # 构建最终响应 - 注入累积内容使 from_litellm_response 能正确解析
        if collected_content and final_response:
            try:
                if not final_response.choices[0].message:
                    # 对于某些 provider，最终 chunk 的 message 可能为 None
                    pass
            except Exception:
                pass

        mock = MockResponse.from_litellm_response(final_response, streamed_text=collected_content)

        # 如果流中断，在 content 末尾附加标记供 do_no_tool 检测
        if stream_interrupted:
            if not mock.content:
                mock.content = ""
            if "[!!! 流异常中断" not in mock.content:
                mock.content += "\n[!!! 流异常中断"
            mock.stop_reason = "stream_interrupted"

        # 如果流式解析丢失了 thinking 或 tool_calls，从累积数据补全
        if not mock.thinking and collected_thinking:
            mock.thinking = collected_thinking

        if not mock.tool_calls and collected_tool_calls:
            from zero_agent.llm.base import MockFunction, MockToolCall
            mock.tool_calls = [
                MockToolCall(
                    function=MockFunction(name=tc["name"], arguments=tc["arguments"]),
                    id=tc["id"],
                )
                for tc in sorted(collected_tool_calls.values(), key=lambda x: list(collected_tool_calls.keys())[list(collected_tool_calls.values()).index(x)])
            ]
            mock.stop_reason = "tool_use"

        # 如果 litellm 未返回 tool_calls，尝试从文本中回退解析
        if not mock.tool_calls and collected_content:
            text_calls = self._parse_text_tool_calls(collected_content)
            if text_calls:
                from zero_agent.llm.base import MockFunction, MockToolCall
                mock.tool_calls = [
                    MockToolCall(
                        function=MockFunction(
                            name=tc["tool_name"],
                            arguments=json.dumps(tc["args"], ensure_ascii=False)
                            if isinstance(tc["args"], dict) else str(tc["args"]),
                        ),
                        id=tc["id"],
                    )
                    for tc in text_calls
                ]
                mock.stop_reason = "tool_use"

        # 将助手消息追加到历史
        self._record_usage(
            getattr(final_response, "usage", None),
            streamed_text=collected_content,
        )
        self._record_assistant(mock)

        return mock

    def _sync_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Generator[str, None, MockResponse]:
        """非流式调用 LLM.

        Yields:
            一次性全部文本内容.

        Returns:
            MockResponse.
        """
        kwargs = self._build_completion_kwargs(messages, tools, stream=False)
        response = litellm.completion(**kwargs)

        mock = MockResponse.from_litellm_response(response)
        yield mock.content

        # 文本回退：litellm 未返回 tool_calls 时尝试从文本解析
        if not mock.tool_calls and mock.content:
            text_calls = self._parse_text_tool_calls(mock.content)
            if text_calls:
                from zero_agent.llm.base import MockFunction, MockToolCall
                mock.tool_calls = [
                    MockToolCall(
                        function=MockFunction(
                            name=tc["tool_name"],
                            arguments=json.dumps(tc["args"], ensure_ascii=False)
                            if isinstance(tc["args"], dict) else str(tc["args"]),
                        ),
                        id=tc["id"],
                    )
                    for tc in text_calls
                ]
                mock.stop_reason = "tool_use"

        self._record_usage(getattr(response, "usage", None))
        self._record_assistant(mock)
        return mock

    def _build_completion_kwargs(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
    ) -> Dict[str, Any]:
        """构建传递给 litellm.completion() 的参数.

        Args:
            messages: 消息列表.
            tools: 工具 schema 列表.
            stream: 是否流式.

        Returns:
            litellm.completion() 的关键字参数字典.
        """
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": stream,
            "api_key": self.config.api_key,
            "api_base": self.config.api_base,
            "temperature": self.config.temperature,
        }
        if self.config.provider:
            kwargs["custom_llm_provider"] = self.config.provider

        if self.config.max_tokens:
            kwargs["max_tokens"] = self.config.max_tokens

        # Tool schema 缓存：最后一个 tool 标记 ephemeral cache（与 GenericAgent NativeClaudeSession 对齐）
        if tools and "claude" in self.config.provider.lower():
            tools = list(tools)
            if tools:
                tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}

        if tools:
            kwargs["tools"] = tools

        if self.config.max_retries:
            kwargs["num_retries"] = self.config.max_retries

        if self.config.connect_timeout:
            import httpx
            kwargs["timeout"] = httpx.Timeout(
                timeout=self.config.read_timeout,
                connect=self.config.connect_timeout,
            )

        if self.config.proxy:
            # 设置环境变量供 litellm httpx 客户端使用（trust_env=True）
            os.environ["HTTP_PROXY"] = self.config.proxy
            os.environ["HTTPS_PROXY"] = self.config.proxy

        # 自定义 HTTP 标头（CC relay 等场景）
        if self.config.extra_headers:
            kwargs["extra_headers"] = self.config.extra_headers

        # service_tier (OpenAI)
        if self.config.service_tier:
            kwargs["service_tier"] = self.config.service_tier

        # SSL 验证
        if not self.config.verify:
            kwargs["ssl_verify"] = False

        # Claude thinking 支持
        if self.config.thinking_type and "claude" in self.config.provider.lower():
            thinking = {"type": self.config.thinking_type}
            if self.config.thinking_type == "enabled" and self.config.thinking_budget_tokens:
                thinking["budget_tokens"] = self.config.thinking_budget_tokens
            kwargs["thinking"] = thinking

        # reasoning_effort (OpenAI o-series, Claude 等)
        if self.config.reasoning_effort:
            kwargs["reasoning_effort"] = self.config.reasoning_effort

        # Responses API 模式
        if self.config.api_mode and self.config.api_mode != "chat_completions":
            kwargs["api_mode"] = self.config.api_mode

        return kwargs

    def _build_messages(self) -> List[Dict[str, Any]]:
        """从历史构建完整的消息列表，包含 system prompt.

        自动修复消息格式并标记缓存.

        Returns:
            完整的消息列表.
        """
        messages: List[Dict[str, Any]] = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
        messages.extend(self._history_without_session_system())

        # 修复 Anthropic/OpenAI 消息格式问题
        messages = self._fix_messages(messages)

        # 标记 Anthropic 缓存
        messages = self._stamp_cache_markers(
            messages, provider=self.config.provider,
        )

        return messages

    def _normalize_incoming_messages(
        self,
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Normalize loop messages before storing them in session history."""
        normalized: List[Dict[str, Any]] = []

        for raw_msg in messages:
            msg = dict(raw_msg)
            role = str(msg.get("role", "user")).lower()

            if role == "system":
                content = msg.get("content", "")
                if content:
                    self.system = str(content)
                continue

            tool_results = msg.pop("tool_results", None) or []
            if tool_results:
                fallback_texts: List[str] = []
                for result in tool_results:
                    tool_use_id = str(result.get("tool_use_id") or "")
                    content = result.get("content", "")
                    if not isinstance(content, str):
                        content = str(content)
                    if tool_use_id:
                        normalized.append({
                            "role": "tool",
                            "tool_call_id": tool_use_id,
                            "content": content,
                        })
                    else:
                        fallback_texts.append(
                            f"<tool_result>{content}</tool_result>"
                        )

                content = msg.get("content", "")
                if fallback_texts:
                    msg["content"] = "\n".join(
                        fallback_texts + ([str(content)] if content else [])
                    )
                if str(msg.get("content", "")).strip():
                    normalized.append(msg)
                continue

            normalized.append(msg)

        return normalized

    def _history_without_session_system(self) -> List[Dict[str, Any]]:
        """Return history entries, excluding system prompts owned by session."""
        result: List[Dict[str, Any]] = []
        for msg in self.history:
            if msg.get("role") == "system":
                continue
            result.append(msg)
        return result

    def _trim_history(self) -> None:
        """裁剪历史消息，防止超出上下文窗口.

        混合策略:
            1. 先对早期消息做标签级压缩 (compress_history_tags).
            2. 再按消息数量裁剪，保留最近的 N 条.
        """
        max_messages = self._context_window // 100  # 粗略估算
        if len(self.history) <= max_messages:
            return

        # 步骤 1: 对早期消息做标签级压缩
        if len(self.history) > max_messages * 0.5:
            keep_recent = int(max_messages * 0.3)
            older = self.history[:-keep_recent] if keep_recent > 0 else []
            recent = self.history[-keep_recent:] if keep_recent > 0 else self.history
            if older:
                older = self._compress_history_tags(older)
            self.history = older + recent

        # 步骤 2: 如果仍然超限，裁剪到保留比例
        if len(self.history) > max_messages:
            keep = max(int(max_messages * self._trim_keep_rate), 2)
            self.history = self.history[-keep:]

    def _record_assistant(self, mock: MockResponse) -> None:
        """将助手响应追加到历史.

        Args:
            mock: MockResponse 实例.
        """
        msg: Dict[str, Any] = {"role": "assistant", "content": mock.content or ""}

        if mock.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for i, tc in enumerate(mock.tool_calls)
            ]
            # 如果有 tool_calls，content 可能为 None
            if not mock.content:
                msg["content"] = None

        if mock.thinking:
            msg["reasoning_content"] = mock.thinking

        with self.lock:
            self.history.append(msg)

    @staticmethod
    def _sanitize_tools(
        tools: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """清理工具 schema：移除 file_write 的 content 参数描述.

        防止 LLM 把大段文件内容放进 tool call arguments 而非 reply body.

        Args:
            tools: 原始工具 schema 列表.

        Returns:
            清理后的工具 schema 列表.
        """
        if not tools:
            return tools
        sanitized = []
        for t in tools:
            func = t.get("function", {})
            if func.get("name") == "file_write":
                props = func.get("parameters", {}).get("properties", {})
                if "content" in props:
                    t = json.loads(json.dumps(t, ensure_ascii=False))
                    params = t["function"]["parameters"]
                    params["properties"].pop("content", None)
                    required = params.get("required")
                    if isinstance(required, list):
                        params["required"] = [
                            name for name in required if name != "content"
                        ]
                    extra = ". Content must be placed in <file_content> tags in reply body, not in args"
                    desc = t["function"].get("description", "")
                    if extra not in desc:
                        t["function"]["description"] = desc + extra
            sanitized.append(t)
        return sanitized

    # ---- 文本工具调用回退解析 ----

    @staticmethod
    def _parse_text_tool_calls(text: str) -> list[dict]:
        """从纯文本中提取工具调用，用于不支持原生 tool_calling 的模型.

        支持两种模式:
            1. XML: <tool_use>{"name":..., "arguments":...}</tool_use>
            2. JSON 数组: [{"type":"tool_use","name":...,"input":...}]

        Args:
            text: LLM 响应的纯文本内容.

        Returns:
            工具调用列表 [{"tool_name": str, "args": dict, "id": str}].
        """
        import json as _json
        import re as _re

        results: list[dict] = []

        # 模式 1: XML <tool_use> 标签
        xml_pattern = _re.compile(
            r"<tool_use>\s*(\{.*?\})\s*</tool_use>", _re.DOTALL
        )
        for match in xml_pattern.finditer(text):
            try:
                data = _json.loads(match.group(1))
                results.append({
                    "tool_name": data.get("name", "unknown"),
                    "args": data.get("arguments", data.get("input", {})),
                    "id": f"text_fallback_{len(results)}",
                })
            except _json.JSONDecodeError:
                continue

        if results:
            return results

        # 模式 2: JSON 数组格式
        json_array_pattern = _re.compile(
            r"\[\s*\{.*?\"type\"\s*:\s*\"tool_use\".*?\}\s*\]", _re.DOTALL
        )
        for match in json_array_pattern.finditer(text):
            try:
                arr = _json.loads(match.group(0))
                for item in arr:
                    if item.get("type") == "tool_use":
                        results.append({
                            "tool_name": item.get("name", "unknown"),
                            "args": item.get("input", item.get("arguments", {})),
                            "id": item.get("id", f"text_fallback_{len(results)}"),
                        })
            except _json.JSONDecodeError:
                continue

        if results:
            return results

        # 模式 3: 宽松的 <tool_call> 或类似标签订阅
        loose_pattern = _re.compile(
            r"<(?:tool_use|tool_call|function_call)>\s*(.*?)\s*</(?:tool_use|tool_call|function_call)>",
            _re.DOTALL,
        )
        for match in loose_pattern.finditer(text):
            raw = match.group(1).strip()
            tool_name = "unknown"
            args = {}
            name_match = _re.search(r"\"name\"\s*:\s*\"(\w+)\"", raw)
            if name_match:
                tool_name = name_match.group(1)
            args_match = _re.search(
                r"\"(?:arguments|input|args)\"\s*:\s*(\{.*?\})", raw, _re.DOTALL
            )
            if args_match:
                try:
                    args = _json.loads(args_match.group(1))
                except _json.JSONDecodeError:
                    args = {"raw": raw}
            else:
                args = {"raw": raw}
            results.append({
                "tool_name": tool_name,
                "args": args,
                "id": f"text_fallback_{len(results)}",
            })

        # 模式 4: 裸 JSON 对象（无 XML 包装）
        # 检测 {"name": ..., "arguments": ...} 格式
        if '"name":' in text and '"arguments":' in text:
            json_match = _re.search(
                r'\{[^{}]*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{[^{}]*\}[^{}]*\}',
                text, _re.DOTALL,
            )
            if json_match:
                try:
                    data = _json.loads(json_match.group(0))
                    func_name = (
                        data.get('name') or data.get('function')
                        or data.get('tool')
                    )
                    args = (
                        data.get('arguments') or data.get('args')
                        or data.get('params') or data.get('parameters') or {}
                    )
                    if func_name:
                        results.append({
                            "tool_name": func_name,
                            "args": args if isinstance(args, dict)
                                    else {"raw": str(args)},
                            "id": data.get(
                                "id", f"text_fallback_{len(results)}"
                            ),
                        })
                except _json.JSONDecodeError:
                    pass

        return results

    # ---- 粘连 JSON 解析 ----

    @staticmethod
    def _try_parse_tool_args(raw: str) -> dict:
        """尝试解析可能粘连/残缺的 JSON 对象字符串.

        处理 LLM 输出中的各种异常格式:
            - 粘连 JSON: {..}{..}
            - 残缺 JSON: 尾部缺失括号
            - markdown 代码块包装: ```json ... ```
            - 反引号包装: `` {...} ``

        Args:
            raw: 原始 JSON 参数字符串.

        Returns:
            解析后的参数字典.

        Raises:
            ValueError: 所有解析策略均失败.
        """
        import json as _json
        import re as _re

        # 策略 1: 直接解析
        try:
            return _json.loads(raw)
        except _json.JSONDecodeError:
            pass

        # 策略 2: 去除反引号和 "json" 前缀后重试
        try:
            cleaned = raw.strip().strip("`").replace("json\n", "", 1).strip()
            return _json.loads(cleaned)
        except _json.JSONDecodeError:
            pass

        # 策略 3: 尝试删除末尾 1 个字符（处理残缺 JSON）
        try:
            return _json.loads(raw[:-1])
        except (_json.JSONDecodeError, IndexError):
            pass

        # 策略 4: 找到最后一个 '}' 并截断到此处
        try:
            if "}" in raw:
                truncated = raw[: raw.rfind("}") + 1]
                return _json.loads(truncated)
        except _json.JSONDecodeError:
            pass

        # 策略 5: 正则分割粘连的 JSON 对象，取第一个完整对象
        try:
            parts = _re.split(r"(?<=\})(?=\s*\{)", raw)
            for part in parts:
                part = part.strip()
                if part.startswith("{"):
                    return _json.loads(part)
        except Exception:
            pass

        # 策略 6: 清理 markdown 代码块包装后重试
        cleaned = _re.sub(r"^```(?:json)?\s*", "", raw.strip())
        cleaned = _re.sub(r"\s*```$", "", cleaned)
        try:
            return _json.loads(cleaned)
        except _json.JSONDecodeError:
            pass

        # 策略 7: 提取第一个 { } 块
        match = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if match:
            try:
                return _json.loads(match.group(0))
            except _json.JSONDecodeError:
                pass

        raise ValueError(f"无法解析工具参数: {raw[:200]}")

    # ---- 历史压缩 ----

    @staticmethod
    def _compress_history_tags(history: list[dict]) -> list[dict]:
        """压缩历史消息中过长的标签内容.

        对 <thinking>, <tool_use>, <tool_result> 标签做头尾截断，
        保留关键信息同时减少 token 消耗.

        Args:
            history: 原始历史消息列表.

        Returns:
            压缩后的历史消息列表（浅拷贝，仅修改 content 字段）.
        """
        import re as _re

        TAIL = 400
        TAG_TRUNC = " ... [TRUNCATED] "

        def _compress(text: str) -> str:
            if not text or len(text) <= 1000:
                return text

            for tag in ("thinking", "think", "tool_use", "tool_result",
                        "history", "key_info", "earlier_context"):
                pattern = _re.compile(
                    rf"<{tag}>(.*?)</{tag}>", _re.DOTALL
                )
                def _replacer(m: _re.Match, t=tag) -> str:
                    body = m.group(1)
                    if len(body) <= TAIL * 2 + 100:
                        return m.group(0)
                    return f"<{t}>{body[:TAIL]}{TAG_TRUNC}{body[-TAIL:]}</{t}>"
                text = pattern.sub(_replacer, text)

            return text

        result = []
        for msg in history:
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                compressed = _compress(content)
                if compressed != content:
                    msg = {**msg, "content": compressed}
            result.append(msg)

        return result

    # ---- 消息格式修复 ----

    @staticmethod
    def _fix_messages(messages: list[dict]) -> list[dict]:
        """修复 Anthropic/OpenAI 消息格式问题.

        修复:
            1. 合并相同角色的连续消息.
            2. 修复孤立的 tool_result（无对应 tool_use）.
            3. 修复孤立的 tool_use（无对应 result 的转为纯文本）.
            4. 确保首条消息为 user/system 角色.

        Args:
            messages: 原始消息列表.

        Returns:
            修复后的消息列表.
        """
        if not messages:
            return messages

        # 1. 合并相同角色的连续文本消息.
        # Tool results must stay one message per tool_call_id to preserve
        # provider protocol continuity after multi-tool assistant turns.
        merged = []
        for msg in messages:
            role = msg.get("role", "")
            if (
                role != "tool"
                and merged
                and merged[-1].get("role") == role
                and not msg.get("tool_calls")
                and not merged[-1].get("tool_calls")
            ):
                prev_content = merged[-1].get("content", "")
                curr_content = msg.get("content", "")
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    merged[-1] = {
                        **merged[-1],
                        "content": prev_content + "\n" + curr_content,
                    }
                    continue
            merged.append(msg)

        # 2. 修复孤立的 tool_result: 收集所有 tool_use ids
        tool_use_ids = set()
        for msg in merged:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    tc_id = tc.get("id", "")
                    if tc_id:
                        tool_use_ids.add(tc_id)

        # 3. 修复孤立的 tool_use: 检查未匹配的 tool_result
        fixed = []
        for msg in merged:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id", "")
                if tc_id and tc_id not in tool_use_ids:
                    # 孤儿 tool_result: 注入 "(error)" 标记
                    content = msg.get("content", "")
                    fixed.append({
                        "role": "user",
                        "content": f"(error) orphan tool_result id={tc_id}: {content}",
                    })
                    continue
            elif msg.get("role") == "assistant":
                tool_calls = msg.get("tool_calls", [])
                orphan_tools = [
                    tc for tc in tool_calls
                    if tc.get("id") not in tool_use_ids and tc.get("id")
                ]
                if orphan_tools and not msg.get("content"):
                    # 孤立的 tool_use 转为纯文本
                    fixed.append({
                        "role": "assistant",
                        "content": f"[orphan tool_calls: {orphan_tools}]",
                    })
                    continue
            fixed.append(msg)

        # 4. 确保首条消息为 user/system
        if fixed and fixed[0].get("role") not in ("user", "system"):
            fixed.insert(0, {"role": "user", "content": "Hello"})

        return fixed

    # ---- 缓存控制标记 ----

    @staticmethod
    def _stamp_cache_markers(
        messages: list[dict],
        provider: str = "",
    ) -> list[dict]:
        """在消息上设置 cache_control 标记，与 GenericAgent 的缓存策略对齐.

        对 Anthropic 模型启用 prompt caching:
            - System prompt → "persistent" 缓存（跨请求复用）
            - 最后 2 条 user 消息 → "ephemeral" 缓存

        也支持 OAI 兼容端点（OpenRouter 等）通过 Claude 模型时的缓存标记.

        Args:
            messages: 消息列表.
            provider: 后端提供商类型 ("claude", "openai" 等).

        Returns:
            标记后的消息列表.
        """
        if not messages:
            return messages

        is_claude_provider = "claude" in provider.lower()
        is_oai_claude_relay = not is_claude_provider and LiteLLMSession._has_claude_model_in_messages(messages)

        if not is_claude_provider and not is_oai_claude_relay:
            return messages

        user_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "user"
        ]
        stamp_indices = set(user_indices[-2:])  # 最后 2 条 user 消息

        result = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")

            # System prompt → persistent cache
            if role == "system" and isinstance(content, str) and content:
                if is_claude_provider:
                    msg = {
                        **msg,
                        "content": [{
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "persistent"},
                        }],
                    }
                # OAI relay: system 也用 ephemeral（OAI 不区分 persistent）
                elif is_oai_claude_relay:
                    msg = {
                        **msg,
                        "content": [{
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }],
                    }
            # 最后 2 条 user 消息 → ephemeral cache
            elif i in stamp_indices:
                if isinstance(content, str):
                    msg = {
                        **msg,
                        "content": [{
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }],
                    }
                elif isinstance(content, list):
                    # OAI relay: 为 content list 中最后一个 text block 标记
                    content_list = list(content)
                    for j in range(len(content_list) - 1, -1, -1):
                        if isinstance(content_list[j], dict) and content_list[j].get("type") == "text":
                            content_list[j] = {**content_list[j], "cache_control": {"type": "ephemeral"}}
                            break
                    msg = {**msg, "content": content_list}
            result.append(msg)

        return result

    @staticmethod
    def _has_claude_model_in_messages(messages: list[dict]) -> bool:
        """检测消息历史中是否包含 Claude 模型的 tool_use 标记.

        用于判断 OAI 兼容中继场景是否需要缓存标记.
        """
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "")
                        if block_type in ("tool_use", "tool_result"):
                            return True
        return False

    # ---- Thinking block 边界处理 ----

    @staticmethod
    def _keep_claude_block(block: dict) -> bool:
        """判断 Claude content block 是否应保留.

        过滤掉无签名（signature）的 thinking block，
        与 GenericAgent 的 _keep_claude_block 对齐.

        Args:
            block: 单个 content block 字典.

        Returns:
            True 表示保留，False 表示丢弃.
        """
        if not isinstance(block, dict):
            return True
        if block.get("type") != "thinking":
            return True
        return bool(block.get("signature"))

    @staticmethod
    def _drop_unsigned_thinking(messages: list[dict]) -> list[dict]:
        """删除消息中无签名的 thinking blocks.

        某些 API 版本要求 thinking block 必须带有签名，
        否则会拒绝请求.

        Args:
            messages: 消息列表.

        Returns:
            过滤后的消息列表.
        """
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = [
                    b for b in content
                    if LiteLLMSession._keep_claude_block(b)
                ]
        return messages

    @staticmethod
    def _ensure_thinking_blocks(messages: list[dict], model: str) -> list[dict]:
        """DeepSeek 模型需要在历史中保留 thinking blocks.

        DeepSeek 要求 assistant 消息中至少有一个 thinking block，
        否则会报错。此函数为缺失 thinking 的历史消息注入占位符.

        Args:
            messages: 消息列表.
            model: 模型名称（用于判断是否需要处理）.

        Returns:
            补充后的消息列表.
        """
        if "deepseek" not in model.lower():
            return messages
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            has_thinking = any(
                isinstance(b, dict) and b.get("type") == "thinking"
                for b in content
            )
            if not has_thinking:
                msg["content"] = [
                    {
                        "type": "thinking",
                        "thinking": "...",
                        "signature": "placeholder",
                    },
                    *content,
                ]
        return messages

    @staticmethod
    def _ensure_text_block(blocks: list[dict]) -> Optional[str]:
        """若响应有 thinking 但无 text block，注入合成摘要.

        某些模型返回的响应可能只有 thinking 没有 text，
        这会导致后续轮次出现空 content 错误.
        从 thinking 第一行提取摘要注入为 text block.

        Args:
            blocks: 响应的 content blocks 列表.

        Returns:
            注入的摘要文本，若无需注入则返回 None.
        """
        if any(b.get("type") == "text" for b in blocks):
            return None
        thinking = next(
            (b.get("thinking", "") for b in blocks if b.get("type") == "thinking"),
            "",
        )
        if not thinking:
            return None
        line = thinking.strip().split("\n", 1)[0]
        summary = (
            "<summary>" + (line[:60] + "..." if len(line) > 60 else line) + "</summary>"
        )
        blocks.insert(1, {"type": "text", "text": summary})
        return summary

    # ---- LLM 调用日志 ----

    def _record_usage(self, usage: Any, streamed_text: str = "") -> None:
        """记录 LLM 调用的 token 使用和缓存命中统计.

        Args:
            usage: litellm response 的 usage 对象.
            streamed_text: 流式调用时收集的文本内容，用于估算 output tokens.
        """
        if usage is None:
            if streamed_text:
                # 流式调用可能没有 usage 对象，从文本长度估算
                estimated = max(len(streamed_text) // 3, 1)
                self._total_output_tokens += estimated
            self._total_requests += 1
            return

        inp = getattr(usage, "prompt_tokens", 0)
        out = getattr(usage, "completion_tokens", 0)

        # Anthropic 缓存 tokens
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0)
        cache_read = getattr(usage, "cache_read_input_tokens", 0)

        # OpenAI 缓存 tokens
        if not cache_read:
            details = getattr(usage, "prompt_tokens_details", None) or {}
            if isinstance(details, dict):
                cache_read = details.get("cached_tokens", 0)
        if not cache_read:
            details = getattr(usage, "input_tokens_details", None) or {}
            if isinstance(details, dict):
                cache_read = details.get("cached_tokens", 0)

        self._total_input_tokens += inp
        self._total_output_tokens += out
        self._total_cached_tokens += cache_read + cache_creation
        self._total_requests += 1

        # 打印资源使用信息
        parts = [f"[Usage] #{self._total_requests} input={inp}"]
        if out:
            parts.append(f"output={out}")
        if cache_read:
            parts.append(f"cache_read={cache_read}")
        if cache_creation:
            parts.append(f"cache_create={cache_creation}")
        if self._total_cached_tokens:
            hit_rate = (
                self._total_cached_tokens
                / max(self._total_input_tokens, 1)
                * 100
            )
            parts.append(f"cumulative_cache_hit={hit_rate:.1f}%")
        print(" ".join(parts))

    @property
    def usage_stats(self) -> dict:
        """获取累计资源使用统计.

        Returns:
            包含 input/output/cached/requests 的统计字典.
        """
        return {
            "total_requests": self._total_requests,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_cached_tokens": self._total_cached_tokens,
            "cache_hit_rate": (
                self._total_cached_tokens
                / max(self._total_input_tokens, 1)
                * 100
            ),
        }

    def _write_llm_log(self, label: str, content: str) -> None:
        """将 LLM 交互日志写入文件.

        Args:
            label: 日志标签 (e.g., "Prompt", "Response").
            content: 日志内容.
        """
        import os
        import time

        log_dir = getattr(self, "_log_dir", None)
        if not log_dir:
            return

        os.makedirs(log_dir, exist_ok=True)
        safe_name = self.name.replace("/", "_").replace("\\", "_")
        log_path = os.path.join(log_dir, f"{safe_name}.log")

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = (
            f"\n{'=' * 60}\n"
            f"[{timestamp}] {label}\n"
            f"{'=' * 60}\n"
            f"{content}\n"
        )

        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            pass

    def _write_model_response_log(
        self, messages: List[Dict[str, Any]], mock: MockResponse
    ) -> None:
        """Write Prompt/Response pair to sessions_dir for session history.

        Format matches GenericAgent's _write_llm_log and is parseable by
        continue_cmd._pairs():
            === Prompt === {timestamp}
            {json of the last user/tool message}
            === Response === {timestamp}
            {repr of content blocks}

        Args:
            messages: The message list sent to the LLM for this turn.
            mock: The MockResponse from the LLM.
        """
        import time as _time

        if not self._sessions_dir:
            return
        os.makedirs(self._sessions_dir, exist_ok=True)
        log_path = os.path.join(self._sessions_dir, f"model_responses_{os.getpid()}.txt")

        # Prompt: last message in the list (user or tool continuation)
        prompt_msg = messages[-1] if messages else {}

        # Response: content blocks in the format continue_cmd expects
        blocks: List[Dict[str, Any]] = []
        if mock.content:
            blocks.append({"type": "text", "text": mock.content})
        for tc in (mock.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}
            blocks.append({
                "type": "tool_use",
                "name": tc.function.name,
                "input": args,
                "id": getattr(tc, "id", ""),
            })
        if mock.thinking:
            blocks.append({"type": "thinking", "thinking": mock.thinking})

        ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"=== Prompt === {ts}\n"
                        f"{json.dumps(prompt_msg, ensure_ascii=False)}\n")
                f.write(f"=== Response === {ts}\n{repr(blocks)}\n")
        except Exception:
            pass
