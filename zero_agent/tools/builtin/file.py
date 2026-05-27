"""文件操作工具.

file_read: 读取文件内容，支持行号范围、关键词搜索、行号显示.
file_write: 创建/覆盖/追加文件内容，支持 {{file:...}} 引用展开.
file_patch: 精准替换文件中的唯一文本块.
"""

from __future__ import annotations

import collections
import difflib
import itertools
import os
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from zero_agent.core.config import AgentConfig
from zero_agent.tools.registry import ToolRegistry
from zero_agent.utils.files import expand_file_refs
from zero_agent.utils.text import smart_format


def _t(zh: str, en: str, lang: str) -> str:
    """根据语言选择中文或英文文本."""
    return zh if lang == "zh" else en


# 已读取过的目录集合，用于 file_read 的文件推荐
_read_dirs: set = set()


def file_read(
    path: str,
    start: int = 1,
    keyword: Optional[str] = None,
    count: int = 200,
    show_linenos: bool = True,
) -> str:
    """读取文件内容，支持行范围、关键词搜索和行号显示.

    Args:
        path: 文件路径.
        start: 起始行号（从 1 开始）.
        keyword: 可选关键词，返回第一个匹配行附近的内容.
        count: 最大返回行数.
        show_linenos: 是否显示行号前缀.

    Returns:
        格式化的文件内容字符串，出错时返回以 "Error:" 开头的描述.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            stream = ((i, line.rstrip("\r\n")) for i, line in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < start, stream)

            if keyword:
                before: collections.deque = collections.deque(maxlen=count // 3)
                for i, line in stream:
                    if keyword.lower() in line.lower():
                        res = list(before) + [(i, line)] + list(
                            itertools.islice(stream, count - len(before) - 1)
                        )
                        break
                    before.append((i, line))
                else:
                    return (
                        f"Keyword '{keyword}' not found after line {start}. "
                        f"Falling back to content from line {start}:\n\n"
                        + file_read(path, start, None, count, show_linenos)
                    )
            else:
                res = list(itertools.islice(stream, count))

            realcnt = len(res)
            L_MAX = min(max(100, 256000 // max(realcnt, 1)), 8000)
            TAG = " ... [TRUNCATED]"

            remaining = sum(1 for _ in itertools.islice(stream, 5000))
            total_lines = (res[0][0] - 1 if res else start - 1) + realcnt + remaining
            tl_str = f"{total_lines}+" if remaining >= 5000 else str(total_lines)
            partial = total_lines > realcnt

            total_tag = (
                f"[FILE] {tl_str} lines"
                + (f" | PARTIAL showing {realcnt}; assess need for more" if partial else "")
                + "\n"
            )

            res = [(i, line if len(line) <= L_MAX else line[:L_MAX] + TAG) for i, line in res]
            result = "\n".join(f"{i}|{line}" if show_linenos else line for i, line in res)

            if show_linenos:
                result = total_tag + result
            elif partial:
                result += f"\n\n[FILE PARTIAL: showing {realcnt}/{tl_str} lines; assess need for more]"

            _read_dirs.add(os.path.dirname(os.path.abspath(path)))
            return result

    except FileNotFoundError:
        msg = f"Error: File not found: {path}"
        try:
            tgt = os.path.basename(path)
            scan = os.path.dirname(os.path.dirname(os.path.abspath(path)))
            roots = [scan] + [d for d in _read_dirs if not d.startswith(scan)]
            cands = list(
                itertools.islice(
                    (c for base in roots for c in _scan_files(base)), 2000
                )
            )
            top = sorted(
                [
                    (difflib.SequenceMatcher(None, tgt.lower(), c[0].lower()).ratio(), c)
                    for c in cands[:2000]
                ],
                key=lambda x: -x[0],
            )[:5]
            top = [(s, c) for s, c in top if s > 0.3]
            if top:
                msg += "\n\nDid you mean:\n" + "\n".join(
                    f"  {c[1]}  ({s:.0%})" for s, c in top
                )
        except Exception:
            pass
        return msg
    except Exception as e:
        return f"Error: {str(e)}"


def _scan_files(base: str, depth: int = 2):
    """递归扫描目录下的文件，用于 file_read 的文件推荐.

    Args:
        base: 起始目录.
        depth: 递归深度.

    Yields:
        (文件名, 完整路径) 元组.
    """
    try:
        for entry in os.scandir(base):
            if entry.is_file():
                yield (entry.name, entry.path)
            elif depth > 0 and entry.is_dir(follow_symlinks=False):
                yield from _scan_files(entry.path, depth - 1)
    except (PermissionError, OSError):
        pass


def file_patch(path: str, old_content: str, new_content: str) -> dict:
    """在文件中精准替换唯一的文本块.

    Args:
        path: 文件路径.
        old_content: 待替换的旧文本块，必须在文件中唯一匹配.
        new_content: 替换后的新文本块.

    Returns:
        {"status": "success"|"error", "msg": str}
    """
    path = str(Path(path).resolve())
    try:
        if not os.path.exists(path):
            return {"status": "error", "msg": "文件不存在"}
        with open(path, "r", encoding="utf-8") as f:
            full_text = f.read()
        if not old_content:
            return {"status": "error", "msg": "old_content 为空，请确认 arguments"}
        count = full_text.count(old_content)
        if count == 0:
            return {
                "status": "error",
                "msg": (
                    "未找到匹配的旧文本块，建议：先用 file_read 确认当前内容，"
                    "再分小段进行 patch。若多次失败则询问用户，"
                    "严禁自行使用 overwrite 或代码替换。"
                ),
            }
        if count > 1:
            return {
                "status": "error",
                "msg": (
                    f"找到 {count} 处匹配，无法确定唯一位置。"
                    "请提供更长、更具体的旧文本块以确保唯一性。"
                    "建议：包含上下文行来增强特征，或分小段逐个修改。"
                ),
            }
        updated_text = full_text.replace(old_content, new_content)
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated_text)
        return {"status": "success", "msg": "文件局部修改成功"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def register_file_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """注册文件操作工具到 ToolRegistry.

    Args:
        registry: 工具注册中心.
        config: Agent 配置.
    """
    from zero_agent.tools.registry import ToolDefinition

    lang = config.resolved_tool_language

    registry.register(ToolDefinition(
        name="file_read",
        description=_t(
            "读取文件内容。支持从指定行开始读取、关键词搜索（忽略大小写，"
            "返回第一个匹配行附近内容）、行号显示。"
            "适用于查看代码、日志、配置文件等文本文件。",
            "Read file contents. Supports reading from a specific line, "
            "keyword search (case-insensitive, returns content around "
            "the first match), and line number display. "
            "Suitable for viewing code, logs, config files, and other text files.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": _t(
                        "文件路径（相对或绝对）",
                        "File path (relative or absolute)",
                        lang,
                    ),
                },
                "start": {
                    "type": "integer",
                    "description": _t(
                        "起始行号，默认 1",
                        "Starting line number, default 1",
                        lang,
                    ),
                },
                "keyword": {
                    "type": "string",
                    "description": _t(
                        "搜索关键词（忽略大小写），返回匹配行附近内容",
                        "Search keyword (case-insensitive), returns content around matching line",
                        lang,
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": _t(
                        "最大返回行数，默认 200",
                        "Maximum lines to return, default 200",
                        lang,
                    ),
                },
                "show_linenos": {
                    "type": "boolean",
                    "description": _t(
                        "是否显示行号前缀，默认 true",
                        "Whether to show line number prefixes, default true",
                        lang,
                    ),
                },
            },
            "required": ["path"],
        },
        handler=_make_file_read_handler(config),
        category="file",
    ))

    registry.register(ToolDefinition(
        name="file_write",
        description=_t(
            "创建或覆盖写入文件。适用于创建新文件或整体重写。"
            "精细的局部修改请使用 file_patch。"
            "支持 overwrite / append / prepend 三种写入模式。",
            "Create or overwrite a file. Suitable for new files or "
            "full rewrites. For precise local modifications, use file_patch. "
            "Supports overwrite / append / prepend write modes.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": _t(
                        "文件路径（相对或绝对）",
                        "File path (relative or absolute)",
                        lang,
                    ),
                },
                "content": {
                    "type": "string",
                    "description": _t(
                        "要写入的文件内容",
                        "File content to write",
                        lang,
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append", "prepend"],
                    "description": _t(
                        "写入模式: overwrite(覆盖,默认) / append(追加) / prepend(前置)",
                        "Write mode: overwrite(default) / append / prepend",
                        lang,
                    ),
                },
            },
            "required": ["path", "content"],
        },
        handler=_make_file_write_handler(config),
        category="file",
    ))

    registry.register(ToolDefinition(
        name="file_patch",
        description=_t(
            "在文件中精准替换唯一的文本块。"
            "old_content 必须在文件中恰好出现一次，否则操作失败。"
            "适用于小幅局部修改，不适合大范围重写。",
            "Precisely replace a unique text block in a file. "
            "old_content must appear exactly once in the file, "
            "otherwise the operation fails. "
            "Suitable for small local modifications, not for large rewrites.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": _t(
                        "文件路径（相对或绝对）",
                        "File path (relative or absolute)",
                        lang,
                    ),
                },
                "old_content": {
                    "type": "string",
                    "description": _t(
                        "待替换的旧文本块，必须在文件中唯一匹配",
                        "Old text block to replace; must match uniquely in the file",
                        lang,
                    ),
                },
                "new_content": {
                    "type": "string",
                    "description": _t(
                        "替换后的新文本块",
                        "New text block to replace with",
                        lang,
                    ),
                },
            },
            "required": ["path", "old_content", "new_content"],
        },
        handler=_make_file_patch_handler(config),
        category="file",
    ))


def _resolve_path(path: str, config: AgentConfig) -> str:
    """将相对路径解析为绝对路径.

    Args:
        path: 原始路径.
        config: Agent 配置.

    Returns:
        解析后的绝对路径。若 path 已是绝对路径则直接返回.
    """
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(config.workspace_dir, path))


def _make_file_read_handler(config: AgentConfig):
    """创建 file_read 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, str]:
        path = _resolve_path(args.get("path", ""), config)
        yield f"[Action] Reading file: {path}\n"
        start = args.get("start", 1)
        count = args.get("count", 200)
        keyword = args.get("keyword")
        show_linenos = args.get("show_linenos", True)
        result = file_read(
            path, start=start, keyword=keyword,
            count=count, show_linenos=show_linenos,
        )
        if show_linenos and not result.startswith("Error:"):
            result = "（以下返回格式为：行号|内容）\n" + result
        if " ... [TRUNCATED]" in result:
            result += "\n\n（某些行被截断，如需完整内容可改用 code_run 读取）"
        # SOP 读取提示
        if "/memory/" in path and not result.startswith("Error:"):
            from zero_agent.utils.memory_stats import log_memory_access
            log_memory_access(path)
            result += (
                "\n\n[SYSTEM TIPS] 请严格遵循已读取的 SOP 内容执行对应操作，"
                "切勿自行发挥或偏离文档指引。"
            )
        maxlen = 15000 // max(args.get("_tool_num", 1), 1)
        return smart_format(result, max_str_len=maxlen, omit_str="\n\n[omitted long content]\n\n")
    return _handler


def _make_file_write_handler(config: AgentConfig):
    """创建 file_write 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        path = _resolve_path(args.get("path", ""), config)
        mode = args.get("mode", "overwrite")
        action_str = {"prepend": "Prepending to", "append": "Appending to"}.get(mode, "Overwriting")
        yield f"[Action] {action_str} file: {os.path.basename(path)}\n"

        content = args.get("content", "")
        if not content:
            # 回退: 从 LLM 响应中提取 <file_content> 标签
            content = handler._extract_file_content(_response)
        if not content:
            # 二级回退: 从响应中提取代码块
            content = handler._extract_code_block(_response)
        if not content:
            yield "[Status] ERR 失败: 缺少 content 参数\n"
            return {"status": "error", "msg": "缺少 content 参数"}

        try:
            content = expand_file_refs(content, base_dir=config.workspace_dir)
            if mode == "prepend":
                old = open(path, "r", encoding="utf-8").read() if os.path.exists(path) else ""
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content + old)
            else:
                fmode = "a" if mode == "append" else "w"
                with open(path, fmode, encoding="utf-8") as f:
                    f.write(content)
            yield f"[Status] OK {mode.capitalize()} 成功 ({len(content)} bytes)\n"
            return {"status": "success", "writed_bytes": len(content)}
        except Exception as e:
            yield f"[Status] ERR 写入异常: {str(e)}\n"
            return {"status": "error", "msg": str(e)}
    return _handler


def _make_file_patch_handler(config: AgentConfig):
    """创建 file_patch 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        path = _resolve_path(args.get("path", ""), config)
        yield f"[Action] Patching file: {path}\n"
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")
        try:
            new_content = expand_file_refs(new_content, base_dir=config.workspace_dir)
        except ValueError as e:
            yield f"[Status] ERR 引用展开失败: {e}\n"
            return {"status": "error", "msg": str(e)}
        result = file_patch(path, old_content, new_content)
        yield f"\n{str(result)}\n"
        return result
    return _handler
