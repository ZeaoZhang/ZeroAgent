"""权限门禁扩展：按工具声明的副作用执行高危拦截与审批.

策略层不再依赖工具名，而是读取 ``ToolDefinition.effects``：

- ``execute``：从 ``execute_code_keys`` 收集代码，命中固定危险模式直接拒绝；
  递归删除按 workspace 作用域判定。
- ``write``：从 ``write_path_keys`` 收集目标，命中受保护前缀时请求人类批准。
- ``unknown``：工具未声明副作用，请求人类批准（同任务同工具批量授权）。
- 其余（read / network / memory / user / control）：不干预。

审批只覆盖「工具名 + 参数完全一致」的那一次调用（见设计文档第八节）。

路径判断使用 ``realpath + normcase + commonpath``，与文件工具实际写入的目标
口径一致，避免大小写不敏感文件系统、相对路径和软链接造成的绕过。
已知边界：带 execute 的工具内用其它语言/命令间接写文件仍可绕过，这类隔离
需要沙箱（设计文档第九节），不在本扩展职责内。
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


def _inside(path: str, root: str) -> bool:
    """判断路径是否位于根目录之下（已规范化的绝对路径）.

    Args:
        path: 已规范化的目标路径.
        root: 已规范化的根路径.

    Returns:
        True 表示位于根下.
    """
    if not root:
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # 不同盘符等无法比较的情况，保守视为不在根下
        return False


# 可能包在真实命令外的前缀；剥掉后才能看清真正要执行的命令
_WRAPPERS = frozenset({
    "sudo", "doas", "env", "command", "exec", "nohup", "setsid",
    "nice", "ionice", "time", "stdbuf", "timeout", "xargs",
})

_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _unwrap_tokens(tokens: List[str]) -> List[str]:
    """剥掉前导的环境变量赋值与 wrapper（sudo/env/xargs/timeout 等）.

    只剥名字，不解析 wrapper 选项：调用方随后按「第一个看到的真实命令」扫描，
    因此 ``sudo -u root rm -rf x`` 这种带选项的形态同样能被看清。

    Args:
        tokens: 单个 shell 段的 token 列表.

    Returns:
        从第一个非 wrapper 位置开始的 token 列表.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT_PATTERN.match(token):
            index += 1
            continue
        if os.path.basename(token) in _WRAPPERS:
            index += 1
            continue
        break
    return tokens[index:]


def _find_command(tokens: List[str], name: str) -> Optional[int]:
    """在段内查找某个命令名的位置.

    Args:
        tokens: 段内 token.
        name: 命令名（比较 basename，因此 ``/bin/rm`` 也命中）.

    Returns:
        下标；未找到返回 None.
    """
    for index, token in enumerate(tokens):
        if os.path.basename(token) == name:
            return index
    return None


def _split_rm_tokens(tokens: List[str]) -> Tuple[bool, bool, List[str]]:
    """解析 rm 的 flag 与目标.

    Args:
        tokens: ``rm`` 之后的 token.

    Returns:
        ``(是否递归, 是否强制, 目标列表)``.
    """
    recursive = False
    force = False
    targets: List[str] = []
    for token in tokens:
        if token == "--":
            continue
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
        else:
            targets.append(token)
    return recursive, force, targets


def _unsafe_targets_reason(
    targets: List[str],
    base: Optional[str],
    workspace: Optional[str],
    action: str,
) -> Optional[str]:
    """判断删除目标是否越出 workspace.

    目标无法静态确认（含 shell 展开、没有显式目标）时同样拒绝：宁可让用户手动
    执行，也不放行一次范围不明的删除。

    Args:
        targets: 删除目标（原始 shell 文本）.
        base: 相对路径基准（workspace 或此前 cd 到的目录）.
        workspace: workspace 绝对路径；删除目标必须落在它之内.
        action: 操作描述，用于拒绝原因.

    Returns:
        拒绝原因；安全时返回 None.
    """
    if not targets:
        return f"已拦截：{action}，但目标无法静态确认（可能在 workspace 之外）"
    root = _normalize(workspace, None) if workspace else ""
    for target in targets:
        if any(char in target for char in "$`"):
            return (
                f"已拦截：{action} 的目标含 shell 展开 {target!r}，"
                "无法确认删除范围"
            )
        if target in {"*", "/*", "~", "/", "~/*"}:
            return f"已拦截：{action} {target} 会移除 workspace 之外的内容"
        resolved = _normalize(target, base)
        if not _inside(resolved, root):
            return (
                f"已拦截：{action} {target}（解析为 {resolved}）不在 workspace 内"
            )
    return None


def _find_delete_reason(
    tokens: List[str],
    base: Optional[str],
    workspace: Optional[str],
) -> Optional[str]:
    """判断 ``find ... -delete`` 的起始路径是否越出 workspace.

    Args:
        tokens: ``find`` 之后的 token.
        base: 相对路径基准.
        workspace: workspace 绝对路径.

    Returns:
        拒绝原因；安全时返回 None.
    """
    targets: List[str] = []
    for token in tokens:
        if token.startswith("-") or token.startswith(("(", "!", ")")):
            break
        targets.append(token)
    if not targets:
        targets = ["."]
    return _unsafe_targets_reason(targets, base, workspace, "find -delete 删除")


def _shell_verdict(code: str, workspace: Optional[str]) -> Optional[str]:
    """检查 shell 片段，命中危险模式时返回原因.

    判定分两层：

    - 固定危险模式（格式化、写块设备、关机等）无条件拒绝.
    - 递归强制 ``rm`` 与 ``find -delete`` 按作用域判定：目标全部在 workspace 内
      放行（常规清理），任何越界或范围不明拒绝。相对路径按沿段的 ``cd`` 解析，
      避免 ``cd /tmp && rm -rf build`` 这类借 cd 越界的写法.

    Args:
        code: 待执行的 shell 代码.
        workspace: workspace 绝对路径；None 表示无法判定作用域，删除类一律拒绝.

    Returns:
        危险原因；安全时返回 None.
    """
    for pattern, reason in _DANGEROUS_SHELL:
        if re.search(pattern, code):
            return reason

    base = workspace
    for segment in re.split(r"[;&|]+", code.replace("\n", ";")):
        tokens = [
            token.strip("()")
            for token in segment.split()
            if token.strip("()")
        ]
        if not tokens:
            continue
        unwrapped = _unwrap_tokens(tokens)

        cd_index = _find_command(unwrapped, "cd")
        if cd_index is not None:
            directory = [
                token for token in unwrapped[cd_index + 1:]
                if not token.startswith("-")
            ]
            if len(directory) == 1:
                base = _normalize(directory[0], base)
            continue

        rm_index = _find_command(unwrapped, "rm")
        if rm_index is not None:
            recursive, force, targets = _split_rm_tokens(unwrapped[rm_index + 1:])
            if recursive and force:
                reason = _unsafe_targets_reason(
                    targets, base, workspace, "递归强制删除",
                )
                if reason is not None:
                    return reason

        find_index = _find_command(unwrapped, "find")
        if find_index is not None:
            find_tokens = unwrapped[find_index + 1:]
            if "-delete" in find_tokens:
                reason = _find_delete_reason(find_tokens, base, workspace)
                if reason is not None:
                    return reason
    return None


def _specific_prefixes(runtime: Any) -> Tuple[str, ...]:
    """返回显式受保护的写入前缀（不含 broad 的扩展根目录）.

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
    return tuple(dict.fromkeys(prefixes))


def _broad_prefix_hit(
    path: str,
    runtime: Any,
    workspace: Optional[str],
) -> Optional[str]:
    """检查是否落在扩展根目录下（broad 保护）.

    workspace 落在扩展根目录下时（例如 ``workspace_dir`` 配到
    ``~/.zeroagent/workspace``），workspace 内的写入不应触发审批——否则每个
    文件写入都要问一次。显式保护前缀（skills/extensions/mcp.json 及用户
    ``protected_writes``）由 :func:`_specific_prefixes` 先行判定，不受此豁免影响。

    Args:
        path: 待检查路径.
        runtime: ExtensionRuntime.
        workspace: workspace 绝对路径或 None.

    Returns:
        命中的前缀；未命中或属于 workspace 豁免时返回 None.
    """
    home = getattr(runtime, "home", None)
    if home is None:
        return None
    prefix = _under(path, (str(home),), workspace)
    if prefix is None:
        return None
    if workspace and _under(path, (workspace,), workspace) is not None:
        return None
    return prefix


def _definition(call: Any) -> Any:
    """返回门控入参里的工具定义；未注册工具为 None."""
    return getattr(call, "definition", None)


def _effects(call: Any) -> set[str]:
    """读取工具声明的副作用；未声明按 unknown 处理."""
    definition = _definition(call)
    effects = (
        getattr(definition, "effects", ("unknown",))
        if definition is not None
        else ("unknown",)
    )
    if isinstance(effects, str):
        effects = (effects,)
    normalized = {
        str(item).strip()
        for item in effects or ()
        if str(item).strip()
    }
    return normalized or {"unknown"}


def _declared_keys(call: Any, field: str) -> Tuple[str, ...]:
    """读取工具声明的参数键元组."""
    definition = _definition(call)
    keys = getattr(definition, field, ()) if definition is not None else ()
    if isinstance(keys, str):
        return (keys,)
    return tuple(str(key) for key in keys or () if str(key).strip())


def _collect_code_text(
    args: Dict[str, Any],
    runtime: Any,
    keys: Tuple[str, ...],
) -> str:
    """按工具声明的键收集待执行代码文本.

    以 ``_path`` 结尾或名为 path/file 的键按文件读取，其余按文本拼接。
    这样新增执行类工具只需声明 ``execute_code_keys``，无需改门控代码。

    Args:
        args: 工具参数.
        runtime: ExtensionRuntime，用于解析相对路径.
        keys: 工具声明的代码参数键.

    Returns:
        拼接后的代码文本.
    """
    parts: List[str] = []
    for key in keys:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if key.endswith("_path") or key in {"path", "file"}:
            resolved = _resolve_like_file_tool(value, runtime)
            try:
                content = Path(os.path.expanduser(resolved)).read_text(
                    encoding="utf-8", errors="replace",
                )
                parts.append(content[:200_000])
            except OSError:
                # 读不到就不做内容判定；调用方会因空文本拒绝执行
                continue
        else:
            parts.append(value)
    return "\n".join(parts)


def _write_targets(args: Dict[str, Any], keys: Tuple[str, ...]) -> List[str]:
    """收集工具声明的写入目标路径（支持字符串或字符串列表）."""
    targets: List[str] = []
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            targets.append(value)
        elif isinstance(value, (list, tuple)):
            targets.extend(str(item) for item in value if str(item).strip())
    return targets


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
        """门控回调：按工具声明的 effects 决策.

        Args:
            call: ToolCall 实例.

        Returns:
            None 放行；ExtensionVeto 拒绝；ApprovalRequired 请求审批.
        """
        args: Dict[str, Any] = call.args or {}
        workspace = _workspace(ctx.runtime)
        effects = _effects(call)

        if "execute" in effects:
            keys = _declared_keys(call, "execute_code_keys")
            if not keys:
                return ctx.block(
                    "已拦截：工具声明了 execute，但未声明 execute_code_keys，"
                    "无法判定执行内容。"
                )
            code = _collect_code_text(args, ctx.runtime, keys)
            if not code.strip():
                # 没有可判定的代码文本时交给工具自身校验（例如 code_run 会
                # 返回参数错误并由上层重试），不在这里伪造安全结论。
                return None
            reason = _shell_verdict(code, workspace)
            if reason is not None:
                return ctx.block(
                    f"{reason}。这是不可逆的高危操作，如需执行请由用户手动完成。"
                )

        if "write" in effects:
            keys = _declared_keys(call, "write_path_keys")
            if not keys:
                return ctx.block(
                    "已拦截：工具声明了 write，但未声明 write_path_keys，"
                    "无法判定写入目标。"
                )
            targets = _write_targets(args, keys)
            if not targets:
                return ctx.block("已拦截：写入目标为空，无法判定安全性。")
            for raw_path in targets:
                path = _resolve_like_file_tool(raw_path, ctx.runtime)
                prefix = _under(path, _specific_prefixes(ctx.runtime), workspace)
                if prefix is None:
                    prefix = _broad_prefix_hit(path, ctx.runtime, workspace)
                if prefix is not None:
                    # scope 用规范化后的绝对路径：同一文件的软链/相对写法不会
                    # 被当成两个不同目标而重复审批
                    resolved = _normalize(path, workspace)
                    return ctx.confirm(
                        f"将修改 {prefix} 下的内容：{resolved}\n"
                        f"内容：{_content_summary(args)}\n"
                        "skills 会进入系统提示词，extensions 会在下次启动执行，"
                        "mcp.json 会拉起外部命令。是否允许这一次修改？",
                        # 批量授权键：同任务内对同一路径的后续改写不再重复询问，
                        # 避免模型把内容改一个换行就再弹一次审批
                        scope=f"write:{resolved}",
                    )

        if "unknown" in effects:
            return ctx.confirm(
                f"工具 {call.tool_name} 未声明副作用，无法判断是否安全。\n"
                f"内容：{_content_summary(args)}\n"
                "是否允许本次调用？",
                # 未声明副作用的工具按工具名批量授权，避免每轮重复打扰；
                # 插件声明 effects 后即可脱离该默认策略。
                scope=f"tool:{call.tool_name}",
            )

        return None
