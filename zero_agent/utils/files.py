"""文件操作工具函数.

consume_file: 原子读取并删除文件（用于信号文件/进程间通信）.
expand_file_refs: 解析文本中的 {{file:路径:起始行:结束行}} 引用.
"""

import os
import re
from pathlib import Path
from typing import Optional


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
