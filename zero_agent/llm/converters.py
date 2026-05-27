"""Message format converters — Claude ↔ OpenAI 消息格式互转.

消息格式转换器:
  msgs_claude_to_openai: Claude content blocks → OpenAI chat format
  to_responses_input:    OpenAI chat → Responses API format
  openai_tools_to_claude: OpenAI tool schema → Claude format
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List


def to_responses_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI chat messages to Responses API input format.

    将标准 OpenAI chat messages 转为 Responses API 的 input 数组格式。
    system → developer role, tool → function_call_output,
    assistant tool_calls → function_call items.

    Args:
        messages: OpenAI chat format 消息列表.

    Returns:
        Responses API input 格式的消息列表.
    """
    result: List[Dict[str, Any]] = []
    pending: List[str] = []

    for msg in messages:
        role = str(msg.get("role", "user")).lower()
        if role == "tool":
            cid = msg.get("tool_call_id") or (
                pending.pop(0) if pending else f"call_{uuid.uuid4().hex[:8]}"
            )
            result.append({
                "type": "function_call_output",
                "call_id": cid,
                "output": msg.get("content", ""),
            })
            continue
        if role not in ("user", "assistant", "system", "developer"):
            role = "user"
        if role == "system":
            role = "developer"

        content = msg.get("content", "")
        text_type = "output_text" if role == "assistant" else "input_text"
        parts: List[Dict[str, Any]] = []

        if isinstance(content, str):
            if content:
                parts.append({"type": text_type, "text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text = part.get("text", "")
                    if text:
                        parts.append({"type": text_type, "text": text})
                elif ptype == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if url and role != "assistant":
                        parts.append({"type": "input_image", "image_url": url})

        if not parts:
            fallback = str(content) if not isinstance(content, list) else "[empty]"
            parts = [{"type": text_type, "text": fallback}]

        result.append({"role": role, "content": parts})
        pending = []

        for tc in msg.get("tool_calls") or []:
            f = tc.get("function", {})
            cid = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
            pending.append(cid)
            result.append({
                "type": "function_call",
                "call_id": cid,
                "name": f.get("name", ""),
                "arguments": f.get("arguments", ""),
            })

    return result


def msgs_claude_to_openai(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert Claude content-block messages to OpenAI chat format.

    将 Claude content block 格式消息转为 OpenAI chat format。
    tool_result block → tool role message,
    tool_use block → assistant tool_calls,
    thinking block → reasoning_content。

    Args:
        messages: Claude content-block 格式消息列表.

    Returns:
        OpenAI chat format 消息列表.
    """
    result: List[Dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        blocks = (
            content
            if isinstance(content, list)
            else [{"type": "text", "text": str(content)}]
        )

        if role == "assistant":
            text_parts: List[Dict[str, Any]] = []
            tool_calls: List[Dict[str, Any]] = []
            reasoning = ""

            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "thinking" and b.get("thinking"):
                    reasoning = b["thinking"]
                elif b.get("type") == "text" and b.get("text"):
                    text_parts.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": json.dumps(
                                b.get("input", {}), ensure_ascii=False
                            ),
                        },
                    })

            m: Dict[str, Any] = {"role": "assistant"}
            if reasoning:
                m["reasoning_content"] = reasoning
            if text_parts:
                m["content"] = text_parts
            elif not tool_calls:
                m["content"] = "."
            if tool_calls:
                m["tool_calls"] = tool_calls
            result.append(m)

        elif role == "user":
            text_parts = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    if text_parts:
                        result.append({"role": "user", "content": text_parts})
                        text_parts = []
                    tr = b.get("content", "")
                    if isinstance(tr, list):
                        tr = "\n".join(
                            x.get("text", "")
                            for x in tr
                            if isinstance(x, dict) and x.get("type") == "text"
                        )
                    result.append({
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id") or "",
                        "content": tr if isinstance(tr, str) else str(tr),
                    })
                elif b.get("type") == "image":
                    src = b.get("source") or {}
                    if src.get("type") == "base64" and src.get("data"):
                        text_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    f"data:{src.get('media_type', 'image/png')}"
                                    f";base64,{src.get('data', '')}"
                                ),
                            },
                        })
                elif b.get("type") == "image_url":
                    text_parts.append(b)
                elif b.get("type") == "text" and b.get("text"):
                    text_parts.append({"type": "text", "text": b.get("text", "")})
            if text_parts:
                result.append({"role": "user", "content": text_parts})
        else:
            result.append(msg)

    return result


def openai_tools_to_claude(
    tools: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert OpenAI function-calling tool schema to Claude format.

    [{type:'function', function:{name,description,parameters}}]
    → [{name,description,input_schema}]

    已是 Claude 格式的工具直接透传。

    Args:
        tools: OpenAI tool schema 列表.

    Returns:
        Claude tool schema 列表.
    """
    result: List[Dict[str, Any]] = []
    for t in tools:
        if "input_schema" in t:
            result.append(t)
            continue
        fn = t.get("function", t)
        result.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get(
                "parameters", {"type": "object", "properties": {}}
            ),
        })
    return result
