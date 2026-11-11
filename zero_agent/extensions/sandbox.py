"""进程沙箱：给 ``code_run`` 提供可选的命令隔离包装.

设计原则（见 ``docs/extension-design.md`` 第九节）:

- **默认关闭**。只有显式配置 ``extensions.sandbox = "auto" | "macos" | "linux"``
  才会包装命令。
- **best-effort**。平台不支持、工具缺失或 profile 构建失败时，一律退回原始
  命令并返回降级原因，绝不因为沙箱问题让正常执行失败。
- 隔离目标是把写入限制在工作目录与临时目录，减少「审批层挡不住的间接写盘」。

平台实现:

- macOS: ``sandbox-exec -p <profile>``（系统自带，Apple 已标记 deprecated，但仍可用）
- Linux: ``bwrap``（bubblewrap），未安装则降级
- 其他: 不支持，降级
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from typing import List, Optional, Tuple

VALID_MODES = ("off", "auto", "macos", "linux")
DEFAULT_MODE = "off"


def resolve_mode(config: object) -> str:
    """从配置解析沙箱模式，非法值按关闭处理.

    Args:
        config: AgentConfig 或任意带 ``sandbox_mode`` 属性的对象.

    Returns:
        规范化后的模式字符串.
    """
    raw = str(getattr(config, "sandbox_mode", "") or DEFAULT_MODE).strip().lower()
    return raw if raw in VALID_MODES else DEFAULT_MODE


def _macos_profile(cwd: str) -> str:
    """构造 macOS sandbox profile：只允许写工作目录与临时目录.

    Args:
        cwd: 工作目录.

    Returns:
        sandbox profile 文本.
    """
    tmp = tempfile.gettempdir()
    writable = [cwd, tmp, "/tmp", "/private/tmp", "/private/var/folders"]
    rules = "\n".join(f'  (subpath "{path}")' for path in dict.fromkeys(writable))
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        "(allow file-write*\n"
        f"{rules}\n"
        '  (literal "/dev/null")\n'
        '  (literal "/dev/stdout")\n'
        '  (literal "/dev/stderr"))\n'
    )


def wrap_command(
    cmd: List[str],
    cwd: Optional[str],
    mode: str,
) -> Tuple[List[str], bool, str]:
    """把命令包进平台沙箱.

    Args:
        cmd: 原始 argv 列表.
        cwd: 工作目录；沙箱只在这里和临时目录放行写操作.
        mode: ``off`` / ``auto`` / ``macos`` / ``linux``.

    Returns:
        ``(最终命令, 是否已包装, 降级原因)``；未包装时原因为空串.
    """
    if not cmd:
        return cmd, False, ""
    if mode not in VALID_MODES:
        return cmd, False, f"未知沙箱模式 {mode!r}，已降级"
    normalized = mode
    if normalized == "off":
        return cmd, False, ""

    workdir = os.path.abspath(cwd or os.getcwd())

    if normalized == "auto":
        if sys.platform == "darwin":
            normalized = "macos"
        elif sys.platform.startswith("linux"):
            normalized = "linux"
        else:
            return cmd, False, f"平台 {sys.platform} 不支持沙箱，已降级"

    if normalized == "macos":
        if sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
            return cmd, False, "sandbox-exec 不可用，已降级"
        return (
            ["sandbox-exec", "-p", _macos_profile(workdir), "--", *cmd],
            True,
            "",
        )

    if normalized == "linux":
        if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
            return cmd, False, "bwrap 不可用，已降级"
        return (
            [
                "bwrap",
                "--ro-bind", "/", "/",
                "--bind", workdir, workdir,
                "--dev", "/dev",
                "--proc", "/proc",
                "--tmpfs", "/tmp",
                "--unshare-pid",
                "--die-with-parent",
                "--",
                *cmd,
            ],
            True,
            "",
        )

    return cmd, False, f"未知沙箱模式 {mode!r}，已降级"
