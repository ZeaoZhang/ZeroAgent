"""Slash command helpers for the ZeroAgent desktop bridge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
_SETTINGS_PATH = _ROOT / "temp" / "desktop_settings.json"


def _current_lang() -> str:
    """Return the preferred prompt language."""
    env = (os.environ.get("ZA_LANG") or "").strip().lower()
    if env in {"zh", "en"}:
        return env
    try:
        saved = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8")).get("lang")
        if saved in {"zh", "en"}:
            return saved
    except Exception:
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value:
            return "zh" if value.lower().startswith("zh") else "en"
    return "en"


def _tail(args_text: str, label: str = "额外指示") -> str:
    """Append user-provided slash command arguments."""
    extra = (args_text or "").strip()
    return f"\n\n{label}: {extra}" if extra else ""


def build_update_prompt(args_text: str = "") -> str:
    """Build the prompt used by `/update`."""
    if _current_lang() == "en":
        return (
            "Update this ZeroAgent checkout from its configured git remote.\n"
            "1. Fetch upstream and identify the current branch.\n"
            "2. Preview upstream commits and changed files before modifying the tree.\n"
            "3. Apply the update while preserving uncommitted work.\n"
            "4. Finish with branch HEAD, changed files, and any conflicts or resolutions."
            f"{_tail(args_text, 'Extra instructions')}"
        )
    return (
        "请更新当前 ZeroAgent 仓库，使用当前配置的 git remote。\n"
        "1. 先 fetch 并识别当前分支。\n"
        "2. 修改前预览远端提交和变更文件。\n"
        "3. 执行更新，同时保留未提交工作区改动。\n"
        "4. 最后汇报分支 HEAD、变更文件、冲突或解决方式。"
        f"{_tail(args_text)}"
    )


def build_init_prompt(args_text: str = "") -> str:
    """Build the prompt used by `/init`."""
    return (
        "请执行 ZeroAgent 一键初始化。\n\n"
        "必须先读取并遵循这些初始化/能力文档：\n"
        "- memory/sops/memory_management_sop.md\n"
        "- memory/sops/web_setup_sop.md\n"
        "- memory/sops/tmwebdriver_sop.md\n"
        "- memory/sops/vision_sop.md\n\n"
        "初始化目标：\n"
        "1. 识别当前 ZeroAgent 配置：定位 config.yaml，检查 default_backend、"
        "llm_backends、workspace_dir、memory_dir、sessions_dir、log_dir 等关键字段。"
        "不要泄露 API key，只报告是否缺失或占位。\n"
        "2. 初始化本地运行目录和记忆体系；已有内容不能覆盖。\n"
        "3. 检查浏览器/Web 能力：确认 CDP bridge 相关文件、扩展路径和浏览器可用性。\n"
        "4. 对缺失能力给出可执行修复；必须用户参与时用 ask_user 一次性询问关键选择。\n"
        "5. 初始化 ZeroAgent 的身份与长期画像；没有明确资料时用 ask_user 让用户输入自由文本，"
        "再按 memory/sops/memory_management_sop.md 写入合适记忆。\n"
        "6. 输出初始化报告：已完成、仍需用户支持、建议下一步。"
        f"{_tail(args_text, '初始化范围/偏好')}"
    )


def build_autorun_prompt(args_text: str = "") -> str:
    """Build the prompt used by `/autorun`."""
    return (
        "请进入 autonomous 模式：先读 memory/sops/autonomous_operation_sop.md。"
        "全程自驱，不可逆或高风险动作先 ask_user，结案给一份简明回执。"
        f"{_tail(args_text, '任务种子')}"
    )


def build_morphling_prompt(args_text: str = "") -> str:
    """Build the prompt used by `/morphling`."""
    return (
        "请启用 Morphling 模式，将外部项目能力蒸馏到本仓库：先读 memory/sops/morphling_sop.md。"
        "没有目标时先 ask_user 获取 GitHub 仓库、本地路径或能力描述。"
        f"{_tail(args_text, '目标技能/仓库')}"
    )


def build_goal_prompt(args_text: str = "") -> str:
    """Build the prompt used by `/goal`."""
    return (
        "请进入 Goal 模式：先读 memory/sops/goal_mode_sop.md。"
        "若未给目标，先 ask_user 一次性问清一句话目标和 condition 约束。"
        f"{_tail(args_text, '用户目标')}"
    )


def build_hive_prompt(args_text: str = "") -> str:
    """Build the prompt used by `/hive`."""
    return (
        "请进入 Goal Hive 模式：先读 memory/sops/goal_hive_sop.md。"
        "集群目标、worker 配额或终止条件未明确时先 ask_user 补齐再启动。"
        f"{_tail(args_text, '集群目标')}"
    )


def list_scheduler_tasks() -> list[dict]:
    """Return configured scheduler task metadata."""
    out: list[dict] = []
    task_dir = _ROOT / "sche_tasks"
    if not task_dir.is_dir():
        return out
    for path in sorted(task_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        out.append({
            "name": path.stem,
            "path": str(path),
            "schedule": data.get("schedule") or data.get("cron") or data.get("every") or "",
            "enabled": bool(data.get("enabled", True)),
        })
    return out


def _scheduler_process() -> tuple[str, int] | None:
    """Return scheduler process name and pid when it is running."""
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    marker = os.path.join("reflect", "scheduler.py")
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["pid"] == os.getpid():
                continue
            name = (proc.info.get("name") or "").lower()
            if "python" not in name and "py.exe" not in name:
                continue
            cmdline = proc.cmdline()
        except Exception:
            continue
        if any(marker in (arg or "") for arg in cmdline):
            return ("reflect/scheduler.py", int(proc.info["pid"]))
    return None


def running_services(use_cache: bool = True) -> dict[str, int]:
    """Return currently running desktop-managed services."""
    proc = _scheduler_process()
    return {proc[0]: proc[1]} if proc else {}


def start_reflect_task(name: str) -> tuple[bool, str]:
    """Start a reflect task by module stem."""
    script = _ROOT / "reflect" / f"{name}.py"
    if not script.is_file():
        return False, f"reflect/{name}.py 不存在"
    try:
        flags = 0
        if os.name == "nt":
            flags = 0x00000200 | 0x08000000
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(_ROOT),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        time.sleep(0.4)
        rc = proc.poll()
        if rc is not None:
            return False, f"启动失败 (退出码 {rc}): reflect/{name}.py"
        return True, f"已启动 reflect/{name}.py (pid={proc.pid})"
    except Exception as exc:
        return False, f"启动失败: {type(exc).__name__}: {exc}"


PALETTE_ENTRIES: list[tuple[str, str, str]] = [
    ("/init", "[scope]", "一键初始化配置、记忆、浏览器能力与 ZeroAgent 身份画像"),
    ("/update", "[note]", "git pull 更新 ZeroAgent 仓库并报告影响面"),
    ("/autorun", "[seed]", "进入 autonomous_operation 自主模式"),
    ("/morphling", "[target]", "启用 Morphling 蒸馏外部能力"),
    ("/goal", "[goal]", "进入 Goal 模式（需 condition 约束）"),
    ("/hive", "[target]", "进入 Hive 多 worker 协作模式"),
    ("/scheduler", "", "启动或查看 reflect scheduler"),
    ("/resume", "", "列出并恢复任意历史会话"),
]


def prompt_for(cmd: str, args_text: str) -> Optional[str]:
    """Return the prompt for a desktop slash command."""
    table = {
        "/init": build_init_prompt,
        "/update": build_update_prompt,
        "/autorun": build_autorun_prompt,
        "/morphling": build_morphling_prompt,
        "/goal": build_goal_prompt,
        "/hive": build_hive_prompt,
    }
    fn = table.get(cmd)
    return fn(args_text) if fn else None

