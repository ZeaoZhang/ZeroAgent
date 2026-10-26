"""文件操作工具.

file_read: 读取文件内容，支持行号范围、关键词搜索、行号显示.
file_write: 创建/覆盖/追加文件内容，支持 {{file:...}} 引用展开.
file_patch: 精准替换文件中的唯一文本块.
"""

from __future__ import annotations

import collections
import difflib
import hashlib
import itertools
import os
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from zero_agent.core.config import AgentConfig
from zero_agent.context.errors import ContextError
from zero_agent.core.types import StepAction, StepOutcome
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
            "读取文件内容。建议在修改文件前先读取，以确保获取最新的上下文和行号。支持分页读取或关键字搜索",
            "Read file. Read before modify for latest context and line numbers",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": _t(
                        "文件相对或绝对路径",
                        "Relative or absolute",
                        lang,
                    ),
                },
                "start": {
                    "type": "integer",
                    "description": _t(
                        "起始行号（从 1 开始）",
                        "Start line number (1-based)",
                        lang,
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": _t(
                        "读取的行数",
                        "Number of lines to read",
                        lang,
                    ),
                    "default": 200,
                },
                "keyword": {
                    "type": "string",
                    "description": _t(
                        "可选搜索关键字。如果提供，将返回第一个匹配项（忽略大小写）及其周边的内容",
                        "[Optional] If provided, returns first match (case-insensitive) with context",
                        lang,
                    ),
                },
                "show_linenos": {
                    "type": "boolean",
                    "description": _t(
                        "是否显示行号，建议开启以辅助 file_patch 定位",
                        "Show line numbers",
                        lang,
                    ),
                    "default": True,
                },
            },
        },
        handler=_make_file_read_handler(config),
        evidence_kind="read",
        category="file",
    ))

    registry.register(ToolDefinition(
        name="file_patch",
        description=_t(
            "精细化局部文件修改。在文件中寻找唯一的 old_content 块并替换为 new_content。"
            "要求 old_content 必须在文件中唯一存在，且空格、缩进、换行必须与原文件完全一致。"
            "如果匹配失败，请使用 file_read 重新确认文件内容",
            "Replace unique old_content with new_content. Exact match required "
            "(whitespace/indentation). On failure, file_read to recheck",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": _t("文件路径", "File path", lang),
                },
                "old_content": {
                    "type": "string",
                    "description": _t(
                        "文件中需要被替换的原始文本块（需确保唯一性）",
                        "Original text block to replace (must be unique)",
                        lang,
                    ),
                },
                "new_content": {
                    "type": "string",
                    "description": _t(
                        "替换后的新文本内容。支持 {{file:路径:起始行:结束行}} 语法引用文件内容，写入前自动展开",
                        "New content. Supports {{file:path:startLine:endLine}} to ref file lines, auto-expanded",
                        lang,
                    ),
                },
            },
        },
        handler=_make_file_patch_handler(config),
        evidence_kind="write",
        skill_action="workspace.write",
        category="file",
    ))

    registry.register(ToolDefinition(
        name="file_write",
        description=_t(
            "用于文件的新建、全量覆盖或追加写入。对于精细的代码修改，应优先使用 file_patch。"
            "长代码或临时脚本请写入 scripts/（如 scripts/za_tmp.py），内容很长时分块并用 append，"
            "随后用 code_run 的 script_path 执行。"
            "必须在原生工具参数 content 中提供完整写入内容；不会从回复正文、代码块或标签中提取内容。"
            "写入内容支持 {{file:路径:起始行:结束行}} 语法引用文件片段，写入前自动展开",
            "Create/overwrite/append files. HUGE edits ONLY. Provide the complete "
            "long scripts or temporary programs under scripts/ (for example "
            "scripts/za_tmp.py), append large content in chunks, then run with "
            "code_run script_path. "
            "file text in the native content argument; reply-body text, code "
            "fences, or tags are ignored. Supports "
            "{{file:path:startLine:endLine}}, auto-expanded",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": _t(
                        "文件路径；临时脚本推荐使用 scripts/za_tmp.py",
                        "File path; use scripts/za_tmp.py for temporary scripts",
                        lang,
                    ),
                },
                "content": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append", "prepend"],
                    "description": _t(
                        "写入模式覆盖、追加或在开头追加",
                        "Write mode",
                        lang,
                    ),
                    "default": "overwrite",
                },
            },
            "required": ["path", "content"],
        },
        handler=_make_file_write_handler(config),
        evidence_kind="write",
        skill_action="workspace.write",
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
    normalized = os.path.normpath(path)
    parts = normalized.split(os.sep)
    if parts and parts[0] == os.path.basename(os.path.normpath(config.memory_dir)):
        return os.path.normpath(os.path.join(config.memory_dir, *parts[1:]))
    return os.path.normpath(os.path.join(config.workspace_dir, path))


def _is_under_dir(path: str, directory: str) -> bool:
    """Return True when path is inside directory or is directory itself."""
    for candidate, root in (
        (os.path.abspath(path), os.path.abspath(directory)),
        (os.path.realpath(path), os.path.realpath(directory)),
    ):
        try:
            if os.path.commonpath([candidate, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _managed_path_kind(path: str, config: AgentConfig) -> str | None:
    if _is_under_dir(path, config.context.root):
        return "context"
    if _is_under_dir(path, config.memory_dir):
        return "legacy"
    return None


def _managed_write_outcome() -> StepOutcome:
    return StepOutcome(
        {
            "status": "error",
            "code": "MEMORY_WRITE_REQUIRES_SERVICE",
            "msg": (
                "MEMORY_WRITE_REQUIRES_SERVICE: use memory_update for managed memory, "
                "or the authorized context resource import command for project resources."
            ),
        },
        next_prompt="\n",
        action=StepAction.CONTINUE,
    )


def _read_managed_legacy_alias(path: str, args: Dict[str, Any], handler: Any, config: AgentConfig) -> str | None:
    managed_kind = _managed_path_kind(path, config)
    if managed_kind is None:
        return None
    runtime = getattr(handler, "runtime_context", None)
    service = getattr(handler, "context_service", None)
    if runtime is None or service is None:
        return "Error: local context identity is unavailable; use context_read with a logical URI."
    if managed_kind == "context":
        locator = getattr(service.store, "resolve_committed_revision_path", None)
        reference = locator(path) if callable(locator) else None
        if reference is None:
            return "Error: physical context storage is not a logical object; use context_list or context_search."
        uri, revision = reference
        try:
            obj = service.read(uri, revision=revision)
        except ContextError as exc:
            return f"Error: the committed context object is unavailable [{exc.code}]."
        return _format_managed_context_read(obj.content, uri, revision, args)
    root = Path(os.path.realpath(config.memory_dir))
    resolved = Path(os.path.realpath(path))
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError:
        return "Error: legacy memory aliases outside the configured data root cannot be read in local_v1."

    uri = None
    if relative.startswith("sops/"):
        try:
            raw_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
            from zero_agent.skills.manifest import load_builtin_catalog

            for skill in load_builtin_catalog():
                if skill.source_path == f"memory_seed/{relative}" and skill.source_sha256 == raw_hash:
                    uri = skill.uri
                    break
        except (OSError, ContextError):
            pass
    if uri is None:
        from zero_agent.context.migration import legacy_uri_for_relative

        uri = legacy_uri_for_relative(runtime.user_id, relative)
    try:
        obj = service.read(uri)
    except ContextError as exc:
        if exc.code == "PERMISSION_DENIED":
            return "Error: managed context access denied [PERMISSION_DENIED]."
        if uri.startswith("za://skills/builtin/"):
            return f"Error: the bundled skill alias could not be loaded [{exc.code}]."
        return (
            "Error: the legacy file has not been migrated to local context. "
            f"Use context_read after migration; proposed URI: {uri}."
        )

    return _format_managed_context_read(obj.content, uri, obj.revision, args)


def _format_managed_context_read(content: str, uri: str, revision: int, args: Dict[str, Any]) -> str:
    lines = content.splitlines()
    start = max(1, int(args.get("start", 1)))
    count = max(1, min(1000, int(args.get("count", 200))))
    keyword = args.get("keyword")
    if isinstance(keyword, str) and keyword:
        match = next(
            (index for index in range(start - 1, len(lines)) if keyword.casefold() in lines[index].casefold()),
            None,
        )
        if match is not None:
            start = max(start, match + 1 - count // 3)
        else:
            keyword = None
    selected = list(enumerate(lines[start - 1 : start - 1 + count], start))
    show_linenos = args.get("show_linenos", True)
    body = "\n".join(
        f"{line_number}|{line}" if show_linenos else line
        for line_number, line in selected
    )
    if show_linenos:
        body = f"[FILE] {len(lines)} lines | PARTIAL showing {len(selected)}\n" + body
    elif start - 1 + len(selected) < len(lines):
        body += f"\n\n[FILE PARTIAL: showing {len(selected)}/{len(lines)} lines]"
    return (
        body
        + f"\n\n[Managed context alias. Use context_read(uri={uri}, revision={revision}).]"
    )


def _make_file_read_handler(config: AgentConfig):
    """创建 file_read 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, str | StepOutcome]:
        path = _resolve_path(args.get("path", ""), config)
        managed_kind = _managed_path_kind(path, config)
        if managed_kind is None:
            yield f"[Action] Reading file: {path}\n"
        else:
            yield "[Action] Reading managed context through its authorized logical URI.\n"
        start = args.get("start", 1)
        count = args.get("count", 200)
        keyword = args.get("keyword")
        show_linenos = args.get("show_linenos", True)
        cache = getattr(handler, "_file_read_cache", None)
        cache_key = (path, str(start), str(count), str(keyword), bool(show_linenos))
        if managed_kind is None:
            try:
                stat = os.stat(path)
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            except OSError:
                signature = None
        else:
            # Managed aliases resolve through ContextService, so the physical file
            # cache must never become a second source of authority.
            signature = None
        turn = int(getattr(handler, "current_turn", 0))
        previous = cache.get(cache_key) if isinstance(cache, dict) else None
        if (
            signature is not None
            and previous is not None
            and previous[0] == signature
        ):
            return StepOutcome(
                {
                    "status": "duplicate",
                    "path": path,
                    "previous_turn": previous[1],
                    "message": "This exact file range is unchanged. Use the earlier result or read a different range.",
                },
                action=StepAction.CONTINUE,
            )
        if managed_kind is None:
            result = file_read(
                path, start=start, keyword=keyword,
                count=count, show_linenos=show_linenos,
            )
        else:
            result = _read_managed_legacy_alias(path, args, handler, config) or (
                "Error: the managed context alias could not be resolved."
            )
        if " ... [TRUNCATED]" in result:
            result += "\n\n（某些行被截断，如需完整内容可改用 code_run 读取）"
        next_prompt_suffix = None
        if (
            ("memory" in path or "sop" in path)
            and not result.startswith("Error:")
        ):
            next_prompt_suffix = (
                "\n[SYSTEM TIPS] 正在读取记忆或SOP文件，若决定按sop执行请提取"
                "sop中的关键点（特别是靠后的）update working memory."
            )
        maxlen = 15000 // max(args.get("_tool_num", 1), 1)
        result = smart_format(
            result, max_str_len=maxlen, omit_str="\n\n[omitted long content]\n\n"
        )
        if signature is not None and not result.startswith("Error:") and isinstance(cache, dict):
            try:
                after = os.stat(path)
                after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            except OSError:
                after_signature = None
            if after_signature == signature:
                cache[cache_key] = (signature, turn)
        if next_prompt_suffix is not None:
            return StepOutcome(
                result,
                next_prompt_suffix=next_prompt_suffix,
                action=StepAction.CONTINUE,
            )
        return result
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
        if _managed_path_kind(path, config) is not None:
            yield "[Action] Managed memory writes must use the context service.\n"
            return _managed_write_outcome()
        action_str = {"prepend": "Prepending to", "append": "Appending to"}.get(mode, "Overwriting")
        yield f"[Action] {action_str} file: {os.path.basename(path)}\n"

        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            yield "[Status] ERR 失败: 缺少 content 参数\n"
            return {
                "status": "error",
                "msg": (
                    "file_write content argument is required and must be a "
                    "non-empty string. Native-only mode does not read reply-body "
                    "text, code fences, or <file_content> tags."
                ),
                "_za_next_prompt": "\n",
            }

        try:
            content = expand_file_refs(content, base_dir=config.workspace_dir)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
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
            return StepOutcome(
                {"status": "error", "msg": str(e)},
                next_prompt="\n",
                action=StepAction.CONTINUE,
            )
    return _handler


def _make_file_patch_handler(config: AgentConfig):
    """创建 file_patch 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        path = _resolve_path(args.get("path", ""), config)
        if _managed_path_kind(path, config) is not None:
            yield "[Action] Managed memory writes must use the context service.\n"
            return _managed_write_outcome()
        yield f"[Action] Patching file: {path}\n"
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")
        try:
            new_content = expand_file_refs(new_content, base_dir=config.workspace_dir)
        except ValueError as e:
            yield f"[Status] ERR 引用展开失败: {e}\n"
            return StepOutcome(
                {"status": "error", "msg": str(e)},
                next_prompt="\n",
                action=StepAction.CONTINUE,
            )
        result = file_patch(path, old_content, new_content)
        yield f"\n{str(result)}\n"
        return result
    return _handler
