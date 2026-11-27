"""System-prompt block describing self-extension and self-upgrade.

The agent can extend itself (skills / SOPs / plugins / MCP) and even rewrite its
own core modules, so the prompt must state the exact writable locations, when a
change takes effect, and the always-on irreversible-damage floor. Paths come
from the live config, so custom ``extensions_home`` / ``memory_dir`` are
reported correctly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from zero_agent.core import hot_restart
from zero_agent.utils.files import display_path, project_root


def _resolve(path: Any, base: str = "") -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    expanded = os.path.expanduser(text)
    if base and not os.path.isabs(expanded):
        expanded = os.path.join(base, expanded)
    return os.path.normpath(expanded)


def _home(config: Any) -> str:
    configured = getattr(config, "extensions_home", None)
    if configured:
        return _resolve(configured)
    return os.path.join(os.path.expanduser("~"), ".zeroagent")


def _mode_note_zh(config: Any) -> str:
    if str(getattr(config, "permission_mode", "full")) == "full":
        return "门控层不执行：写入与执行直接生效，不需要等待审批"
    return "门控层生效：受保护的写入会暂停等待用户批准"


def _mode_note_en(config: Any) -> str:
    if str(getattr(config, "permission_mode", "full")) == "full":
        return "no tool-call gate runs: writes and execution take effect immediately"
    return "the gate layer is on: protected writes pause for human approval"


def build(config: Any, lang: str = "zh") -> str:
    """Return the localized self-extension prompt block (``""`` when useless).

    Args:
        config: AgentConfig; supplies extensions home, memory dir and modes.
        lang: ``"zh"`` or ``"en"``.

    Returns:
        提示词块文本，包含真实路径与当前模式.
    """
    project = project_root()
    home = _home(config)
    skills = display_path(os.path.join(home, "skills"), project)
    plugins = display_path(os.path.join(home, "extensions"), project)
    mcp_json = display_path(os.path.join(home, "mcp.json"), project)
    memory = _resolve(getattr(config, "memory_dir", "")) or "./memory"
    sops = display_path(os.path.join(memory, "sops"), project)
    core_root = display_path(str(Path(hot_restart.__file__).resolve().parents[1]), project)
    sessions_dir = _resolve(getattr(config, "sessions_dir", "")) or "./workspace/sessions"
    sessions_db = display_path(os.path.join(sessions_dir, "sessions.sqlite3"), project)
    sandbox = str(getattr(config, "sandbox_mode", "auto") or "auto")
    mode = str(getattr(config, "permission_mode", "full") or "full")
    enabled = bool(getattr(config, "extensions_enabled", True))
    disabled_note_zh = (
        ""
        if enabled
        else "\n- extensions.enabled=false：插件/技能/MCP 当前不加载；改配置后下一轮生效。"
    )
    disabled_note_en = (
        ""
        if enabled
        else "\n- extensions.enabled=false: plugins/skills/MCP are not loaded; enabling it takes effect next turn."
    )

    if lang == "en":
        return (
            "<self_modification>\n"
            "You can extend and upgrade yourself; changes apply at the next turn boundary or "
            "after a hot restart, without user action.\n"
            "- Path tokens: <project> is the project root (the directory holding config.yaml), "
            "<workspace> the working directory, <home> the user home. File tools and code_run "
            "accept all three as path prefixes; in a shell use $ZA_PROJECT_ROOT / "
            "$ZA_WORKSPACE_DIR. Avoid other host-specific absolute paths.\n"
            f"- Skills: {skills}/<name>/SKILL.md (indexed next turn).\n"
            f"- SOPs: {sops}/*.md (higher priority than skills).\n"
            f"- Plugins: {plugins}/<name>.py or <name>/__init__.py with setup(ctx) to register "
            "tools/gates/prompt blocks (reload on file change; a failing setup is skipped until "
            "the file changes or /update workflow).\n"
            f"- MCP: {mcp_json} (reconnects next turn).\n"
            f"- Session history: {sessions_db} is SQLite (tables sessions/messages; "
            "messages.content holds the full transcript). Query it with the "
            "session_history tool (list/read/search) instead of exporting txt or guessing paths.\n"
            f"- Core code: {core_root}/**/*.py — compile + import checked, then a checkpoint and "
            "os.execv hot restart resumes the task (max 5 restarts per 10 min). Run tests before "
            "restarting.\n"
            f"- Permissions: {mode}; {_mode_note_en(config)}.\n"
            f"- Sandbox: code_run is {sandbox}-sandboxed, writable only in workspace and temp dirs.\n"
            "- Floor (always on, no approval): system dirs, credentials, user data (~/Documents, "
            "~/Desktop, ~/Downloads, ~/Pictures, ~/Movies, ~/Music, iCloud Drive) and any .git are "
            "refused; repo source, workspace and temp dirs are writable. Commit/push with git in "
            f"code_run, not file_write on .git.{disabled_note_en}\n"
            "</self_modification>"
        )

    return (
        "<self_modification>\n"
        "你可以扩展和升级自己；改动在下一轮 turn 边界或热重启后生效，不需要用户操作。\n"
        "- 路径记号：<project> 是项目根目录（config.yaml 所在目录），<workspace> 是工作目录，"
        "<home> 是用户目录。文件工具与 code_run 的路径参数都接受这三个前缀；shell 里用 "
        "$ZA_PROJECT_ROOT / $ZA_WORKSPACE_DIR。不要用其它主机相关的绝对路径。\n"
        f"- 技能：{skills}/<name>/SKILL.md（下一轮进入索引）。\n"
        f"- SOP：{sops}/*.md（优先级高于技能）。\n"
        f"- 插件：{plugins}/<name>.py 或 <name>/__init__.py 里的 setup(ctx)，用于注册工具/门控/"
        "提示词块（文件变化才重载；setup 报错只跳过该插件，直到文件改动或 /update workflow）。\n"
        f"- MCP：{mcp_json}（下一轮重连）。\n"
        f"- 会话历史：{sessions_db} 是 SQLite（sessions/messages 表，messages.content 存完整转录）；"
        "用 session_history 工具直接查询（list/read/search），不要导出 txt 再读，也不要猜路径。\n"
        f"- 核心代码：{core_root}/**/*.py —— 先做语法 + 子进程 import 校验，再写检查点并 "
        "os.execv 热重启续跑（同一任务 10 分钟最多 5 次）。改完先跑测试再重启。\n"
        f"- 权限：{mode}；{_mode_note_zh(config)}。\n"
        f"- 沙箱：code_run 运行在 {sandbox} 沙箱内，只能写 workspace 与临时目录。\n"
        "- 底线（常开、无审批）：系统目录、凭据目录、用户数据目录（~/Documents ~/Desktop "
        "~/Downloads ~/Pictures ~/Movies ~/Music、iCloud Drive）与任意 .git 不可写；仓库源码、"
        f"workspace、临时目录可写。git 提交/推送用 code_run 里的 git，不要用 file_write 写 .git。{disabled_note_zh}\n"
        "</self_modification>"
    )
