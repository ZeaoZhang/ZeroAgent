"""Source validation and immutable observations for memory evaluation."""

from __future__ import annotations

import copy
import json
from typing import Any


def historical_tool_evidence(history: dict[str, Any]) -> list[dict[str, Any]]:
    """Return linked, explicitly successful tool results, never prose claims."""
    calls: dict[str, str] = {}
    seen_results: set[str] = set()
    verified: list[dict[str, Any]] = []
    for message in history["messages"]:
        if not isinstance(message, dict) or message.get("role") not in {
            "user", "assistant", "tool", "system"
        }:
            raise ValueError("History messages must have a supported role")
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ValueError("Historical tool calls must contain a function object")
            call_id = call.get("id")
            function = call.get("function") or {}
            if message["role"] != "assistant" or not isinstance(call_id, str) or not call_id or not function.get("name"):
                raise ValueError("Historical tool calls need an assistant, id and function name")
            if call_id in calls:
                raise ValueError(f"Duplicate historical tool call: {call_id}")
            calls[call_id] = function["name"]
        if message["role"] != "tool":
            continue
        call_id = message.get("tool_call_id")
        if call_id not in calls or call_id in seen_results:
            raise ValueError(f"Unmatched or duplicate historical tool result: {call_id}")
        seen_results.add(call_id)
        content = message.get("content", "")
        try:
            result = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            result = None
        if isinstance(result, dict):
            exit_code = result.get("exit_code")
            clean_exit = type(exit_code) is int and exit_code == 0
            success = (
                (result.get("status") == "success" or clean_exit)
                and result.get("status") in {None, "success"}
                and ("exit_code" not in result or clean_exit)
            )
        else:
            success = isinstance(content, str) and content.startswith("[FILE]")
        if success:
            verified.append({"tool_call_id": call_id, "tool_name": calls[call_id], "content": content})
    return verified


def trace_context(event: str, context: dict[str, Any]) -> dict[str, Any]:
    """Capture loop evidence without serializing clients, keys or raw headers."""
    record = {"event": event}
    for key in ("turn", "model", "messages", "system_prompt", "tool_calls", "tool_results", "usage"):
        if key in context:
            record[key] = copy.deepcopy(context[key])
    response = context.get("response")
    if response is not None:
        record["response"] = {
            "content": getattr(response, "content", ""),
            "stop_reason": getattr(response, "stop_reason", None),
            "stream_interrupted": getattr(response, "stream_interrupted", False),
        }
    terminal = context.get("terminal")
    if terminal is not None:
        record["terminal"] = terminal.to_dict()
    return record


def observed_tool_results(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select observed read/execute outputs, excluding write arguments and LLM text."""
    results: list[dict[str, Any]] = []
    for event in trace:
        if event.get("event") != "turn_after":
            continue
        calls = {call.get("id"): call for call in event.get("tool_calls", [])}
        for result in event.get("tool_results", []):
            call = calls.get(result.get("tool_use_id"), {})
            if call.get("tool_name") not in {"file_read", "code_run", "shell", "web_scan", "web_search"}:
                continue
            results.append({
                "turn": event.get("turn"),
                "tool_name": call["tool_name"],
                "args": call.get("args", {}),
                "content": result.get("content", ""),
            })
    return results
