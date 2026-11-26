"""权限门禁扩展：按工具声明的副作用执行高危拦截与审批.

策略层不再依赖工具名，而是读取 ``ToolDefinition.effects``：

- ``execute``：从 ``execute_code_keys`` 收集代码，命中固定危险模式直接拒绝；
  递归删除按 workspace 作用域判定。参数里没有代码时，退到本轮回复的围栏
  代码块（``code_run`` 会在参数为空时执行它）——绝不把「参数里看不到」当成
  「不会执行」。两者都为空说明这次调用不执行任何代码，交给工具自身报参数错误。
- ``write``：从 ``write_path_keys`` 收集目标，命中受保护前缀时请求人类批准。
  目标为空时：工具声明了 ``optional_write_paths`` 才放行（说明本次写入落在
  工具自己写死的默认位置，模型控制不到），否则拒绝。
- ``unknown``：工具未声明副作用，请求人类批准（同任务同工具批量授权）。
- 其余（read / network / memory / user / control）：不干预。

审批只覆盖「工具名 + 参数完全一致」的那一次调用（见设计文档第八节）。

``permissions.mode: full`` 下运行时不会调用本扩展：``ExtensionRuntime.evaluate_tool_call``
在 ``full`` 直接返回 ``allow``，因此本扩展给出的 ``confirm`` 与 ``block``
（危险 shell、声明缺失、目标不可判定）只在 ``ask`` 下生效。本扩展不感知模式。

与审批无关的「不可逆破坏底线」不在本扩展里：系统目录、凭据、用户重要数据与
``.git`` 的写入由 :mod:`zero_agent.core.safety` 在文件工具入口直接拒绝，任何
模式下都生效、也没有「同意」选项。

路径判断使用 ``realpath + 平台大小写归一 + commonpath``（见 ``path_policy``），
与文件工具实际写入的目标口径一致，避免大小写不敏感文件系统、相对路径和软链接
造成的绕过。已知边界：带 execute 的工具内用其它语言/命令间接写文件仍可绕过，
这类隔离需要沙箱（设计文档第九节），不在本扩展职责内。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from zero_agent.extensions.builtin import path_policy, shell_policy

# 受保护的写入目标：扩展根目录下的这些相对路径修改前必须经人类同意
_PROTECTED_RELATIVE: Tuple[str, ...] = ("skills", "extensions", "mcp.json")

# 扩展根目录缺失时的兜底（正常情况下由 runtime.home 派生）
_PROTECTED_FALLBACK: Tuple[str, ...] = (
    "~/.zeroagent/skills",
    "~/.zeroagent/extensions",
    "~/.zeroagent/mcp.json",
)

# 单个判定文本的字符上限；超过即拒绝，避免判定被尾部截断后失效
_MAX_CODE_CHARS = 200_000


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


def _specific_prefixes(runtime: Any) -> Tuple[str, ...]:
    """返回显式受保护的写入前缀（不含 broad 的扩展根目录）.

    前缀从 ``runtime.home`` 派生，因此自定义 ``extensions.home`` 时保护范围跟着
    走；用户配置的 ``protected_writes`` 追加在末尾。

    Args:
        runtime: ExtensionRuntime.

    Returns:
        去重后的前缀元组.
    """
    home = getattr(runtime, "home", None)
    if home is None:
        prefixes: List[str] = list(_PROTECTED_FALLBACK)
    else:
        root = Path(home)
        prefixes = [str(root / name) for name in _PROTECTED_RELATIVE]
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
    prefix = path_policy.under(path, (str(home),), workspace)
    if prefix is None:
        return None
    if workspace and path_policy.under(path, (workspace,), workspace) is not None:
        return None
    return prefix


def _memory_dir(runtime: Any) -> Optional[str]:
    """返回记忆目录；记忆文件是系统提示词的一部分，允许写入."""
    config = getattr(runtime, "config", None)
    raw = getattr(config, "memory_dir", None) if config else None
    return str(raw) if raw else None


def _extra_write_roots(runtime: Any, workspace: Optional[str]) -> list[str]:
    """返回运行时声明的额外可写根（已规范化）.

    来源是 ``AgentConfig.extra_write_roots``（由 reflect runner 从模块级
    ``WRITE_ROOTS`` 注入）。门禁刻意不把系统临时目录纳入白名单：它必须比
    沙箱更严，否则任意 ``/tmp`` 写入都会绕过审批。

    Args:
        runtime: ExtensionRuntime.
        workspace: 相对路径解析基准.

    Returns:
        规范化后的绝对路径列表.
    """
    config = getattr(runtime, "config", None)
    raw = getattr(config, "extra_write_roots", None) if config else None
    return [
        path_policy.normalize(str(item), workspace)
        for item in (raw or [])
        if str(item).strip()
    ]


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


def _optional_write_paths(call: Any) -> bool:
    """工具是否声明「写入目标可以缺省」."""
    definition = _definition(call)
    return bool(getattr(definition, "optional_write_paths", False))


def _resolve_executable_path(path: str, runtime: Any) -> str:
    """按 ``code_run`` 的口径解析将要执行的脚本路径.

    这里刻意不用文件工具的解析口径：``script_path`` 由核心执行器用
    ``resolve_workspace_path`` 解析（必须以 workspace 为根、不允许逃逸），
    门控若用另一套口径读文件，就会出现「检查的文件 ≠ 执行的文件」。

    Args:
        path: 原始路径.
        runtime: ExtensionRuntime.

    Returns:
        解析后的绝对路径.

    Raises:
        ValueError: 路径无法解析或越出 workspace.
    """
    from zero_agent.utils.files import resolve_workspace_path

    workspace = _workspace(runtime)
    if not workspace:
        raise ValueError("没有可用的 workspace，无法确认脚本位置")
    return resolve_workspace_path(path, workspace)


def _read_code_file(path: str, runtime: Any) -> Tuple[str, Optional[str]]:
    """读取待执行脚本的内容；读不到或过大都拒绝.

    Args:
        path: 原始路径.
        runtime: ExtensionRuntime.

    Returns:
        ``(代码文本, 拒绝原因)``；成功时原因为 None.
    """
    try:
        resolved = _resolve_executable_path(path, runtime)
    except ValueError as exc:
        return "", f"已拦截：脚本路径 {path!r} 不被允许（{exc}）。"
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(_MAX_CODE_CHARS + 1)
    except OSError as exc:
        return "", (
            f"已拦截：无法读取将要执行的脚本 {path!r}（{exc}），"
            "无法判定执行内容。"
        )
    if len(content) > _MAX_CODE_CHARS:
        return "", (
            f"已拦截：脚本 {path!r} 超过 {_MAX_CODE_CHARS} 字符，"
            "无法完整判定执行内容。"
        )
    return content, None


def _collect_code_text(
    args: Dict[str, Any],
    runtime: Any,
    keys: Tuple[str, ...],
) -> Tuple[str, Optional[str]]:
    """按工具声明的键收集待执行代码文本.

    以 ``_path`` 结尾或名为 path/file 的键按脚本读取（相对路径按 workspace 解析
    ——与 ``code_run`` 的 ``resolve_workspace_path`` 同口径；读不到、越界或过大
    即拒绝），其余按文本拼接。这样新增执行类工具只需声明
    ``execute_code_keys``，无需改门控代码。

    Args:
        args: 工具参数.
        runtime: ExtensionRuntime，用于解析相对路径.
        keys: 工具声明的代码参数键.

    Returns:
        ``(代码文本, 拒绝原因)``；成功时原因为 None.
    """
    parts: List[str] = []
    for key in keys:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if key.endswith("_path") or key in {"path", "file"}:
            content, reason = _read_code_file(value, runtime)
            if reason is not None:
                return "", reason
            parts.append(content)
        else:
            parts.append(value)
    return "\n".join(parts), None


def _response_code(call: Any) -> List[str]:
    """收集本轮回复里可能被执行的围栏代码块.

    工具参数为空时，``code_run`` 会退到回复中的代码块执行；门控必须用同一份
    内容判定，否则「把命令写进回复代码块」就绕过了整张黑名单。

    Args:
        call: ToolCall（可能带本轮响应）.

    Returns:
        代码块正文列表.
    """
    response = getattr(call, "response", None)
    if response is None:
        return []
    text = "\n".join(
        str(getattr(response, attr, "") or "")
        for attr in ("content", "thinking")
    )
    return shell_policy.fenced_code(text)


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


def _resolve_write_path(path: str, runtime: Any) -> str:
    """按文件工具的写入口径解析路径.

    ``tools/builtin/file.py`` 会把首段等于 ``memory_dir`` 目录名的相对路径
    重定向到记忆目录；门控必须使用同一口径，否则可以绕过审批。

    Args:
        path: 原始路径.
        runtime: ExtensionRuntime.

    Returns:
        解析后的路径.
    """
    from zero_agent.tools.builtin.file import resolve_file_path

    if not path or os.path.isabs(os.path.expanduser(path)):
        return path
    config = getattr(runtime, "config", None)
    if config is not None:
        return resolve_file_path(path, config)
    return path


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
            code, reason = _collect_code_text(args, ctx.runtime, keys)
            if reason is not None:
                return ctx.block(reason)
            if not code.strip():
                # 参数为空时 code_run 会转而执行本轮回复的围栏代码块；判定必须
                # 覆盖那一份内容。两者都为空说明本次调用不会执行任何代码
                # （工具自身会报参数错误），因此不在这里伪造安全结论。
                blocks = _response_code(call)
                if not blocks:
                    return None
                code = "\n".join(blocks)
            reason = shell_policy.shell_verdict(code, workspace)
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
            if not targets and not _optional_write_paths(call):
                # 没有声明「目标可缺省」的工具，写入目标为空说明插件声明有误，
                # 或把目标藏在了别的参数里：无法判定，拒绝。
                return ctx.block("已拦截：写入目标为空，无法判定安全性。")
            # 声明了 optional_write_paths 时，本次写入落在工具写死的默认位置
            # （通常在 workspace 内），模型无法把目标指向别处，因此不干预。
            extra_roots = _extra_write_roots(ctx.runtime, workspace)
            for raw_path in targets:
                path = _resolve_write_path(raw_path, ctx.runtime)
                prefix = path_policy.under(
                    path, _specific_prefixes(ctx.runtime), workspace,
                )
                if prefix is None:
                    prefix = _broad_prefix_hit(path, ctx.runtime, workspace)
                if prefix is not None:
                    # scope 用规范化后的绝对路径：同一文件的软链/相对写法不会
                    # 被当成两个不同目标而重复审批
                    resolved = path_policy.normalize(path, workspace)
                    return ctx.confirm(
                        f"将修改 {prefix} 下的内容：{resolved}\n"
                        f"内容：{_content_summary(args)}\n"
                        "skills 会进入系统提示词，extensions 会在下次启动执行，"
                        "mcp.json 会拉起外部命令。是否允许这一次修改？",
                        # 批量授权键：同任务内对同一路径的后续改写不再重复询问，
                        # 避免模型把内容改一个换行就再弹一次审批
                        scope=f"write:{resolved}",
                    )
                resolved = path_policy.normalize(path, workspace)
                memory_dir = _memory_dir(ctx.runtime)
                # inside() expects both sides normalized: a workspace reached
                # through a symlink (e.g. /tmp -> /private/tmp) would otherwise
                # report every ordinary in-workspace write as outside.
                workspace_root = (
                    path_policy.normalize(workspace, None) if workspace else ""
                )
                inside_workspace = bool(
                    workspace_root
                    and path_policy.inside(resolved, workspace_root)
                )
                inside_memory = bool(
                    memory_dir
                    and path_policy.inside(
                        resolved, path_policy.normalize(memory_dir, None)
                    )
                )
                inside_extra = any(
                    path_policy.inside(resolved, root) for root in extra_roots
                )
                if not inside_workspace and not inside_memory and not inside_extra:
                    return ctx.confirm(
                        f"将写入 workspace 之外的路径"
                        f"（不在记忆目录或本次任务的报告目录内）：{resolved}\n"
                        f"内容：{_content_summary(args)}\n"
                        "是否允许这一次写入？",
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
