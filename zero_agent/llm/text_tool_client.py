"""text_tool_client — text-based tool-call protocol fallback.

Some models/providers cannot use native function calling (e.g. some o-series
endpoints, open-source models behind relay proxies). For those, TextToolSession
wraps an existing LLM session (LiteLLMSession or AutoFailoverSession) and
emulates tool calling through a text interaction protocol:
    <thinking>...</thinking><summary>...</summary><tool_use>{...}</tool_use>

The wrapped session is called with `tools=None` and a synthetic prompt built
from the message history + tool schema. This keeps logging, history, retries,
cost tracking, and failover behavior inside the existing session stack.

Public API:
    TextToolSession(backend, auto_save_tokens=True)
        .chat(messages, tools=None) -> Generator[str, None, MockResponse]
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Generator, List, Optional

from zero_agent.llm.base import MockFunction, MockResponse, MockToolCall


_THINK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL)
_TOOL_USE_TAG_RE = re.compile(
    r"<(?:tool_use|tool_call)>((?:(?!<(?:tool_use|tool_call)>).){15,}?)</(?:tool_use|tool_call)>",
    re.DOTALL,
)
_BAD_JSON_PREFIX = "Failed to parse tool_use JSON: "


class TextToolSession:
    """Text-protocol fallback wrapper around any LLM session.

    Attributes:
        backend: The wrapped session (LiteLLMSession / AutoFailoverSession).
        auto_save_tokens: When True, compress repeated tool descriptions.
        last_tools: Last emitted tool JSON for staleness detection.
        name / log_path / temperature / max_tokens: delegated to backend.
    """

    def __init__(self, backend: Any, auto_save_tokens: bool = True) -> None:
        self.backend = backend
        self.auto_save_tokens = auto_save_tokens
        self.last_tools: str = ""
        self._last_tools_json: str = ""
        self.total_cd_tokens: int = 0

    # ---- delegated properties ----
    @property
    def name(self) -> str:
        return getattr(self.backend, "name", "text_tool")

    @property
    def config(self) -> Any:
        return getattr(self.backend, "config", None)

    @property
    def history(self) -> List[Dict[str, Any]]:
        return getattr(self.backend, "history", [])

    @history.setter
    def history(self, value: List[Dict[str, Any]]) -> None:
        setattr(self.backend, "history", value)

    @property
    def system(self) -> str:
        return getattr(self.backend, "system", "")

    @system.setter
    def system(self, value: str) -> None:
        setattr(self.backend, "system", value)

    @property
    def log_path(self) -> Any:
        return getattr(self.backend, "log_path", None)

    @log_path.setter
    def log_path(self, value: Any) -> None:
        setattr(self.backend, "log_path", value)

    @property
    def temperature(self) -> float:
        return getattr(self.backend, "temperature", 1.0)

    @temperature.setter
    def temperature(self, value: float) -> None:
        setattr(self.backend, "temperature", value)

    @property
    def max_tokens(self) -> Optional[int]:
        return getattr(self.backend, "max_tokens", None)

    @max_tokens.setter
    def max_tokens(self, value: Optional[int]) -> None:
        setattr(self.backend, "max_tokens", value)

    @property
    def usage_stats(self) -> dict:
        return getattr(self.backend, "usage_stats", {})

    @property
    def extra_sys_prompt(self) -> str:
        return getattr(self.backend, "extra_sys_prompt", "")

    @extra_sys_prompt.setter
    def extra_sys_prompt(self, value: str) -> None:
        setattr(self.backend, "extra_sys_prompt", value)

    def reset_tool_protocol_cache(self) -> None:
        self.last_tools = ""
        self._last_tools_json = ""

    # ---- chat: text-protocol emulation ----
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Generator[str, None, MockResponse]:
        """Send messages to the wrapped session with a synthetic text protocol.

        The wrapped session receives `tools=None` plus a built prompt that
        includes the interaction protocol and tool schema. Tool calls are
        parsed back from the response text into MockToolCall objects.

        Yields:
            Visible text chunks (excluding <thinking> and <tool_use> blocks).
        Returns:
            MockResponse with parsed tool_calls and visible content.
        """
        # Build the protocol prompt
        full_prompt = self._build_protocol_prompt(messages, tools)

        # Delegate to the wrapped session with no native tools
        raw_text = ""
        gen = self.backend.chat(full_prompt, tools=None)
        try:
            for chunk in gen:
                raw_text += chunk
                yield chunk
        finally:
            if hasattr(gen, "close"):
                gen.close()

        # Parse the response back into MockResponse
        return self._parse_mixed_response(raw_text)

    # ---- protocol prompt construction ----
    def _build_protocol_prompt(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Build the synthetic messages array with the text protocol.

        The first system message gets the interaction protocol + tool schema
        appended. User/assistant messages are rendered into a single user
        message in the "=== ROLE ===" block format GA uses.
        """
        # Normalize tools for JSON emission
        tools = json.loads(json.dumps(tools, ensure_ascii=False)) if tools else tools

        # file_write content hint: content goes in <file_content> tags in body, not args
        if tools:
            for t in tools:
                f = t.get("function", {})
                if f.get("name") == "file_write":
                    props = f.get("parameters", {}).get("properties", {})
                    props.pop("content", None)
                    extra = '. Content must be placed in <file_content> tags in reply body, not in args'
                    if extra not in f.get("description", ""):
                        f["description"] = f.get("description", "") + extra
                    break

        tool_instruction = self._prepare_tool_instruction(tools)

        # Extract system content
        system_content = ""
        history_msgs: List[Dict[str, Any]] = []
        for m in messages:
            role = (m.get("role") or "").lower()
            if role == "system":
                if not system_content:
                    system_content = str(m.get("content", ""))
                continue
            history_msgs.append(m)

        # Build the synthetic system + user text
        system = ""
        if system_content:
            system += f"{system_content}\n"
        system += tool_instruction

        # Render history into a single user block
        user = ""
        for m in history_msgs:
            role = "USER" if (m.get("role") == "user") else "ASSISTANT"
            user += f"=== {role} ===\n"
            for tr in m.get("tool_results", []) or []:
                tr_content = tr.get("content", "") if isinstance(tr, dict) else str(tr)
                user += f"<tool_result>{tr_content}</tool_result>\n"
            content = m.get("content", "")
            if isinstance(content, list):
                # Multimodal: extract text blocks
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                content = "\n".join(parts)
            user += str(content) + "\n"
            self.total_cd_tokens += len(user) // 3

        # If conversation is long, reset tool cache to re-emit full schema
        if self.total_cd_tokens > 9000:
            self.last_tools = ""
        user += "=== ASSISTANT ===\n"

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _prepare_tool_instruction(self, tools: Optional[List[Dict[str, Any]]]) -> str:
        """Build the text protocol tool instruction block.

        Compresses repeated identical tool schemas by emitting a
        "tools still active" pointer when the JSON is unchanged.
        """
        if not tools:
            return ""

        tools_json = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        _en = os.environ.get("ZA_LANG") == "en"

        if _en:
            tool_instruction = (
                "\n### Interaction Protocol (must follow strictly, always in effect)\n"
                "Follow these steps to think and act:\n"
                "1. **Think**: Analyze the current situation and strategy inside `<thinking>` tags.\n"
                "2. **Summarize**: Output a minimal one-line (<30 words) physical snapshot in `<summary>`: "
                "new info from last tool result + current tool call intent. This goes into long-term "
                "working memory. Must contain real information, no filler.\n"
                "3. **Act**: If you need to call tools, output one or more **<tool_use> blocks** after your reply, then stop.\n"
                f"\nFormat: ```<tool_use>{{\"name\": \"tool_name\", \"arguments\": {{...}}}}</tool_use>```\n"
                f"\n### Tools (mounted, always in effect):\n{tools_json}\n"
            )
        else:
            tool_instruction = (
                "\n### 交互协议 (必须严格遵守，持续有效)\n"
                "请按照以下步骤思考并行动：\n"
                "1. **思考**: 在 `<thinking>` 标签中先进行思考，分析现状和策略。\n"
                "2. **总结**: 在 `<summary>` 中输出*极为简短*的高度概括的单行（<30字）物理快照，"
                "包括上次工具调用结果产生的新信息+本次工具调用意图。此内容将进入长期工作记忆，"
                "记录关键信息，严禁输出无实际信息增量的描述。\n"
                "3. **行动**: 如需调用工具，请在回复正文之后输出一个（或多个）**<tool_use>块**，然后结束。\n"
                f"\n格式: ```<tool_use>{{\"name\": \"tool_name\", \"arguments\": {{...}}}}</tool_use>```\n"
                f"\n### Tools (mounted, always in effect):\n{tools_json}\n"
            )

        if self.auto_save_tokens and self._last_tools_json == tools_json:
            if _en:
                tool_instruction = (
                    "\n### Tools: still active, **ready to call**. Protocol unchanged.\n"
                )
            else:
                tool_instruction = (
                    "\n### 工具库状态：持续有效（code_run/file_read等），"
                    "**可正常调用**。调用协议沿用。\n"
                )
        else:
            self.total_cd_tokens = 0
        self._last_tools_json = tools_json
        return tool_instruction

    # ---- response parsing ----
    def _parse_mixed_response(self, text: str) -> MockResponse:
        """Parse text-protocol response into MockResponse.

        Extracts <thinking>, <tool_use> blocks, and fallback JSON patterns.
        Visible content excludes thinking and tool_use blocks.
        """
        remaining_text = text
        thinking = ""

        think_match = _THINK_RE.search(text)
        if think_match:
            thinking = think_match.group(1).strip()
            remaining_text = _THINK_RE.sub("", remaining_text, count=0)

        tool_calls, remaining_text = self._parse_text_tool_calls(remaining_text)

        if not tool_calls:
            # Weak fallback: incomplete <tool_use> tag or bare JSON
            json_strs: List[str] = []
            errors: List[str] = []
            if "<tool_use>" in remaining_text:
                weaktoolstr = remaining_text.split("<tool_use>")[-1].strip()
                weaktoolstr_orig = weaktoolstr
                # Strip closing tag suffixes before angle-bracket cleanup
                for suffix in ("</tool_use>", "</tool_call>"):
                    if weaktoolstr.endswith(suffix):
                        weaktoolstr = weaktoolstr[: -len(suffix)].strip()
                # Now strip leading/trailing angle brackets
                weaktoolstr = weaktoolstr.strip("><")
                json_str = weaktoolstr if weaktoolstr.endswith("}") else ""
                if json_str == "" and "```" in weaktoolstr and weaktoolstr.split("```")[0].strip().endswith("}"):
                    json_str = weaktoolstr.split("```")[0].strip()
                if json_str:
                    json_strs.append(json_str)
                remaining_text = remaining_text.replace("<tool_use>" + weaktoolstr_orig, "")
            elif '"name":' in remaining_text and '"arguments":' in remaining_text:
                json_match = re.search(r'\{.*"name":.*\}', remaining_text, re.DOTALL)
                if json_match:
                    json_strs.append(json_match.group(0).strip())
                    remaining_text = remaining_text.replace(json_match.group(0), "").strip()

            for json_str in json_strs:
                try:
                    data = json.loads(json_str)
                    func_name = data.get("name") or data.get("function") or data.get("tool")
                    args = (
                        data.get("arguments")
                        or data.get("args")
                        or data.get("params")
                        or data.get("parameters")
                        or data.get("input")
                    )
                    if args is None:
                        args = data
                    if func_name:
                        tool_calls.append(self._make_tool_call(str(func_name), args))
                except json.JSONDecodeError:
                    err_msg = f"{_BAD_JSON_PREFIX}{json_str[:200]}"
                    errors.append(err_msg)
                    # On parse failure, clear the protocol cache
                    self.last_tools = ""
                    self._last_tools_json = ""
                except Exception:
                    pass

            if not tool_calls:
                for e in errors:
                    tool_calls.append(self._make_tool_call("bad_json", {"msg": e}))

        return MockResponse(
            thinking=thinking,
            content=remaining_text.strip(),
            tool_calls=tool_calls,
            raw=text,
        )

    def _parse_text_tool_calls(self, content: str) -> "tuple[List[MockToolCall], str]":
        """Extract tool calls from <tool_use>...</tool_use> tags.

        Also handles fenced JSON inside tags and JSON arrays of tool_use items.
        Returns (tool_calls, remaining_text_without_tool_blocks).
        """
        tcs: List[MockToolCall] = []

        # JSON array: [{"type":"tool_use", "name":..., "input":...}]
        array_prefix = next(
            (p for p in ['[{"type":"tool_use"', '[{"type": "tool_use"'] if p in content),
            None,
        )
        if array_prefix and content.endswith("}]"):
            try:
                idx = content.index(array_prefix)
                raw = json.loads(content[idx:])
                for b in raw:
                    if b.get("type") == "tool_use":
                        name = b.get("name", "")
                        args = b.get("input", {})
                        if name:
                            tcs.append(self._make_tool_call(str(name), args))
                if tcs:
                    return tcs, content[:idx].strip()
            except Exception:
                pass

        # XML-style <tool_use>{...}</tool_use>
        for s in _TOOL_USE_TAG_RE.findall(content):
            try:
                d = _tryparse_json(s.strip())
                if d is None:
                    continue
                name = d.get("name")
                if not name:
                    continue
                args = (
                    d.get("arguments")
                    or d.get("args")
                    or d.get("input")
                    or {}
                )
                tcs.append(self._make_tool_call(str(name), args))
            except Exception:
                pass

        if tcs:
            content = _TOOL_USE_TAG_RE.sub("", content, count=0).strip()
        return tcs, content

    @staticmethod
    def _make_tool_call(name: str, args: Any) -> MockToolCall:
        """Build a MockToolCall with arguments serialized to JSON string."""
        if isinstance(args, (dict, list)):
            arg_str = json.dumps(args, ensure_ascii=False)
        elif args is None:
            arg_str = "{}"
        else:
            arg_str = str(args)
        return MockToolCall(function=MockFunction(name=name, arguments=arg_str))


def _tryparse_json(json_str: str) -> Optional[dict]:
    """Try to parse JSON, tolerating fenced code blocks."""
    try:
        return json.loads(json_str)
    except Exception:
        pass
    # Strip markdown fences and leading 'json\n'
    s = json_str.strip().strip("`")
    if s.startswith("json\n"):
        s = s[5:]
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        return None
