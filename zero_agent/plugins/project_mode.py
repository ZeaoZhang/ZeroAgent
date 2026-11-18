"""Project Mode plugin — cross-session project context persistence.

Mechanism: registers an ``on_llm_request`` transform that appends L1 project
context (rule + memory file pointer + closing discipline) to the last user
message when a project is active. The transform runs on a deep-copied request,
so the injection never enters session history.

Anchor file: <workspace_dir>/projects/.active_project.<pid>
  - PID keying: each agent process activates its own project
  - Auto-deactivation when the agent exits
  - On startup, cleans stale anchors from previous runs (own pid only)
"""

import os
from typing import Any, Optional


def _workspace_dir_from_ctx(ctx: dict) -> str:
    """Extract workspace_dir from hook context, with reasonable fallback."""
    handler = ctx.get("handler") if isinstance(ctx, dict) else None
    if handler is not None:
        parent = getattr(handler, "parent", None)
        if parent is not None:
            config = getattr(parent, "config", None)
            if config is not None:
                ws = getattr(config, "workspace_dir", None)
                if ws:
                    return ws
    # Fallback: current working directory
    return os.getcwd()


def _projects_dir(ctx: dict) -> str:
    """Projects directory under workspace."""
    return os.path.join(_workspace_dir_from_ctx(ctx), "projects")


def _anchor_path(ctx: dict) -> str:
    """Per-process anchor file path."""
    return os.path.join(_projects_dir(ctx), f".active_project.{os.getpid()}")


def _cleanup_stale_anchors(ctx: dict):
    """Clean anchors from previous runs with the same pid (pid reuse).

    Only touches anchors for own pid — never touches other processes' anchors.
    """
    import glob

    proj_dir = _projects_dir(ctx)
    if not os.path.isdir(proj_dir):
        return
    my_anchor = _anchor_path(ctx)
    for path in glob.glob(os.path.join(proj_dir, ".active_project*")):
        if path == my_anchor:
            continue
        pid = path.rsplit(".", 1)[-1]
        if pid.isdigit() and int(pid) == os.getpid():
            try:
                os.remove(path)
            except OSError:
                pass


def _active_project(ctx: dict) -> Optional[str]:
    """Return the currently active project name, or None."""
    # Check per-agent override first
    handler = ctx.get("handler") if isinstance(ctx, dict) else None
    if handler is not None:
        parent = getattr(handler, "parent", None)
        if parent is not None and hasattr(parent, "_za_project_mode_name"):
            val = getattr(parent, "_za_project_mode_name", None)
            return val or None

    anchor = _anchor_path(ctx)
    if not os.path.isfile(anchor):
        return None
    try:
        return open(anchor, encoding="utf-8").read().strip() or None
    except Exception:
        return None


def _project_dir(ctx: dict, name: str) -> str:
    return os.path.join(_projects_dir(ctx), name)


def _mem_path(ctx: dict, name: str) -> str:
    return os.path.join(_project_dir(ctx, name), "project_memory.md")


def _memory_stat(ctx: dict, name: str) -> "tuple[bool, int, int]":
    """Return (exists, lines, bytes) for project_memory.md."""
    path = _mem_path(ctx, name)
    if os.path.isfile(path):
        try:
            data = open(path, encoding="utf-8").read()
            return True, len(data.splitlines()), len(data.encode("utf-8"))
        except Exception:
            pass
    return False, 0, 0


def _build_injection(ctx, name: str) -> str:
    """Build L1 injection text (rules + memory pointer + closing discipline).

    L2 (project_memory.md full text) is NOT injected — the model decides
    whether to read it via file tools based on the pointer.
    """
    pdir = _project_dir(ctx, name)
    mem_path_str = _mem_path(ctx, name)
    exists, lines, nbytes = _memory_stat(ctx, name)

    if exists and nbytes > 0:
        mem_hint = (
            f"项目全量记忆在 {mem_path_str}（{lines} 行，{nbytes} 字节）。"
            f"任务涉及项目上下文时用 file 工具自行读取，无关则不读。"
        )
    else:
        mem_hint = f"项目记忆文件尚未创建：{mem_path_str}"

    return (
        f"\n\n[Project Mode] 当前项目：{name}\n"
        f"项目目录：{pdir}\n"
        f"{mem_hint}\n\n"
        f"项目期间纪律：\n"
        f"1. 所有产物放入项目目录，禁止丢 workspace 根目录\n"
        f"2. 每得到一条信息，自问若记忆归零是否需重复付出认知代价——"
        f"是则追加进 project_memory.md（一条一句，增量更新，不整篇重写）\n"
        f"3. 离开项目模式时提醒用户：删除 .active_project.<pid> 锚文件即可\n"
    )


def _hook_context(ctx: Any) -> dict:
    """把扩展上下文适配成现有项目模式辅助函数需要的字典."""
    agent = getattr(getattr(ctx, "runtime", None), "agent", None)
    return {"handler": getattr(agent, "handler", None)}


def _inject_project_context(request: Any, ctx: dict) -> Any:
    """活跃项目时把项目上下文追加到最后一条 user 消息.

    Args:
        request: LlmRequest（session 已深拷贝）.
        ctx: 含 handler 的辅助上下文字典.

    Returns:
        变换后的 LlmRequest；无活跃项目时原样返回.
    """
    name = _active_project(ctx)
    if not name:
        return request

    # Ensure projects dir exists and clean stale anchors
    os.makedirs(_projects_dir(ctx), exist_ok=True)
    _cleanup_stale_anchors(ctx)

    injection = _build_injection(ctx, name)

    messages = request.messages
    if not messages or not isinstance(messages, list):
        return request

    # 只改最后一条 user 消息；工具结果轮次的尾部可能是 role=tool，
    # 往那里追加文本会生成非法消息序列。
    last_index = None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            last_index = index
            break
    if last_index is None:
        return request

    last_msg = messages[last_index]
    content = last_msg.get("content")
    if content is None:
        return request

    updated = dict(last_msg)
    if isinstance(content, str):
        updated["content"] = content + injection
    elif isinstance(content, list):
        # Multimodal: append a text block
        updated["content"] = [*content, {"type": "text", "text": injection}]
    else:
        return request

    updated_messages = list(messages)
    updated_messages[last_index] = updated
    return request.replace(messages=updated_messages)


def setup(ctx: Any) -> None:
    """安装 project_mode 扩展.

    Args:
        ctx: 插件上下文（ExtensionContext）.
    """

    @ctx.on_llm_request
    def inject(request: Any) -> Any:
        """在请求副本上注入项目上下文."""
        return _inject_project_context(request, _hook_context(ctx))
