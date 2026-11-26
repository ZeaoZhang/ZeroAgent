"""不可逆破坏底线：系统目录、凭据、用户重要数据与 .git 元数据的写入硬拒绝.

与 ``permission_gate`` 的分工：

- ``permission_gate`` 是**策略门控**，只在 ``ask`` 下运行，会暂停等人审批。
- 本模块是**底线**，永远生效、**不弹审批**、不受 ``permissions.mode`` 影响。
  ``full`` 下没有任何门控，但「写系统目录 / 写凭据 / 覆盖用户重要数据 /
  篡改 .git」这类不可逆操作仍然被直接拒绝。

设计目标就是「允许自我升级，但不搞崩系统」：仓库源码、workspace、临时目录都不
在保护范围内，agent 照常改自己的代码、跑测试、重启。默认保护清单集中在
:data:`SYSTEM_ROOTS` / :data:`SECRET_ROOTS` / :data:`DATA_ROOTS`，可用
``safety.protected_paths`` 追加、``safety.allow_paths`` 解禁（例如把
``~/Downloads`` 交给 agent 管）。

已知边界：这是**写入路径**的底线，不是进程隔离。``code_run`` 的机器保护由
``extensions.sandbox`` 提供；沙箱未生效（``off`` 或降级）时 ``code_run`` 退回
``shell_policy`` 的静态判定兜底，但仍看不到 ``python -c`` 里的间接删除。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterable, List

#: 系统与运行时目录：写入（含删除、替换）会搞崩或损坏 OS / 工具链.
#: POSIX 清单在 macOS / Linux 上都成立；不存在的路径只是永远不命中，不影响可移植性.
_POSIX_SYSTEM_ROOTS: tuple[str, ...] = (
    "/System",
    "/Library",
    "/Applications",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/private/etc",
    "/private/var/db",
    "/private/var/vm",
    "/dev",
    "/boot",
    "/Volumes",
    "/opt/homebrew",
    "/usr/local",
)


def _system_roots() -> tuple[str, ...]:
    """Return the platform's system roots (no machine-specific literals)."""

    roots: List[str] = list(_POSIX_SYSTEM_ROOTS)
    if os.name == "nt":
        for variable in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(variable)
            if value:
                roots.append(value)
    elif sys.platform.startswith("linux"):
        roots.extend(("/proc", "/sys"))
    return tuple(dict.fromkeys(roots))


SYSTEM_ROOTS = _system_roots()

#: 凭据与安全配置：篡改等于交出账号或留下后门.
SECRET_ROOTS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.kube",
    "~/.docker",
    "~/.password-store",
    "~/.config/gh",
    "~/.netrc",
    "~/.git-credentials",
    "~/Library/Keychains",
)

#: 用户重要数据：覆盖或删除属于不可逆的数据损失.
DATA_ROOTS: tuple[str, ...] = (
    "~/Documents",
    "~/Desktop",
    "~/Downloads",
    "~/Pictures",
    "~/Movies",
    "~/Music",
    "~/Library/Mobile Documents",
)

#: git 元数据目录名：hooks / config / refs 被写等于植入持久化后门或篡改历史.
_GIT_METADATA_DIR = ".git"


def _normalize(path: Any) -> str:
    """展开 ``~`` 并规范化一个路径，空值返回空串."""

    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.normpath(os.path.expanduser(text))


def _inside(path: str, root: str) -> bool:
    """Return True when *path* is *root* itself or below it."""

    if not path or not root:
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # 不同盘符 / 相对与绝对混用
        return False


def _as_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if str(item).strip()]


def allow_roots(config: Any = None) -> List[str]:
    """返回 ``safety.allow_paths`` 解禁的路径（这些路径连同子树一起放行）."""

    return [item for item in (_normalize(p) for p in _as_list(
        getattr(config, "safety_allow_paths", None),
    )) if item]


def protected_roots(config: Any = None) -> List[str]:
    """返回当前生效的保护路径（默认清单 + 配置追加 - 配置解禁）."""

    allowed = allow_roots(config)
    candidates = [*SYSTEM_ROOTS, *SECRET_ROOTS, *DATA_ROOTS]
    candidates.extend(_as_list(getattr(config, "safety_protected_paths", None)))
    roots: List[str] = []
    for raw in candidates:
        root = _normalize(raw)
        if not root:
            continue
        # 解禁优先：显式 allow 的路径与保护路径重叠时（任一方向包含）不再保护，
        # 这样「保护 ~/Documents、只放行 ~/Documents/agent-scratch」也能生效。
        if any(_inside(root, allow) or _inside(allow, root) for allow in allowed):
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _git_metadata_hit(target: str) -> str:
    """返回命中的 ``.git`` 路径（若 *target* 落在某仓库的 .git 内）."""

    parts = target.split(os.sep)
    if _GIT_METADATA_DIR in parts:
        index = parts.index(_GIT_METADATA_DIR)
        return os.sep.join(parts[: index + 1])
    return ""


def write_denial(path: str, config: Any = None) -> str:
    """判断一次写入是否触到底线.

    Args:
        path: 已解析的写入目标（绝对路径最佳）.
        config: AgentConfig；用于读取 ``safety.protected_paths/allow_paths``.

    Returns:
        拒绝原因；允许写入时返回空串.
    """

    target = _normalize(path)
    if not target:
        return ""

    allowed: Iterable[str] = allow_roots(config)
    if any(_inside(target, root) for root in allowed):
        return ""

    git_hit = _git_metadata_hit(target)
    if git_hit:
        return (
            f"拒绝写入 git 元数据 {git_hit}：hooks/config/refs 被改写等同于"
            "植入持久化后门或篡改历史。用 code_run 里的 git 命令代替。"
        )

    for root in protected_roots(config):
        if _inside(target, root):
            return (
                f"拒绝写入受保护路径 {root}：这是系统目录、凭据或用户重要数据，"
                "写入/删除不可逆。确需放行请把它加入 safety.allow_paths。"
            )
    return ""


#: 静态判定时最多读取的脚本文本长度（与 permission_gate 同量级）.
_MAX_CODE_CHARS = 200_000


def command_denial(args: Any, workspace: str) -> str:
    """沙箱未生效时的兜底：静态判定 ``code_run`` 将要执行的代码.

    ``code_run`` 只有在沙箱**真的包上了**命令时才跳过这一步——那时机器保护由
    内核策略提供，命令本身不需要被限制。沙箱关闭或本机不可用时（``off``、
    平台不支持、``sandbox-exec``/``bwrap`` 探测失败），这里用
    ``shell_policy`` 挡住格式化、写块设备、关机、fork 炸弹与 workspace 之外的
    递归删除。

    Args:
        args: ``code_run`` 参数（读 ``script`` / ``script_path``）.
        workspace: 当前 workspace 绝对路径，作为删除作用域的判定基准.

    Returns:
        拒绝原因；允许执行时返回空串.
    """

    from zero_agent.extensions.builtin import shell_policy

    parts: List[str] = []
    script = args.get("script") if isinstance(args, dict) else None
    if isinstance(script, str) and script.strip():
        parts.append(script)
    script_path = args.get("script_path") if isinstance(args, dict) else None
    if isinstance(script_path, str) and script_path.strip():
        try:
            from zero_agent.utils.files import resolve_workspace_path

            resolved = resolve_workspace_path(script_path, workspace)
            with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(_MAX_CODE_CHARS + 1)
        except (ValueError, OSError) as exc:
            return f"拒绝执行：无法读取 script_path（{exc}），无法判定执行内容。"
        if len(content) > _MAX_CODE_CHARS:
            return (
                f"拒绝执行：script_path 超过 {_MAX_CODE_CHARS} 字符，"
                "无法完整判定执行内容。"
            )
        parts.append(content)
    if not parts:
        return ""
    verdict = shell_policy.shell_verdict("\n".join(parts), workspace)
    return verdict or ""
