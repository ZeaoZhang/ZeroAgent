"""SSE stream parsers — 解析 Anthropic Messages API 和 OpenAI API 的流式响应.

Yields text chunks for real-time display, returns aggregated content_block list.

content_block 格式:
  {"type": "text", "text": str}
  {"type": "thinking", "thinking": str, "signature": str}
  {"type": "tool_use", "id": str, "name": str, "input": dict}
"""

from __future__ import annotations

import json
import re
from typing import Any, Generator, Iterator, List, Optional


def parse_claude_sse(
    resp_lines: Iterator[bytes],
) -> Generator[str, None, List[dict]]:
    """Parse Anthropic Messages API SSE stream.

    解析 message_start / content_block_start / delta / stop 事件流。
    流中断时自动插入警告标记。

    Yields:
        text chunks 供 UI 实时展示.

    Returns:
        list[content_block] 聚合后的内容块列表.
    """
    content_blocks: List[dict] = []
    current_block: Optional[dict] = None
    tool_json_buf = ""
    stop_reason: Optional[str] = None
    got_message_stop = False
    warn: Optional[str] = None

    for line in resp_lines:
        if not line:
            continue
        line = line.decode("utf-8") if isinstance(line, bytes) else line
        if not line.startswith("data:"):
            continue
        data_str = line[5:].lstrip()
        if data_str == "[DONE]":
            break
        try:
            evt: dict = json.loads(data_str)
        except Exception:
            continue

        evt_type = evt.get("type", "")

        if evt_type == "message_start":
            pass  # usage handled by caller via _record_usage
        elif evt_type == "content_block_start":
            block = evt.get("content_block", {})
            if block.get("type") == "text":
                current_block = {"type": "text", "text": ""}
            elif block.get("type") == "thinking":
                current_block = {
                    "type": "thinking",
                    "thinking": "",
                    "signature": "",
                }
            elif block.get("type") == "tool_use":
                current_block = {
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": {},
                }
                tool_json_buf = ""
        elif evt_type == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if current_block and current_block.get("type") == "text":
                    current_block["text"] += text
                if text:
                    yield text
            elif delta.get("type") == "thinking_delta":
                if current_block and current_block.get("type") == "thinking":
                    current_block["thinking"] += delta.get("thinking", "")
            elif delta.get("type") == "signature_delta":
                if current_block and current_block.get("type") == "thinking":
                    current_block["signature"] = (
                        current_block.get("signature", "")
                        + delta.get("signature", "")
                    )
            elif delta.get("type") == "input_json_delta":
                tool_json_buf += delta.get("partial_json", "")
        elif evt_type == "content_block_stop":
            if current_block:
                if current_block["type"] == "tool_use":
                    try:
                        current_block["input"] = (
                            json.loads(tool_json_buf) if tool_json_buf else {}
                        )
                    except Exception:
                        current_block["input"] = {"_raw": tool_json_buf}
                content_blocks.append(current_block)
                current_block = None
        elif evt_type == "message_delta":
            delta = evt.get("delta", {})
            stop_reason = delta.get("stop_reason", stop_reason)
        elif evt_type == "message_stop":
            got_message_stop = True
        elif evt_type == "error":
            err = evt.get("error", {})
            emsg = (
                err.get("message", str(err))
                if isinstance(err, dict)
                else str(err)
            )
            warn = f"\n\n!!!Error: SSE {emsg}"
            break

    # 流中断检测
    if not warn:
        if not got_message_stop and not stop_reason:
            warn = "\n\n[!!! 流异常中断，未收到完整响应 !!!]"
        elif stop_reason == "max_tokens":
            warn = "\n\n[!!! Response truncated: max_tokens !!!]"

    # 处理未关闭的 current_block（流中断场景）
    if current_block:
        if current_block["type"] == "tool_use":
            try:
                current_block["input"] = (
                    json.loads(tool_json_buf) if tool_json_buf else {}
                )
            except Exception:
                current_block["input"] = {"_raw": tool_json_buf}
        content_blocks.append(current_block)
        current_block = None

    if warn:
        content_blocks.append({"type": "text", "text": warn})
        yield warn

    return content_blocks


def parse_openai_sse(
    resp_lines: Iterator[bytes],
    api_mode: str = "chat_completions",
) -> Generator[str, None, List[dict]]:
    """Parse OpenAI SSE stream.

    同时支持 chat_completions 和 responses 两种 API 模式。
    chat_completions: 解析 choices[0].delta (content, tool_calls, reasoning_content).
    responses: 解析 output_text.delta, function_call 事件.

    Args:
        resp_lines: SSE 响应行迭代器.
        api_mode: "chat_completions" 或 "responses".

    Yields:
        text chunks 供 UI 实时展示.

    Returns:
        list[content_block] 聚合后的内容块列表.
    """
    content_text = ""

    if api_mode == "responses":
        return _parse_openai_responses_sse(resp_lines, content_text)
    else:
        return _parse_openai_chat_sse(resp_lines, content_text)


# ---- internal helpers ----

def _try_parse_tool_args(raw: str) -> List[dict]:
    """解析工具参数 JSON，支持粘连 JSON 对象 {..}{..}.

    Args:
        raw: 原始参数字符串.

    Returns:
        解析后的 dict 列表.
    """
    if not raw:
        return [{}]
    try:
        return [json.loads(raw)]
    except Exception:
        pass
    parts = re.split(r"(?<=\})(?=\{)", raw)
    if len(parts) > 1:
        parsed: List[dict] = []
        for p in parts:
            try:
                parsed.append(json.loads(p))
            except Exception:
                return [{"_raw": raw}]
        return parsed
    return [{"_raw": raw}]


def _parse_openai_responses_sse(
    resp_lines: Iterator[bytes],
    content_text: str,
) -> Generator[str, None, List[dict]]:
    """Parse OpenAI Responses API SSE stream."""
    seen_delta = False
    fc_buf: dict = {}
    current_fc_idx: Optional[int] = None

    for line in resp_lines:
        if not line:
            continue
        line = (
            line.decode("utf-8", errors="replace")
            if isinstance(line, bytes)
            else line
        )
        if not line.startswith("data:"):
            continue
        data_str = line[5:].lstrip()
        if data_str == "[DONE]":
            break
        try:
            evt: dict = json.loads(data_str)
        except Exception:
            continue

        etype = evt.get("type", "")

        if etype == "response.output_text.delta":
            delta = evt.get("delta", "")
            if delta:
                seen_delta = True
                content_text += delta
                yield delta
        elif etype == "response.output_text.done" and not seen_delta:
            text = evt.get("text", "")
            if text:
                content_text += text
                yield text
        elif etype == "response.output_item.added":
            item = evt.get("item", {})
            if item.get("type") == "function_call":
                idx: int = evt.get("output_index", 0)
                fc_buf[idx] = {
                    "id": item.get("call_id", item.get("id", "")),
                    "name": item.get("name", ""),
                    "args": "",
                }
                current_fc_idx = idx
        elif etype == "response.function_call_arguments.delta":
            idx = evt.get("output_index", current_fc_idx or 0)
            if idx in fc_buf:
                fc_buf[idx]["args"] += evt.get("delta", "")
        elif etype == "response.function_call_arguments.done":
            idx = evt.get("output_index", current_fc_idx or 0)
            if idx in fc_buf:
                fc_buf[idx]["args"] = evt.get(
                    "arguments", fc_buf[idx]["args"]
                )
        elif etype == "error":
            err = evt.get("error", {})
            emsg = (
                err.get("message", str(err))
                if isinstance(err, dict)
                else str(err)
            )
            if emsg:
                content_text += f"!!!Error: {emsg}"
                yield f"!!!Error: {emsg}"
            break
        elif etype == "response.completed":
            break

    return _build_content_blocks(content_text, fc_buf)


def _parse_openai_chat_sse(
    resp_lines: Iterator[bytes],
    content_text: str,
) -> Generator[str, None, List[dict]]:
    """Parse OpenAI chat_completions SSE stream."""
    tc_buf: dict = {}  # index -> {id, name, args}
    reasoning_text = ""

    for line in resp_lines:
        if not line:
            continue
        line = (
            line.decode("utf-8", errors="replace")
            if isinstance(line, bytes)
            else line
        )
        if not line.startswith("data:"):
            continue
        data_str = line[5:].lstrip()
        if data_str == "[DONE]":
            break
        try:
            evt: dict = json.loads(data_str)
        except Exception:
            continue

        ch: dict = (evt.get("choices") or [{}])[0]
        delta: dict = ch.get("delta") or {}

        if delta.get("reasoning_content"):
            reasoning_text += delta["reasoning_content"]
        if delta.get("content"):
            text: str = delta["content"]
            content_text += text
            yield text
        for tc in delta.get("tool_calls") or []:
            idx: int = tc.get("index", 0)
            has_name = bool(tc.get("function", {}).get("name"))
            if idx not in tc_buf:
                if has_name or not tc_buf:
                    tc_buf[idx] = {
                        "id": tc.get("id") or "",
                        "name": "",
                        "args": "",
                    }
                else:
                    idx = max(tc_buf)
            if has_name:
                tc_buf[idx]["name"] = tc["function"]["name"]
            if tc.get("function", {}).get("arguments"):
                tc_buf[idx]["args"] += tc["function"]["arguments"]
            if tc.get("id") and not tc_buf[idx]["id"]:
                tc_buf[idx]["id"] = tc["id"]

    blocks: List[dict] = []
    if reasoning_text:
        blocks.append({"type": "thinking", "thinking": reasoning_text})
    if content_text:
        blocks.append({"type": "text", "text": content_text})
    for idx in sorted(tc_buf):
        tc = tc_buf[idx]
        inps = _try_parse_tool_args(tc["args"])
        for i, inp in enumerate(inps):
            bid: str = tc["id"] or ""
            if len(inps) > 1:
                bid = f"{bid}_{i}" if bid else f"split_{i}"
            blocks.append({
                "type": "tool_use",
                "id": bid,
                "name": tc["name"],
                "input": inp,
            })

    return blocks


def _build_content_blocks(
    content_text: str,
    fc_buf: dict,
) -> List[dict]:
    """Build content_block list from parsed Responses API data.

    Args:
        content_text: 累积的文本内容.
        fc_buf: function_call buffer (index → {id, name, args}).

    Returns:
        content_block 列表.
    """
    blocks: List[dict] = []
    if content_text:
        blocks.append({"type": "text", "text": content_text})
    for idx in sorted(fc_buf):
        fc = fc_buf[idx]
        inps = _try_parse_tool_args(fc["args"])
        for i, inp in enumerate(inps):
            bid: str = fc["id"] or ""
            if len(inps) > 1:
                bid = f"{bid}_{i}" if bid else f"split_{i}"
            blocks.append({
                "type": "tool_use",
                "id": bid,
                "name": fc["name"],
                "input": inp,
            })
    return blocks
