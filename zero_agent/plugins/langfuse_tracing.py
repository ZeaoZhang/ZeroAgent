"""LangFuse 追踪插件 — 通过钩子系统记录 agent 全生命周期.

自动激活: 若 keychain 中包含 langfuse_config，import 时自动注册钩子。
Span 层级: Agent Trace → LLM Generation → Tool Span.

缺失 langfuse 包或配置时静默跳过，不影响 agent 正常运行。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

from zero_agent.core.hooks import (
    EVENT_AGENT_AFTER,
    EVENT_AGENT_BEFORE,
    EVENT_LLM_AFTER,
    EVENT_LLM_BEFORE,
    EVENT_TOOL_AFTER,
    EVENT_TOOL_BEFORE,
    EVENT_TURN_AFTER,
    EVENT_TURN_BEFORE,
)

# 线程本地状态，隔离并发 session
_local = threading.local()


def _get_langfuse():
    """延迟导入 langfuse，缺失时返回 None."""
    try:
        from langfuse import Langfuse
        return Langfuse
    except ImportError:
        return None


def _get_config() -> Optional[dict]:
    """从 keychain 读取 langfuse_config. 失败返回 None."""
    try:
        from zero_agent.utils.keychain import Keychain
        kc = Keychain()
        cfg = kc.langfuse_config
        if hasattr(cfg, "use"):
            import json
            return json.loads(cfg.use())
        return None
    except Exception:
        pass

    # fallback: 环境变量
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
    if pk and sk:
        return {
            "public_key": pk,
            "secret_key": sk,
            "host": host,
        }
    return None


def _ensure_client() -> Optional[Any]:
    """获取或创建线程本地的 Langfuse 客户端."""
    if hasattr(_local, "client"):
        return _local.client

    Langfuse = _get_langfuse()
    if Langfuse is None:
        return None

    cfg = _get_config()
    if cfg is None:
        return None

    _local.client = Langfuse(
        public_key=cfg.get("public_key", ""),
        secret_key=cfg.get("secret_key", ""),
        host=cfg.get("host", "https://cloud.langfuse.com"),
    )
    return _local.client


# ---- 钩子回调 ----

def _on_agent_before(ctx: dict) -> None:
    """agent 启动时创建 Trace."""
    client = _ensure_client()
    if client is None:
        return
    task = ctx.get("task", "unknown")
    _local.trace = client.trace(
        name="zero-agent-task",
        input=task[:500],
    )
    _local.trace_id = _local.trace.id
    _local.gen_spans = []
    _local.start_time = time.time()


def _on_turn_before(ctx: dict) -> None:
    """每轮开始时记录."""
    pass


def _on_llm_before(ctx: dict) -> None:
    """LLM 调用前创建 Generation span."""
    if not hasattr(_local, "trace"):
        return
    model = ctx.get("model", "unknown")
    span = _local.trace.span(
        name="llm-call",
        input={"model": model, "turn": ctx.get("turn", 0)},
    )
    span._start_time = time.time()
    _local._current_llm_span = span


def _on_llm_after(ctx: dict) -> None:
    """LLM 调用后结束 Generation span."""
    span = getattr(_local, "_current_llm_span", None)
    if span is None:
        return
    usage = ctx.get("usage", {})
    span.end(
        output={
            "stop_reason": ctx.get("stop_reason", ""),
            "tool_calls": len(ctx.get("tool_calls", [])),
        },
        usage={
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
        },
    )
    _local._current_llm_span = None


def _on_tool_before(ctx: dict) -> None:
    """工具调用前创建 Tool span."""
    if not hasattr(_local, "trace"):
        return
    tool_name = ctx.get("tool_name", "unknown")
    span = _local.trace.span(
        name=f"tool:{tool_name}",
        input={"args": str(ctx.get("args", {}))[:500]},
    )
    span._start_time = time.time()
    if not hasattr(_local, "_current_tool_spans"):
        _local._current_tool_spans = {}
    _local._current_tool_spans[tool_name] = span


def _on_tool_after(ctx: dict) -> None:
    """工具调用后结束 Tool span."""
    spans = getattr(_local, "_current_tool_spans", {})
    tool_name = ctx.get("tool_name", "unknown")
    span = spans.pop(tool_name, None)
    if span is None:
        return
    result = ctx.get("result", "")
    span.end(
        output=str(result)[:500],
    )


def _on_turn_after(ctx: dict) -> None:
    """每轮结束."""
    pass


def _on_agent_after(ctx: dict) -> None:
    """agent 结束时 flush trace."""
    if hasattr(_local, "trace"):
        duration = time.time() - getattr(_local, "start_time", time.time())
        _local.trace.update(
            output={"turns": ctx.get("turns", 0), "duration_s": duration}
        )
        if hasattr(_local, "client") and hasattr(_local.client, "flush"):
            _local.client.flush()
    # 清理线程状态
    for attr in (
        "trace", "trace_id", "gen_spans", "start_time",
        "_current_llm_span", "_current_tool_spans", "client",
    ):
        if hasattr(_local, attr):
            delattr(_local, attr)


# 钩子 → 回调映射
_HANDLERS = {
    EVENT_AGENT_BEFORE: _on_agent_before,
    EVENT_TURN_BEFORE: _on_turn_before,
    EVENT_LLM_BEFORE: _on_llm_before,
    EVENT_LLM_AFTER: _on_llm_after,
    EVENT_TOOL_BEFORE: _on_tool_before,
    EVENT_TOOL_AFTER: _on_tool_after,
    EVENT_TURN_AFTER: _on_turn_after,
    EVENT_AGENT_AFTER: _on_agent_after,
}


def register(hook_system: Any) -> bool:
    """在 HookSystem 上注册所有 LangFuse 回调.

    仅当 langfuse 包可用且配置存在时才注册。
    注册失败时静默跳过，不影响 agent 正常运行。

    Args:
        hook_system: HookSystem 实例.

    Returns:
        True 如果注册成功，False 如果跳过.
    """
    if _get_langfuse() is None:
        return False
    if _get_config() is None:
        return False

    for event, callback in _HANDLERS.items():
        hook_system.register(event, callback)

    return True
