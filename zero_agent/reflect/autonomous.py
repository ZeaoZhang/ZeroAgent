"""reflect/autonomous.py — 空闲时间自动处理.

当用户离开超过 IDLE_THRESHOLD 秒时，自动触发自主任务。
通过 OS 级 API 检测键盘/鼠标不活跃时间。

模块配置（可直接修改）:
    INTERVAL: 检查间隔秒数
    IDLE_THRESHOLD: 触发空闲阈值秒数（默认 300 = 5 分钟）
    ONCE: 是否仅执行一次
"""

from __future__ import annotations

import sys

INTERVAL = 60
IDLE_THRESHOLD = 300
ONCE = False


def _get_idle_seconds() -> float:
    """获取用户空闲秒数（跨平台）.

    macOS: Quartz CGEventSourceSecondsSinceLastEventType
    Windows: GetLastInputInfo + GetTickCount
    Linux: xprintidle 命令

    Returns:
        空闲秒数，检测失败返回 float('inf').
    """
    try:
        if sys.platform == "darwin":
            from Quartz import (
                CGEventSourceSecondsSinceLastEventType,
                kCGEventSourceStateHIDSystemState,
                kCGAnyInputEventType,
            )
            return CGEventSourceSecondsSinceLastEventType(
                kCGEventSourceStateHIDSystemState, kCGAnyInputEventType
            )
        elif sys.platform == "win32":
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint),
                    ("dwTime", ctypes.c_uint),
                ]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
            elapsed = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return elapsed / 1000.0
        else:
            import subprocess

            out = subprocess.check_output(
                ["xprintidle"], text=True
            )
            return int(out.strip()) / 1000.0
    except Exception:
        return float("inf")


def check() -> str | None:
    """检查是否应触发自主任务.

    仅在用户空闲超过 IDLE_THRESHOLD 秒时返回 prompt，
    用户活跃时返回 None 不触发 agent.

    Returns:
        自主任务 prompt 字符串，或 None 表示不触发.
    """
    idle_sec = _get_idle_seconds()
    if idle_sec > IDLE_THRESHOLD:
        minutes = int(idle_sec / 60)
        return (
            f"[AUTO] 用户已离开超过 {minutes} 分钟，"
            f"作为自主智能体，请阅读自动化 SOP，执行自动任务。"
        )
    return None
