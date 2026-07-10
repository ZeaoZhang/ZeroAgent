"""Worldline tracking plugin — rewind/checkpoint integration.

When enabled via config (enable_worldline: true) or env (ZA_ENABLE_WORLDLINE=1),
hooks into file_write and file_patch tools to track pre-edit file state,
and commits checkpoints at turn boundaries for rewind capabilities.

Must be explicitly enabled — defaults to false to prevent surprise writes.
"""

import os
from typing import Any, Optional

from zero_agent.core.hooks import EVENT_TOOL_BEFORE, EVENT_TURN_AFTER


def _is_enabled(ctx: dict) -> bool:
    """Check whether worldline tracking is enabled for this session."""
    handler = ctx.get("handler") if isinstance(ctx, dict) else None
    if handler is not None:
        parent = getattr(handler, "parent", None)
        if parent is not None:
            config = getattr(parent, "config", None)
            if config is not None:
                enabled = getattr(config, "enable_worldline", False)
                if enabled:
                    return True
    # Fallback to env
    return os.environ.get("ZA_ENABLE_WORLDLINE", "") == "1"


def _get_store(ctx: dict):
    """Get or lazily create the RewindStore for this session."""
    handler = ctx.get("handler") if isinstance(ctx, dict) else None
    if handler is None:
        return None

    store = getattr(handler, "_worldline_store", None)
    if store is not None:
        return store

    # Lazily create
    try:
        from zero_agent.frontends.worldline import RewindStore

        cwd = getattr(handler, "cwd", os.getcwd())
        parent = getattr(handler, "parent", None)
        if parent is not None:
            config = getattr(parent, "config", None)
            if config is not None:
                cwd = getattr(config, "workspace_dir", cwd)

        root = os.path.join(cwd, ".worldline")
        os.makedirs(root, exist_ok=True)
        store = RewindStore(root, cwd)
        handler._worldline_store = store
        return store
    except ImportError:
        return None


def _resolve_file_path(args: dict, ctx: dict) -> Optional[str]:
    """Resolve a file path from tool args using workspace-aware semantics."""
    path = args.get("path") or args.get("file_path") or args.get("filename")
    if not path:
        return None

    handler = ctx.get("handler") if isinstance(ctx, dict) else None
    if handler is not None:
        cwd = getattr(handler, "cwd", os.getcwd())
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
    return os.path.abspath(path)


def _on_tool_before(ctx: dict) -> None:
    """Hook: track pre-edit file state before file_write/file_patch."""
    if not _is_enabled(ctx):
        return

    tool_name = ctx.get("tool_name", "")
    if tool_name not in ("file_write", "file_patch"):
        return

    store = _get_store(ctx)
    if store is None:
        return

    args = ctx.get("args", {})
    abs_path = _resolve_file_path(args, ctx)
    if abs_path and os.path.isfile(abs_path):
        try:
            store.track_pre_edit(abs_path)
        except Exception:
            pass


def _on_turn_after(ctx: dict) -> None:
    """Hook: commit a checkpoint after each turn."""
    if not _is_enabled(ctx):
        return

    store = _get_store(ctx)
    if store is None:
        return

    handler = ctx.get("handler") if isinstance(ctx, dict) else None
    if handler is None:
        return

    # Extract title/summary from handler context
    summary = ""
    key_info = handler.working.get("key_info", "") if hasattr(handler, "working") else ""
    if key_info:
        summary = str(key_info)[:200]

    history_info = getattr(handler, "history_info", []) or []
    history_len = len(getattr(handler, "client", {}).history if hasattr(handler, "client") and handler.client else [])

    try:
        store.commit(
            summary=summary,
            history_len=history_len,
            history_info=[str(h)[:100] for h in history_info[-5:]],
            key_info=str(key_info)[:500],
        )
    except Exception:
        pass


def register(hook_system: Any) -> bool:
    """Register worldline tracking hooks.

    Args:
        hook_system: HookSystem instance.

    Returns:
        True (always — the hooks no-op when worldline is disabled).
    """
    hook_system.register(EVENT_TOOL_BEFORE, _on_tool_before)
    hook_system.register(EVENT_TURN_AFTER, _on_turn_after)
    return True
