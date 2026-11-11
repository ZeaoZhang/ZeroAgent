"""权限门禁扩展：高危操作拦截 + 受保护写入的审批.

行为分两类：

- 明确危险的 shell（递归强制删除、格式化、关机等）→ 直接拒绝，无批准入口。
- 受保护路径的写入 → 暂停任务请求人类批准；问句附带内容摘要，避免盲批。

审批只覆盖「工具名 + 参数完全一致」的那一次调用（见设计文档第八节）。

路径判断使用 ``realpath + normcase + commonpath``，与文件工具实际写入的目标
口径一致，避免大小写不敏感文件系统、相对路径和软链接造成的绕过。
已知边界：``code_run`` 里用其它语言/命令间接写文件仍可绕过，这类隔离需要
沙箱（设计文档第九节），不在本扩展职责内。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 明显危险的 shell 片段：命中即拒绝，不做模糊判断
_DANGEROUS_SHELL: List[Tuple[str, str]] = [
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\s+[^\n]*of=/dev/", "向块设备写入"),
    (r":\s*\(\)\s*\{.*\};?\s*:", "fork 炸弹"),
    (r"\bshutdown\b|\breboot\b|\bpoweroff\b", "关机或重启"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/\s*$", "放开根目录权限"),
]

# 受保护的写入目标：这些内容修改前必须经人类同意
_PROTECTED_PREFIXES: Tuple[str, ...] = (
    "~/.zeroagent/skills",
    "~/.zeroagent/extensions",
    "~/.zeroagent/mcp.json",
)

_WRITE_TOOLS = {"file_write", "file_patch"}


def _normalize(path: str, cwd: Optional[str]) -> str:
    """把路径规范化为可比较的绝对形式.

    依次做 expanduser → 相对转绝对 → realpath（解析软链接）→ normcase
    （大小写不敏感文件系统上统一大小写）。

    Args:
        path: 原始路径.
        cwd: 相对路径的基准目录.

    Returns:
        规范化后的绝对路径.
    """
    expanded = os.path.expanduser(str(path))
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd or os.getcwd(), expanded)
    return os.path.normcase(os.path.realpath(expanded))


def _under(path: str, prefixes: Tuple[str, ...], cwd: Optional[str]) -> Optional[str]:
    """判断路径是否落在受保护前缀下.

    Args:
        path: 待检查路径.
        prefixes: 受保护前缀.
        cwd: 相对路径基准.

    Returns:
        命中的前缀；未命中返回 None.
    """
    target = _normalize(path, cwd)
    for prefix in prefixes:
        root = _normalize(prefix, cwd)
        try:
            if os.path.commonpath([target, root]) == root:
                return prefix
        except ValueError:
            # 不同盘符等无法比较的情况，保守跳过该前缀
            continue
    return None


def _shell_verdict(code: str) -> Optional[str]:
    """检查 shell 片段，命中危险模式时返回原因.

    Args:
        code: 待执行的 shell 代码.

    Returns:
        危险原因；安全时返回 None.
    """
    for pattern, reason in _DANGEROUS_SHELL:
        if re.search(pattern, code):
            return reason
    if _is_force_recursive_rm(code):
        return "递归强制删除"
    return None


def _is_force_recursive_rm(code: str) -> bool:
    """判断命令里是否存在「递归 + 强制」的 rm（flag 顺序无关）.

    覆盖 ``rm -rf``、``rm -r -f``、``rm -R -F``、``rm --recursive --force``
    等形式。

    Args:
        code: shell 代码.

    Returns:
        True 表示命中.
    """
    for segment in re.split(r"[;&|]+", code):
        tokens = segment.replace("\n", " ").split()
        if not tokens or os.path.basename(tokens[0]) != "rm":
            continue
        recursive = False
        force = False
        for token in tokens[1:]:
            if token.startswith("--"):
                if token == "--recursive":
                    recursive = True
                elif token == "--force":
                    force = True
            elif token.startswith("-") and len(token) > 1:
                flags = token[1:]
                if "r" in flags or "R" in flags:
                    recursive = True
                if "f" in flags or "F" in flags:
                    force = True
        if recursive and force:
            return True
    return False


def _protected_prefixes(runtime: Any) -> Tuple[str, ...]:
    """结合配置返回受保护的写入前缀.

    Args:
        runtime: ExtensionRuntime.

    Returns:
        去重后的前缀元组.
    """
    prefixes = list(_PROTECTED_PREFIXES)
    config = getattr(runtime, "config", None)
    extra = getattr(config, "extensions_protected_writes", None) if config else None
    if isinstance(extra, (list, tuple)):
        prefixes.extend(str(item) for item in extra)
    home = getattr(runtime, "home", None)
    if home is not None:
        prefixes.append(str(home))
    return tuple(dict.fromkeys(prefixes))


def _collect_code_text(args: Dict[str, Any], runtime: Any) -> str:
    """收集 code_run 真正会执行的代码文本.

    ``code_run`` 支持 ``script``（短代码）与 ``script_path``（工作区脚本），
    早期门控只看 ``code``，导致这两个参数可绕过检查。

    Args:
        args: 工具参数.
        runtime: ExtensionRuntime，用于解析相对路径.

    Returns:
        拼接后的代码文本.
    """
    parts: List[str] = []
    for key in ("code", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)

    script_path = args.get("script_path")
    if isinstance(script_path, str) and script_path.strip():
        resolved = _resolve_like_file_tool(script_path, runtime)
        try:
            content = Path(os.path.expanduser(resolved)).read_text(
                encoding="utf-8", errors="replace",
            )
            parts.append(content[:200_000])
        except OSError:
            # 读不到就不做内容判定，路径本身仍受其它规则约束
            pass
    return "\n".join(parts)


def _content_summary(args: Dict[str, Any]) -> str:
    """生成待写入内容的摘要，供用户在批准前判断.

    Args:
        args: 工具参数.

    Returns:
        形如「1234 字符，sha256:ab12cd34，开头: ...」的摘要.
    """
    for key in ("content", "new_content"):
        value = args.get(key)
        if isinstance(value, str):
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
            preview = value[:200].replace("\n", "\\n")
            if len(value) > 200:
                preview += "…"
            return f"{len(value)} 字符，sha256:{digest}，开头: {preview!r}"
    if args.get("old_content") is not None or args.get("new_content") is not None:
        return "（补丁替换，无完整内容）"
    return "（无内容字段）"


def _resolve_like_file_tool(path: str, runtime: Any) -> str:
    """按文件工具的口径解析路径，避免判定与实际写入位置不一致.

    ``tools/builtin/file.py`` 会把首段等于 ``memory_dir`` 目录名的相对路径
    重定向到记忆目录；门控必须使用同一口径，否则可以绕过审批。

    Args:
        path: 原始路径.
        runtime: ExtensionRuntime.

    Returns:
        解析后的路径.
    """
    if not path or os.path.isabs(os.path.expanduser(path)):
        return path
    try:
        from zero_agent.tools.builtin.file import _resolve_path

        config = getattr(runtime, "config", None)
        if config is not None:
            return _resolve_path(path, config)
    except Exception:  # noqa: BLE001 - 解析失败时退回原路径，后续仍会做前缀判断
        pass
    return path


def _workspace(runtime: Any) -> Optional[str]:
    """返回相对路径解析基准（workspace）.

    Args:
        runtime: ExtensionRuntime.

    Returns:
        workspace 绝对路径或 None.
    """
    config = getattr(runtime, "config", None)
    workspace = getattr(config, "workspace_dir", None) if config else None
    return str(workspace) if workspace else None


def setup(ctx: Any) -> None:
    """安装权限门禁.

    Args:
        ctx: 插件上下文（ExtensionContext）.
    """

    @ctx.on_tool_call
    def guard(call: Any) -> Any:
        """门控回调：返回 None 放行，返回 ExtensionVeto 拒绝.

        Args:
            call: ToolCall 实例.

        Returns:
            None 或 ExtensionVeto.
        """
        args: Dict[str, Any] = call.args or {}
        workspace = _workspace(ctx.runtime)

        if call.tool_name == "code_run":
            code = _collect_code_text(args, ctx.runtime)
            reason = _shell_verdict(code)
            if reason is not None:
                return ctx.block(
                    f"已拦截：{reason}。这是不可逆的高危操作，如需执行请由用户手动完成。"
                )

        if call.tool_name in _WRITE_TOOLS:
            raw_path = str(args.get("path") or "")
            if not raw_path:
                return ctx.block("已拦截：写入目标为空，无法判定安全性。")
            path = _resolve_like_file_tool(raw_path, ctx.runtime)
            prefix = _under(path, _protected_prefixes(ctx.runtime), workspace)
            if prefix is not None:
                return ctx.confirm(
                    f"将修改 {prefix} 下的内容：{path}\n"
                    f"内容：{_content_summary(args)}\n"
                    "skills 会进入系统提示词，extensions 会在下次启动执行，"
                    "mcp.json 会拉起外部命令。是否允许这一次修改？"
                )

        return None
