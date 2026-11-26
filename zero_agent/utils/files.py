"""文件操作工具函数.

consume_file: 原子读取并删除文件（用于信号文件/进程间通信）.
expand_file_refs: 解析文本中的 {{file:路径:起始行:结束行}} 引用.
display_path: 把家目录下的路径渲染成 ``~/...``（提示词/日志展示用，跨机器可读）.
"""

import os
import re
from pathlib import Path
from typing import Optional


def display_path(path: str, project_root_path: str = "") -> str:
    """Render a path portably: ``<project>/...`` first, else ``~/...``.

    仅用于展示（系统提示词、日志）。这两个前缀工具侧都能解析：文件工具与
    ``code_run`` 的路径参数接受 ``<project>`` / ``<workspace>`` / ``<home>``，
    shell 里可用 ``$ZA_PROJECT_ROOT`` / ``$ZA_WORKSPACE_DIR``。因此提示词里不需要
    出现任何机器特定的家目录。

    Args:
        path: 绝对或相对路径.
        project_root_path: 项目根目录；空串时跳过 ``<project>`` 归约.

    Returns:
        ``<project>/...``、``~/...`` 或原路径.
    """

    text = str(path or "")
    if not text:
        return ""
    root = str(project_root_path or "").rstrip(os.sep)
    if root and (text == root or text.startswith(root + os.sep)):
        relative = text[len(root):].lstrip(os.sep)
        return "<project>" + (f"/{relative}" if relative else "")
    home = os.path.expanduser("~")
    if not home or home == "~":
        return text
    if text == home:
        return "~"
    prefix = home + os.sep
    return "~" + text[len(home):] if text.startswith(prefix) else text


def project_root() -> str:
    """Return the project root (directory that holds ``config.yaml``)."""

    from zero_agent.core.config import PROJECT_ROOT

    return str(PROJECT_ROOT)


def expand_tool_path(path: str, workspace_dir: str = "") -> str:
    """Expand the portable prefixes accepted by tool path arguments.

    ``<project>`` / ``<repo>`` → 项目根目录，``<workspace>`` / ``<workdir>`` →
    workspace，``<home>`` → 当前用户家目录。未知前缀原样返回。

    Args:
        path: 工具参数里的原始路径.
        workspace_dir: 用于 ``<workspace>`` 的目录.

    Returns:
        展开后的路径（可能仍是相对路径，交由调用方继续解析）.
    """

    text = str(path or "")
    if not text.startswith("<"):
        return text
    mapping = {
        "<project>": project_root(),
        "<repo>": project_root(),
        "<workspace>": str(workspace_dir or ""),
        "<workdir>": str(workspace_dir or ""),
        "<home>": os.path.expanduser("~"),
    }
    for token, value in mapping.items():
        if not value:
            continue
        if text == token:
            return value
        if text.startswith(token + "/") or text.startswith(token + os.sep):
            return os.path.join(value, text[len(token) + 1:])
    return text


def resolve_workspace_path(path: str, workspace_dir: str) -> str:
    """Resolve a path under ``workspace_dir`` and reject traversal escapes.

    This helper is intentionally stricter than the legacy file-tool resolver:
    it is used for executable script references, where an absolute or ``..``
    path must never escape the agent workspace.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")

    root = Path(workspace_dir).expanduser().resolve()
    expanded = expand_tool_path(path, str(root))
    candidate = Path(expanded).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"path must stay inside workspace: {path} -> {resolved}"
        ) from exc
    return str(resolved)


def consume_file(directory: Optional[str], filename: str) -> Optional[str]:
    """原子读取文件内容并删除文件.

    用于进程间通信的信号文件模式：上游创建文件写入内容，
    下游 consume 读取后立刻删除，避免重复处理.

    Args:
        directory: 文件所在目录，为 None 时直接返回 None.
        filename: 文件名（不含路径）.

    Returns:
        文件内容字符串，若 directory 为 None 或文件不存在则返回 None.
    """
    if directory is None:
        return None

    filepath = os.path.join(directory, filename)
    if not os.path.exists(filepath):
        return None

    with open(filepath, encoding="utf-8", errors="replace") as f:
        content = f.read()

    os.remove(filepath)
    return content


def expand_file_refs(
    text: str,
    base_dir: Optional[str] = None,
) -> str:
    """展开文本中的文件引用标记 {{file:路径:起始行:结束行}}.

    引用格式:
        {{file:src/main.py:10:25}} → 读取 src/main.py 第 10-25 行

    支持与普通文本混排，展开失败时抛出 ValueError.
    包含路径遍历保护：解析路径必须在 base_dir 子树内.

    Args:
        text: 包含文件引用的文本.
        base_dir: 相对路径的基准目录，默认为当前工作目录.

    Returns:
        展开引用后的文本.

    Raises:
        ValueError: 引用文件不存在、行号越界或路径遍历攻击.
    """
    pattern = r"\{\{file:(.+?):(\d+):(\d+)\}\}"

    def _replacer(match: re.Match) -> str:
        path = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3))

        resolved = os.path.abspath(
            os.path.join(base_dir or ".", path)
        )

        # 路径遍历保护：解析路径必须在 base_dir 子树内
        base = os.path.abspath(base_dir or ".")
        if not resolved.startswith(base + os.sep) and resolved != base:
            raise ValueError(
                f"路径遍历攻击检测: {path} 解析为 {resolved}，"
                f"不在基准目录 {base} 内"
            )

        if not os.path.isfile(resolved):
            raise ValueError(f"引用的文件不存在: {resolved}")

        with open(resolved, encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        if start < 1 or end > total or start > end:
            raise ValueError(
                f"行号越界: {resolved} 共 {total} 行, "
                f"请求 {start}-{end}"
            )

        return "".join(lines[start - 1 : end])

    return re.sub(pattern, _replacer, text)
