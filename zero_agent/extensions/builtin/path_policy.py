"""路径规范化与作用域比较.

``permission_gate`` 用它判断写入目标是否落在受保护前缀下，``shell_policy`` 用它
判断删除目标是否越出 workspace。两处必须共用同一口径，否则会出现「判定用一套
路径、实际写用另一套」的绕过。

大小写：``os.path.normcase`` 在 POSIX（含 macOS）上是恒等函数，而 macOS 默认
文件系统大小写不敏感，因此 ``~/.ZEROAGENT/mcp.json`` 与 ``~/.zeroagent/mcp.json``
指向同一个文件。比较时按平台做 casefold，避免只改大小写就跳过保护。
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Tuple

# 大小写不敏感文件系统：macOS 默认 APFS、Windows NTFS
_CASE_INSENSITIVE_FS = sys.platform == "darwin" or os.name == "nt"


def is_case_insensitive_fs() -> bool:
    """当前平台的文件系统是否默认大小写不敏感.

    Returns:
        macOS 与 Windows 返回 True.
    """
    return _CASE_INSENSITIVE_FS


def compare_key(path: str) -> str:
    """返回用于比较的路径键.

    Args:
        path: 已规范化的绝对路径.

    Returns:
        大小写不敏感文件系统上为 casefold 结果，否则原样返回.
    """
    return path.casefold() if _CASE_INSENSITIVE_FS else path


def normalize(path: str, cwd: Optional[str]) -> str:
    """把路径规范化为可比较的绝对形式.

    依次做 expanduser → 相对转绝对 → realpath（解析软链接）.

    Args:
        path: 原始路径.
        cwd: 相对路径的基准目录.

    Returns:
        规范化后的绝对路径（保留磁盘上的大小写，便于展示给用户）.
    """
    expanded = os.path.expanduser(str(path))
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd or os.getcwd(), expanded)
    return os.path.realpath(expanded)


def compare(path: str, cwd: Optional[str]) -> str:
    """返回用于作用域比较的路径键.

    Args:
        path: 原始路径.
        cwd: 相对路径的基准目录.

    Returns:
        ``compare_key(normalize(path, cwd))``.
    """
    return compare_key(normalize(path, cwd))


def inside(path: str, root: str) -> bool:
    """判断路径是否位于根目录之下（两者都已规范化）.

    Args:
        path: 已规范化的绝对路径.
        root: 已规范化的根路径.

    Returns:
        True 表示位于根下（含根本身）.
    """
    if not root:
        return False
    try:
        return os.path.commonpath([compare_key(path), compare_key(root)]) == (
            compare_key(root)
        )
    except ValueError:
        # 不同盘符等无法比较的情况，保守视为不在根下
        return False


def under(
    path: str,
    prefixes: Tuple[str, ...],
    cwd: Optional[str],
) -> Optional[str]:
    """判断路径是否落在某个受保护前缀下.

    Args:
        path: 待检查路径.
        prefixes: 受保护前缀.
        cwd: 相对路径基准.

    Returns:
        命中的原始前缀；未命中返回 None.
    """
    for prefix in prefixes:
        if inside(normalize(path, cwd), normalize(prefix, cwd)):
            return prefix
    return None
