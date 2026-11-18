"""进程沙箱：给 ``code_run`` 提供可选的命令隔离包装.

设计原则（见 ``docs/extension-design.md`` 第九节）:

- **默认关闭**。只有显式配置 ``extensions.sandbox = "auto" | "macos" | "linux"``
  才会包装命令。
- **写入根固定为 workspace**，不接受工具参数里的 cwd —— 否则模型传一个
  ``cwd="/"`` 就能把沙箱变成 ``(subpath "/")``，等同于关闭隔离。
- **功能探测后启用**。仅 ``which`` 命中不够：Linux 上 bwrap 可能因内核禁用
  unprivileged userns 而无法工作。探测失败一律降级。
- **best-effort**。包装执行失败时由调用方回退为直接执行，绝不让正常命令失败。

平台实现:

- macOS: ``sandbox-exec -p <profile>``（系统自带，Apple 已标记 deprecated，但仍可用）
- Linux: ``bwrap``（bubblewrap），未安装或被禁用则降级
- 其他: 不支持，降级
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

VALID_MODES = ("off", "auto", "macos", "linux")
DEFAULT_MODE = "off"

# 功能探测结果缓存：同一进程内只探测一次
_PROBE_CACHE: Dict[str, bool] = {}

# 路径里出现这些字符时拒绝进入 profile，避免注入或语法错误
_FORBIDDEN_PATH_CHARS = ('"', "\n", "\r", "\x00")


def resolve_mode(config: object) -> str:
    """从配置解析沙箱模式，非法值按关闭处理.

    Args:
        config: AgentConfig 或任意带 ``sandbox_mode`` 属性的对象.

    Returns:
        规范化后的模式字符串.
    """
    raw = str(getattr(config, "sandbox_mode", "") or DEFAULT_MODE).strip().lower()
    return raw if raw in VALID_MODES else DEFAULT_MODE


def _normalize_root(root: str) -> Optional[str]:
    """校验并规范化写入根.

    只接受绝对路径，且不含会破坏 profile 语法的字符。

    Args:
        root: 候选写入根（通常是 workspace）.

    Returns:
        规范化后的真实路径；不可用时返回 None.
    """
    if not root or not os.path.isabs(str(root)):
        return None
    text = str(root)
    if any(char in text for char in _FORBIDDEN_PATH_CHARS):
        return None
    try:
        return os.path.realpath(text)
    except OSError:
        return None


def _escape(path: str) -> str:
    """转义要放进 sandbox profile 的路径.

    Args:
        path: 已经过校验的绝对路径.

    Returns:
        可安全放进双引号内的字符串.
    """
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _probe(key: str, probe_cmd: List[str]) -> bool:
    """做一个最小功能探测，确认沙箱真的可用.

    Args:
        key: 缓存键.
        probe_cmd: 探测命令.

    Returns:
        True 表示可用.
    """
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    ok = False
    try:
        result = subprocess.run(
            probe_cmd,
            capture_output=True,
            timeout=5,
        )
        ok = result.returncode == 0
    except Exception:  # noqa: BLE001 - 探测失败即视为不可用
        ok = False
    _PROBE_CACHE[key] = ok
    return ok


def _macos_available() -> bool:
    """macOS sandbox-exec 是否真的可用."""
    if sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
        return False
    return _probe(
        "macos",
        ["sandbox-exec", "-p", "(version 1)(allow default)", "--", "/usr/bin/true"],
    )


def _linux_available() -> bool:
    """Linux bwrap 是否真的可用（含 userns 探测）."""
    if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
        return False
    return _probe(
        "linux",
        ["bwrap", "--ro-bind", "/", "/", "--", "/bin/true"],
    )


def _safe_tempdir() -> Optional[str]:
    """获取可用的临时目录，失败时返回 None."""
    try:
        return tempfile.gettempdir()
    except Exception:  # noqa: BLE001
        return None


def _macos_profile(root: str) -> str:
    """构造 macOS sandbox profile：只允许写 workspace 与临时目录.

    Args:
        root: 已规范的写入根.

    Returns:
        sandbox profile 文本.
    """
    writable = [root]
    tempdir = _safe_tempdir()
    if tempdir:
        writable.append(tempdir)
    writable.extend(["/tmp", "/private/tmp"])
    rules = "\n".join(f'  (subpath "{_escape(path)}")' for path in dict.fromkeys(writable))
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
    root: Optional[str],
    mode: str,
) -> Tuple[List[str], bool, str]:
    """把命令包进平台沙箱.

    Args:
        cmd: 原始 argv 列表.
        root: 允许写入的根目录（应传 workspace，而不是模型可控的 cwd）.
        mode: ``off`` / ``auto`` / ``macos`` / ``linux``.

    Returns:
        ``(最终命令, 是否已包装, 降级原因)``；未包装时原因为空串.
    """
    if not cmd:
        return cmd, False, ""
    if mode not in VALID_MODES:
        return cmd, False, f"未知沙箱模式 {mode!r}，已降级"
    if mode == "off":
        return cmd, False, ""

    normalized = mode
    if normalized == "auto":
        if sys.platform == "darwin":
            normalized = "macos"
        elif sys.platform.startswith("linux"):
            normalized = "linux"
        else:
            return cmd, False, f"平台 {sys.platform} 不支持沙箱，已降级"

    writable_root = _normalize_root(root or "")
    if writable_root is None:
        return cmd, False, "写入根不可用（需绝对路径且不含特殊字符），已降级"

    if normalized == "macos":
        if not _macos_available():
            return cmd, False, "sandbox-exec 不可用，已降级"
        return (
            ["sandbox-exec", "-p", _macos_profile(writable_root), "--", *cmd],
            True,
            "",
        )

    if normalized == "linux":
        if not _linux_available():
            return cmd, False, "bwrap 不可用或被内核禁用，已降级"
        return (
            [
                "bwrap",
                "--ro-bind", "/", "/",
                "--bind", writable_root, writable_root,
                # 临时目录保持可写：python 模式的脚本就落在系统临时目录，
                # 用 --tmpfs 会把它遮住导致命令直接失败
                "--dev", "/dev",
                "--proc", "/proc",
                "--unshare-pid",
                "--die-with-parent",
                "--",
                *cmd,
            ],
            True,
            "",
        )

    return cmd, False, f"未知沙箱模式 {mode!r}，已降级"
