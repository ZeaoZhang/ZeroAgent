"""shell 片段的静态判定：危险模式、递归删除作用域、命令替换与嵌套 shell.

本模块只做文本分析，不执行任何命令，也不持有状态。``permission_gate`` 用它
判定 ``effects`` 含 ``execute`` 的工具将要执行的代码。

判定原则是 fail-closed：

- 命中固定危险模式（格式化、写块设备、关机、fork 炸弹）直接拒绝.
- 递归强制删除（``rm -rf``）与 ``find -delete/-exec`` 按 workspace 作用域判定：
  目标与遍历起点必须能静态确认落在 workspace 内，否则拒绝.
- 命令替换 ``$(...)``/反引号、``sh -c``、``eval`` 的内容递归判定，不因为被包
  一层就跳过.
- 解析不了就拒绝，而不是放过（例如 ``rm -rf`` 没有任何显式目标）.

已知边界（见 ``docs/extension-design.md`` 第九节）：用其它语言（``python -c``、
``perl``）间接删除、``sh script.sh`` 执行脚本文件内容，静态文本分析都看不到，
需要沙箱层兜底。
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

# 明显危险的 shell 片段：命中即拒绝，不做模糊判断
DANGEROUS_PATTERNS: List[Tuple[str, str]] = [
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\s+[^\n]*of=/dev/", "向块设备写入"),
    (r":\s*\(\)\s*\{.*\};?\s*:", "fork 炸弹"),
    (r"\bshutdown\b|\breboot\b|\bpoweroff\b", "关机或重启"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/\s*$", "放开根目录权限"),
]

# 可能包在真实命令外的前缀；剥掉后才能看清真正要执行的命令
_WRAPPERS = frozenset({
    "sudo", "doas", "env", "command", "exec", "nohup", "setsid",
    "nice", "ionice", "time", "stdbuf", "timeout", "xargs",
})

# 会把后续参数当作 shell 代码再执行一次的命令：必须递归判定
_SHELL_LAUNCHERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "eval",
})

# 会切换到另一目录的命令：之后的相对路径必须按新目录解析
_DIR_CHANGERS = frozenset({"cd", "pushd"})

_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# 命令替换：先取出内容单独判定，避免被拆成无意义 token
_COMMAND_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")

_SEGMENT_SPLIT = re.compile(r"[;&|\n]+")

_FENCE_PATTERN = re.compile(r"```[^\n]*\n(.*?)```", re.S)

# 明显不是资源名的 token：不参与删除目标的解析
_OPTION_PREFIXES = ("-",)


def fenced_code(text: str) -> List[str]:
    """提取 Markdown 回复里的全部围栏代码块.

    ``handler._extract_code_block`` 只会挑一个块执行；门控要判定「可能被执行的
    代码」，因此把所有块都收进来，宁可多判。

    Args:
        text: 响应文本.

    Returns:
        代码块正文列表；没有代码块时为空列表.
    """
    if not text:
        return []
    return [match.group(1) for match in _FENCE_PATTERN.finditer(text)]


def _command_word(token: str) -> str:
    """把 token 归一成命令名候选.

    去掉 shell 用来「转义」的写法：``\\rm``、``"rm"``、``'rm'``、``r''m`` 都
    应该被认出是 ``rm``，否则加一个引号就能绕过.

    Args:
        token: 单个 token.

    Returns:
        归一后的命令名（保留路径，交给调用方取 basename）.
    """
    cleaned = token.replace('"', "").replace("'", "")
    return cleaned.lstrip("\\")


def _tokenize(segment: str) -> List[str]:
    """按空白切分一个 shell 段，并去掉包裹的空括号.

    Args:
        segment: 单个 shell 段.

    Returns:
        token 列表.
    """
    return [token.strip("()") for token in segment.split() if token.strip("()")]


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
        if os.path.basename(_command_word(token)) in _WRAPPERS:
            index += 1
            continue
        break
    return tokens[index:]


def _find_command(tokens: List[str], name: str) -> Optional[int]:
    """在段内查找某个命令名的位置.

    Args:
        tokens: 段内 token.
        name: 命令名（比较归一后的 basename，因此 ``/bin/rm``、``\\rm``、``"rm"`` 也命中）.

    Returns:
        下标；未找到返回 None.
    """
    for index, token in enumerate(tokens):
        if os.path.basename(_command_word(token)) == name:
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
    执行，也不放行一次范围不明的删除.

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
    if not workspace:
        return f"已拦截：{action}，但没有可判定的 workspace 作用域"
    from zero_agent.extensions.builtin import path_policy

    root = path_policy.normalize(workspace, None)
    for target in targets:
        if any(char in target for char in "$`"):
            return (
                f"已拦截：{action} 的目标含 shell 展开 {target!r}，"
                "无法确认删除范围"
            )
        if target in {"*", "/*", "~", "/", "~/*"}:
            return f"已拦截：{action} {target} 会移除 workspace 之外的内容"
        resolved = path_policy.normalize(target, base)
        if not path_policy.inside(resolved, root):
            return (
                f"已拦截：{action} {target}（解析为 {resolved}）不在 workspace 内"
            )
    return None


def _start_paths(tokens: List[str]) -> List[str]:
    """取出 ``find`` 的起始路径（第一个选项之前的 token）.

    Args:
        tokens: ``find`` 之后的 token.

    Returns:
        起始路径列表；没有显式路径时返回 ``["."]``.
    """
    paths: List[str] = []
    for token in tokens:
        if token.startswith(_OPTION_PREFIXES) or token.startswith(("(", "!", ")")):
            break
        paths.append(token)
    return paths or ["."]


def _find_reason(
    tokens: List[str],
    base: Optional[str],
    workspace: Optional[str],
) -> Optional[str]:
    """判定 ``find`` 段的删除/执行动作是否越界.

    ``find`` 的问题不只是 ``-delete``：``find / -exec rm -rf {} +`` 同样会删掉
    workspace 之外的内容，因此只要出现 ``-delete``/``-exec``/``-execdir``，
    就必须先确认遍历起点在 workspace 内.

    Args:
        tokens: ``find`` 之后的 token.
        base: 相对路径基准.
        workspace: workspace 绝对路径.

    Returns:
        拒绝原因；安全时返回 None.
    """
    destructive = "-delete" in tokens or "-exec" in tokens or "-execdir" in tokens
    if not destructive:
        return None
    return _unsafe_targets_reason(
        _start_paths(tokens), base, workspace, "find 的删除/执行起点",
    )


def _substitution_reason(
    code: str,
    base: Optional[str],
    workspace: Optional[str],
) -> Optional[str]:
    """递归判定命令替换（``$(...)`` 与反引号）里的内容.

    Args:
        code: 待判定的 shell 代码.
        base: 相对路径基准.
        workspace: workspace 绝对路径.

    Returns:
        拒绝原因；安全时返回 None.
    """
    for match in _COMMAND_SUBSTITUTION.finditer(code):
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        if not inner.strip():
            continue
        reason = shell_verdict(inner, workspace, base)
        if reason is not None:
            return reason
    return None


def _shell_payload(tokens: List[str]) -> Optional[str]:
    """取出 ``sh -c`` / ``eval`` 真正会执行的 payload.

    Args:
        tokens: 启动器之后的 token.

    Returns:
        payload 文本；没有（例如 ``sh script.sh``，内容不可见）时返回 None.
    """
    if "-c" in tokens:
        payload = tokens[tokens.index("-c") + 1:]
    else:
        payload = tokens
    cleaned = [token.strip("'\"") for token in payload if token.strip("'\"")]
    return " ".join(cleaned) if cleaned else None


def shell_verdict(
    code: str,
    workspace: Optional[str],
    base: Optional[str] = None,
) -> Optional[str]:
    """检查 shell 片段，命中危险模式时返回原因.

    判定分四层：

    - 固定危险模式（格式化、写块设备、关机等）无条件拒绝.
    - 命令替换与嵌套 shell（``sh -c`` / ``eval``）递归判定.
    - 递归强制 ``rm`` 按作用域判定：目标全部在 workspace 内放行（常规清理），
      任何越界或范围不明拒绝。相对路径按沿段的 ``cd``/``pushd``/``env -C`` 解析.
    - ``find`` 出现 ``-delete``/``-exec``/``-execdir`` 时校验遍历起点.

    Args:
        code: 待执行的 shell 代码.
        workspace: workspace 绝对路径；None 表示无法判定作用域，删除类一律拒绝.
        base: 初始相对路径基准；None 时取 workspace.

    Returns:
        危险原因；安全时返回 None.
    """
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, code):
            return reason

    reason = _substitution_reason(code, base, workspace)
    if reason is not None:
        return reason

    current_base = base if base is not None else workspace
    for segment in _SEGMENT_SPLIT.split(code):
        tokens = _tokenize(segment)
        if not tokens:
            continue
        unwrapped = _unwrap_tokens(tokens)
        if not unwrapped:
            continue

        # env -C <dir> 会切换目录，后续相对路径必须按它解析
        while (
            len(unwrapped) >= 2
            and unwrapped[0] == "-C"
            and not unwrapped[1].startswith("-")
        ):
            current_base = _resolve_dir(unwrapped[1], current_base)
            unwrapped = unwrapped[2:]
        if not unwrapped:
            continue

        head = os.path.basename(_command_word(unwrapped[0]))

        if head in _DIR_CHANGERS:
            directory = [
                token for token in unwrapped[1:]
                if not token.startswith("-")
            ]
            if len(directory) == 1:
                current_base = _resolve_dir(directory[0], current_base)
            continue

        if head in _SHELL_LAUNCHERS:
            payload = _shell_payload(unwrapped[1:])
            if payload:
                reason = shell_verdict(payload, workspace, current_base)
                if reason is not None:
                    return reason
            continue

        rm_index = _find_command(unwrapped, "rm")
        if rm_index is not None:
            recursive, force, targets = _split_rm_tokens(unwrapped[rm_index + 1:])
            if recursive and force:
                reason = _unsafe_targets_reason(
                    targets, current_base, workspace, "递归强制删除",
                )
                if reason is not None:
                    return reason

        find_index = _find_command(unwrapped, "find")
        if find_index is not None:
            reason = _find_reason(
                unwrapped[find_index + 1:], current_base, workspace,
            )
            if reason is not None:
                return reason
    return None


def _resolve_dir(path: str, base: Optional[str]) -> Optional[str]:
    """把 ``cd``/``env -C`` 的目标解析成绝对路径.

    Args:
        path: 目标路径（原始 shell 文本）.
        base: 当前相对路径基准.

    Returns:
        规范化后的绝对路径；解析失败时返回 base.
    """
    from zero_agent.extensions.builtin import path_policy

    if any(char in path for char in "$`"):
        # 动态目录无法确认，交给后续删除判定按 fail-closed 处理
        return base
    return path_policy.normalize(path, base)
