"""ZeroAgent slash command dispatch system.

Central registry + dispatcher for all /slash commands. Built-in commands
mirror the existing CLI handler; delegated commands route to individual
command modules.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from zero_agent.core.types import CompletionCertificate, TaskMode
from zero_agent.frontends.commands import _builtins

if TYPE_CHECKING:
    from zero_agent.core.agent import ZeroAgent

logger = logging.getLogger("zero_agent.commands")


# ── Command definition ────────────────────────────────────────────────


@dataclass
class CommandDef:
    """Definition of a single slash command.

    Attributes:
        name:        Primary command name (without leading ``/``).
        handler:     Function ``(args: str, agent: ZeroAgent) -> str``.
        description: One-line help text.
        arg_hint:    Shown in ``/help``, e.g. ``"<backend_name>"``.
        aliases:     Alternative names (without leading ``/``).
    """

    name: str
    handler: Callable[[str, "ZeroAgent"], str]
    description: str = ""
    arg_hint: str = ""
    aliases: list[str] = field(default_factory=list)


# ── Registry ──────────────────────────────────────────────────────────

COMMANDS: list[CommandDef] = []


def _register(
    name: str,
    handler: Callable[[str, "ZeroAgent"], str],
    *,
    description: str = "",
    arg_hint: str = "",
    aliases: list[str] | None = None,
) -> None:
    """Register a command in the global registry."""
    COMMANDS.append(
        CommandDef(
            name=name,
            handler=handler,
            description=description,
            arg_hint=arg_hint,
            aliases=aliases or [],
        )
    )


# ── Built-in commands (ported from CLI) ───────────────────────────────

_register(
    "help",
    _builtins.handle_help,
    description="显示此帮助信息",
    aliases=["h", "?"],
)

_register(
    "exit",
    _builtins.handle_exit,
    description="退出 ZeroAgent",
    aliases=["quit", "q"],
)

_register(
    "new",
    _builtins.handle_new,
    description="开始新会话（清除历史）",
)

_register(
    "save",
    _builtins.handle_save,
    description="保存当前会话快照",
)

_register(
    "llms",
    _builtins.handle_llms,
    description="列出可用的 LLM 后端",
    aliases=["backends"],
)

_register(
    "resume",
    _builtins.handle_resume,
    description="生成恢复历史会话的提示",
)

_register(
    "stop",
    _builtins.handle_stop,
    description="中止当前任务",
)

_register(
    "tools",
    _builtins.handle_tools,
    description="列出可用工具",
)

_register(
    "session",
    _builtins.handle_session,
    description="动态设置会话属性（/session.xxx=yyy）",
    arg_hint="<attr>=<value>",
)


# ── Delegated commands (modules created by other tasks) ───────────────


def _lazy_import(module_name: str, attr: str = "handle"):
    """Return a handler that lazily imports the named module on first call.

    This prevents import errors when a command module is incomplete or
    missing dependencies — the error surfaces only when the command is
    actually invoked.
    """

    def _handler(args: str, agent: "ZeroAgent") -> str:
        import importlib

        full = f"zero_agent.frontends.commands.{module_name}"
        mod = importlib.import_module(full)
        return getattr(mod, attr)(args, agent)

    return _handler


_register(
    "model",
    _lazy_import("model_cmd"),
    description="显示或切换当前模型",
    arg_hint="[subcommand]",
)

_register(
    "workspace",
    _lazy_import("workspace_cmd"),
    description="工作目录管理",
    arg_hint="[subcommand]",
)

_register(
    "scheduler",
    _lazy_import("scheduler_cmd"),
    description="任务调度管理",
    arg_hint="[subcommand]",
)

_register(
    "review",
    _lazy_import("review_cmd"),
    description="会话内代码审查",
    arg_hint="[file or description]",
)

_register(
    "export",
    _lazy_import("export_cmd"),
    description="导出会话为 markdown",
    arg_hint="[path]",
)


# ── Inline stubs for commands without separate modules ────────────────


def _handle_continue(args: str, agent: "ZeroAgent") -> str:
    """Continue a previous session from a snapshot."""
    snapshot_dir = os.path.join(
        os.path.expanduser("~"), ".zero_agent", "snapshots"
    )
    if not os.path.isdir(snapshot_dir):
        return "  没有找到快照目录"

    if args.strip():
        # Load a specific snapshot file.
        path = os.path.join(snapshot_dir, args.strip())
        if not os.path.isfile(path):
            return f"  快照文件不存在: {path}"
    else:
        # List recent snapshots, pick the newest.
        files = sorted(
            [f for f in os.listdir(snapshot_dir) if f.endswith(".json")],
            reverse=True,
        )
        if not files:
            return "  没有找到会话快照"
        path = os.path.join(snapshot_dir, files[0])

    try:
        with open(path, encoding="utf-8") as f:
            snapshot = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return f"  读取快照失败: {exc}"

    agent.client.history = snapshot.get("history", [])
    agent.client.system = snapshot.get("system", "")
    model = snapshot.get("model", "unknown")
    ts = snapshot.get("timestamp", "unknown")
    return f"  已从快照恢复会话\n  model={model}  timestamp={ts}"


_register(
    "continue",
    _handle_continue,
    description="从快照恢复历史会话",
    arg_hint="[snapshot_file]",
)


def _handle_update(args: str, agent: "ZeroAgent") -> str:
    """Update agent configuration or components."""
    usage = (
        "  用法:\n"
        "    /update config         重新加载配置文件\n"
        "    /update plugins        重新加载插件\n"
    )
    if not args.strip():
        return usage

    sub = args.strip().lower()
    if sub == "config":
        ok = agent.reload_config()
        return "  配置已重新加载" if ok else "  配置未变更或重载失败"
    elif sub == "plugins":
        agent._register_builtin_plugins()
        return "  插件已重新加载"
    else:
        return f"  未知子命令: {sub}\n{usage}"


_register(
    "update",
    _handle_update,
    description="更新配置或组件",
    arg_hint="<config|plugins>",
)


def _handle_goal(args: str, agent: "ZeroAgent") -> str:
    """Set or show the current goal for the session."""
    goal_marker = getattr(agent.handler, "working", {}).get("goal", "")
    if not args.strip():
        if goal_marker:
            return f"  当前目标: {goal_marker}"
        return "  没有设置目标（使用 /goal <描述> 设置）"

    agent.handler.working["goal"] = args.strip()
    return f"  目标已设置: {args.strip()}"


_register(
    "goal",
    _handle_goal,
    description="设置或查看当前会话目标",
    arg_hint="[描述]",
)


def _handle_plan(args: str, agent: "ZeroAgent") -> str:
    """Document the /plan command surface.

    Plan start/execute is owned by the CLI/TUI frontends, which intercept
    ``/plan`` before generic dispatch and manage the workspace lifecycle.
    This handler only returns usage so ``/help`` and direct
    ``handle_command`` callers get a stable, non-mutating description.
    """
    return (
        "  用法:\n"
        "    /plan <task>      创建计划工作区并进入 plan 模式\n"
        "    /plan execute     执行已就绪的计划\n"
    )


_register(
    "plan",
    _handle_plan,
    description="创建并运行一个可验证的计划流程",
    arg_hint="[task]",
)


def has_active_plan(agent: "ZeroAgent") -> bool:
    """Return True only for plans that can still block a new /plan.

    Any pending task blocks starting a new plan because the next ``run()`` call
    must first resume or clear the paused contract.  A live plan/executing run
    and a certified ready PLAN also block so the user can run
    ``/plan execute``.  Terminal failed/cancelled/budget-exhausted leftovers
    have no pending state or ready certificate, so they must not strand the
    user behind the active-plan gate.
    """

    if getattr(agent, "_pending_task_state", None) is not None:
        return True

    handler = getattr(agent, "handler", None)
    contract = getattr(handler, "task_contract", None)
    mode = getattr(getattr(contract, "mode", None), "value", getattr(contract, "mode", None))
    if mode not in ("plan", "executing"):
        return False
    if getattr(agent, "_is_running_task", False):
        return True
    return resolve_ready_plan(agent) is not None


def resolve_pending_plan(agent: "ZeroAgent") -> tuple[str, TaskMode, str] | None:
    """Return the resumable pending PLAN/EXECUTING contract for /plan execute."""

    pending = getattr(agent, "_pending_task_state", None)
    contract = getattr(pending, "contract", None)
    mode = getattr(contract, "mode", None)
    if mode not in (TaskMode.PLAN, TaskMode.EXECUTING):
        return None
    plan_path = str(getattr(contract, "plan_path", None) or "").strip()
    if not plan_path:
        return None
    task = str(getattr(contract, "user_request", "") or "").strip()
    return task, mode, plan_path


def resolve_ready_plan(agent: "ZeroAgent") -> tuple[str, str] | None:
    """Return ``(task, plan_path)`` for the current certified-ready plan.

    A plan is ready only when ALL of the following hold, read from live core
    state (never message history):

    * the current TaskContract is still in PLAN mode;
    * its plan file exists and is non-empty;
    * the completion evaluator produced a ``CompletionCertificate`` with
      ``status == "completed"`` whose ``task_id`` matches the current
      TaskContract;
    * that certificate reports ``plan_remaining == 0`` and a
      ``verify_status`` of ``pass`` or ``partial_accepted``;
    * the plan checklist has at least one item and every item is done.

    A hand-edited plan.md or an empty checklist is never sufficient.
    """
    from zero_agent.frontends import plan_state

    try:
        if not plan_state.is_active(agent):
            return None
        plan_path = plan_state.resolve_path(agent)
    except Exception:
        return None
    if not plan_path:
        return None
    # The plan is only "ready" once the completion evaluator has certified it
    # with a real CompletionCertificate bound to the current TaskContract.
    handler = getattr(agent, "handler", None)
    contract = getattr(handler, "task_contract", None)
    certificate = getattr(handler, "completion_certificate", None)
    if not isinstance(certificate, CompletionCertificate):
        return None
    if certificate.status != "completed":
        return None
    if contract is None or certificate.task_id != getattr(contract, "task_id", None):
        return None
    if certificate.plan_remaining != 0:
        return None
    if certificate.verify_status not in ("pass", "partial_accepted"):
        return None

    try:
        with open(plan_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    items = plan_state.extract(text)
    if not items:
        return None
    if not plan_state.is_complete(items):
        return None
    task = str(getattr(contract, "user_request", "") or "").strip()
    return task, plan_path




# ── Dispatcher ────────────────────────────────────────────────────────

# Build a name→CommandDef lookup for O(1) dispatch.
_name_index: dict[str, CommandDef] | None = None


def _build_index() -> dict[str, CommandDef]:
    """Build the name-to-CommandDef lookup dict."""
    global _name_index
    if _name_index is None:
        _name_index = {}
        for cmd in COMMANDS:
            _name_index[cmd.name] = cmd
            for alias in cmd.aliases:
                _name_index[alias] = cmd
    return _name_index


# ── Exit detection ────────────────────────────────────────────────────

EXIT_NAMES: set[str] = {"exit", "quit", "q"}


def is_exit_command(cmd: str) -> bool:
    """Return True if *cmd* is an exit/quit command.

    The caller (CLI REPL) should check this before dispatching so it
    can break the loop without needing a sentinel return value.
    """
    raw = cmd.strip()
    if not raw.startswith("/"):
        return False
    parts = raw[1:].split(maxsplit=1)
    action = parts[0].lower() if parts else ""
    return action in EXIT_NAMES


# ── Public API ────────────────────────────────────────────────────────


def handle_command(cmd: str, agent: "ZeroAgent") -> str:
    """Parse a /slash command line and dispatch to the matching handler.

    Args:
        cmd:   Raw user input, e.g. ``"/model claude-sonnet-4-6"``.
        agent: The ZeroAgent instance providing config, sessions, etc.

    Returns:
        Display string for the REPL, or empty string when the handler
        has already printed side-effects.
    """
    raw = cmd.strip()
    if not raw.startswith("/"):
        return f"  不是命令: {raw}（以 / 开头）"

    # Strip leading / and split into command + args.
    rest = raw[1:]
    parts = rest.split(maxsplit=1)
    action = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    index = _build_index()

    # Handle /session.xxx=yyy syntax.
    if action.startswith("session.") or action == "session":
        target_cmd = index.get("session")
        if target_cmd is not None:
            return target_cmd.handler(rest, agent)
        return "  内部错误: session 命令未注册"

    target_cmd = index.get(action)
    if target_cmd is None:
        return f"  未知命令: /{action}（输入 /help 查看帮助）"

    try:
        return target_cmd.handler(args, agent)
    except Exception as exc:
        logger.exception("Command /%s failed", action)
        return f"  命令 /{action} 执行失败: {exc}"
