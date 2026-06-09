"""CLI runner — ZeroAgent 交互式命令行入口.

提供 readline-based REPL、一次性任务模式和 reflect 反射式唤醒模式。
支持流式输出展示、/slash 命令、键盘中断处理。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Optional

from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, default_config_path, load_default_config
from zero_agent.core.exceptions import LLMError


def main(argv: Optional[list[str]] = None) -> None:
    """CLI 主入口.

    Args:
        argv: 命令行参数列表，None 时使用 sys.argv[1:].
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # 构建配置
    config = _load_config(args)

    # 创建 agent
    agent = ZeroAgent(config=config)
    agent.handler.max_turns = agent.config.max_turns

    # 记录配置路径以支持热重载
    agent.set_config_path(getattr(config, "_source_path", None))

    if args.reflect:
        # Reflect 反射模式
        if args.llm_no is not None:
            agent.next_llm(args.llm_no)
        _run_reflect(agent, args.reflect, _parse_reflect_args(args.reflect_arg))
    elif args.task:
        # 文件 I/O 批量模式
        _run_task_mode(agent, args.task)
    elif args.input:
        # 一次性任务模式
        _run_oneshot(agent, args.input)
    else:
        # 交互 REPL 模式
        _run_repl(agent)


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器.

    Returns:
        配置好的 ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="zero-agent",
        description="ZeroAgent — 干净可复用的自主 Agent 框架",
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="YAML 配置文件路径",
    )
    parser.add_argument(
        "-i", "--input",
        default=None,
        help="一次性任务输入（非交互模式）",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=None,
        help="详细输出模式",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        default=None,
        help="安静模式（非详细输出）",
    )
    parser.add_argument(
        "-m", "--model",
        default=None,
        help="覆盖配置中的模型 ID",
    )
    parser.add_argument(
        "--llm-no",
        "--llm_no",
        dest="llm_no",
        type=int,
        default=None,
        help="按配置顺序选择第 N 个 LLM 后端（从 0 开始）",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=80,
        help="最大轮次限制（默认 80）",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="覆盖工作目录",
    )
    parser.add_argument(
        "-r", "--reflect",
        default=None,
        help="Reflect 反射模式: 指定 reflect 模块文件路径",
    )
    parser.add_argument(
        "--reflect-arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="传递给 reflect 模块 init() 的参数，可重复指定",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="文件 I/O 批量模式: 从 IODIR/input.md 读取任务，输出到 IODIR/",
    )
    return parser


def _load_config(args: argparse.Namespace) -> AgentConfig:
    """从命令行参数加载配置.

    优先级: YAML 文件 > 环境变量.
    命令行参数（model, workspace, verbose）可覆盖 YAML 中的值.

    Args:
        args: 解析后的命令行参数.

    Returns:
        AgentConfig 实例.
    """
    config_path = args.config or str(default_config_path())
    if args.config:
        config = AgentConfig.from_yaml(config_path)
    else:
        config = load_default_config()

    from zero_agent.bots.shared.continue_cmd import set_sessions_dir
    set_sessions_dir(os.path.abspath(config.sessions_dir))

    # 命令行覆盖
    if args.model:
        for backend in config.llm_backends.values():
            backend.model = args.model
    if args.workspace:
        config.workspace_dir = args.workspace
    if args.max_turns is not None:
        config.max_turns = args.max_turns
    if args.verbose:
        config.verbose = True
    elif args.quiet:
        config.verbose = False

    config._source_path = config_path  # type: ignore[attr-defined]
    return config


def _run_oneshot(agent: ZeroAgent, task: str) -> None:
    """一次性任务模式：执行单个任务并退出.

    Args:
        agent: ZeroAgent 实例.
        task: 任务描述.
    """
    gen = agent.run(task)
    try:
        for chunk in gen:
            _display_chunk(chunk)
    except KeyboardInterrupt:
        agent.abort()
        print("\n[Interrupted]")
    except LLMError as exc:
        _print_llm_error(exc)
        sys.exit(1)


def _run_repl(agent: ZeroAgent) -> None:
    """交互 REPL 模式：循环读取用户输入并执行.

    Args:
        agent: ZeroAgent 实例.
    """
    # 启用 readline 以支持历史记录和行编辑
    _setup_readline()

    _print_welcome(agent)

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue

        # /slash 命令
        if user_input.startswith("/"):
            if _handle_slash_cmd(user_input, agent):
                break
            continue

        # 执行任务
        gen = agent.run(user_input)
        try:
            for chunk in gen:
                _display_chunk(chunk)
        except KeyboardInterrupt:
            agent.abort()
            print("\n[Interrupted]")
        except LLMError as exc:
            _print_llm_error(exc)


def _setup_readline() -> None:
    """配置 readline 历史持久化."""
    try:
        import readline

        history_file = os.path.join(
            os.path.expanduser("~"), ".zero_agent", "history"
        )
        os.makedirs(os.path.dirname(history_file), exist_ok=True)

        try:
            readline.read_history_file(history_file)
        except FileNotFoundError:
            pass

        import atexit
        atexit.register(lambda: readline.write_history_file(history_file))
    except ImportError:
        pass


def _display_chunk(chunk: object) -> None:
    """在终端展示一个 chunk.

    字符串直接输出，dict 中的 turn 信息格式化输出.

    Args:
        chunk: AgentLoop yield 的数据块.
    """
    if isinstance(chunk, dict):
        if "turn" in chunk:
            return  # turn 号不单独打印，下一行有 LLM Running 标题
    elif isinstance(chunk, str):
        sys.stdout.write(chunk)
        sys.stdout.flush()


def _build_resume_prompt(agent: ZeroAgent) -> str:
    """扫描工作目录生成恢复历史会话的 prompt.

    Args:
        agent: ZeroAgent 实例.

    Returns:
        格式化的恢复提示字符串.
    """
    import os

    lines: list[str] = []
    ws = agent.config.workspace_dir
    log_dir = getattr(agent.config, "log_dir", None)

    # 扫描日志目录
    if log_dir and os.path.isdir(log_dir):
        logs = sorted(
            [f for f in os.listdir(log_dir) if f.endswith(".log")],
            key=lambda f: os.path.getmtime(os.path.join(log_dir, f)),
            reverse=True,
        )
        if logs:
            lines.append("最近的会话日志:")
            for log in logs[:5]:
                log_path = os.path.join(log_dir, log)
                size = os.path.getsize(log_path)
                mtime = os.path.getmtime(log_path)
                import datetime
                dt = datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
                lines.append(f"  {log} ({size}b, {dt})")

    # 扫描记忆文件
    memory_dir = agent.config.memory_dir
    if os.path.isdir(memory_dir):
        mem_files = [
            f for f in os.listdir(memory_dir)
            if os.path.isfile(os.path.join(memory_dir, f))
        ]
        if mem_files:
            lines.append("\n记忆文件:")
            for mf in sorted(mem_files)[:10]:
                lines.append(f"  {mf}")

    if lines:
        lines.insert(0, "=== 会话恢复信息 ===\n")
        lines.append(
            "\n可使用 [SYSTEM] Continue from previous session... "
            "开始恢复会话，先用 file_read 查看记忆文件中上次任务的上下文。"
        )
    else:
        lines.append("未找到可恢复的会话记录。")

    return "\n".join(lines)


def _handle_slash_cmd(cmd: str, agent: ZeroAgent) -> bool:
    """处理 /slash 命令.

    Args:
        cmd: 用户输入的原始命令.
        agent: ZeroAgent 实例.

    Returns:
        True 表示需要退出 REPL.
    """
    parts = cmd.split()
    action = parts[0].lower()

    if action in ("/exit", "/quit", "/q"):
        print("Bye.")
        return True
    elif action == "/help":
        print("Commands:")
        print("  /exit, /quit, /q  退出")
        print("  /help             显示此帮助")
        print("  /tools            列出可用工具")
        print("  /model            显示当前模型")
        print("  /backends         列出可用 LLM 后端")
        print("  /switch <name>    切换到指定后端")
        print("  /session.<k>=<v>  动态设置会话属性 (如 /session.max_tokens=8192)")
        print("  /resume           生成恢复历史会话的提示")
        print("  /continue         保存当前会话快照")
        print("  /new              开始新会话（清除历史）")
    elif action == "/tools":
        for tool in agent.registry.list_all():
            print(f"  {tool.name} — {tool.description[:80]}")
    elif action == "/model":
        try:
            name = agent.client.name
        except AttributeError:
            name = "unknown"
        print(f"  model: {name}")
    elif action == "/backends":
        backends = agent.list_backends()
        for name, model, is_active in backends:
            marker = " *" if is_active else ""
            print(f"  {name}: {model}{marker}")
    elif action == "/switch":
        if len(parts) < 2:
            print("  用法: /switch <backend_name>")
        else:
            target = parts[1]
            try:
                agent.switch_backend(target)
                print(f"  切换到 {target}")
            except ValueError as e:
                print(f"  {e}")
    elif action.startswith("/session."):
        # /session.xxx=yyy 动态设置当前 session 属性
        if "=" in action:
            attr_path, value = action[1:].split("=", 1)
            attr_path = attr_path.strip()
            value = value.strip()
            # 尝试解析 JSON 值
            import json
            try:
                parsed_value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                parsed_value = value
            try:
                # 支持嵌套属性如 session.max_tokens
                parts_attr = attr_path.split(".")
                target = agent.client
                for seg in parts_attr[1:]:
                    target = getattr(target, seg) if hasattr(target, seg) else target
                if hasattr(target, "__setattr__"):
                    setattr(target, parts_attr[-1] if len(parts_attr) > 1 else "session", parsed_value)
                print(f"  {attr_path} = {parsed_value}")
            except Exception as e:
                print(f"  设置失败: {e}")
        else:
            print("  用法: /session.<属性>=<值>  例如 /session.max_tokens=8192")
    elif action == "/resume":
        resume_prompt = _build_resume_prompt(agent)
        print(resume_prompt)
    elif action == "/continue":
        _save_snapshot(agent)
    elif action == "/new":
        _new_session(agent)
    else:
        print(f"  未知命令: {action}（输入 /help 查看帮助）")


def _save_snapshot(agent: ZeroAgent) -> None:
    """保存当前会话快照为 JSON 文件.

    Args:
        agent: ZeroAgent 实例.
    """
    import datetime

    snapshot_dir = os.path.join(
        os.path.expanduser("~"), ".zero_agent", "snapshots"
    )
    os.makedirs(snapshot_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(snapshot_dir, f"session_{timestamp}.json")

    snapshot = {
        "timestamp": timestamp,
        "system": getattr(agent.client, "system", ""),
        "history": getattr(agent.client, "history", []),
        "model": getattr(agent.client, "name", "unknown"),
    }

    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"  会话快照已保存: {snapshot_path}")


def _new_session(agent: ZeroAgent) -> None:
    """开始新会话：清除历史记录，保留后端配置.

    Args:
        agent: ZeroAgent 实例.
    """
    agent.client.history = []
    agent.client.system = ""
    agent.handler.working = {}
    agent.handler.history_info = []
    agent.handler._empty_ct = 0
    print("  新会话已开始（后端配置保留）")


def _run_task_mode(agent: ZeroAgent, io_dir: str) -> None:
    """文件 I/O 批量模式：从 IODIR/input.md 读取任务，输出到 IODIR/.

    Args:
        agent: ZeroAgent 实例.
        io_dir: I/O 目录路径.
    """
    import os
    import json as _json
    import datetime

    input_path = os.path.join(io_dir, "input.md")
    output_path = os.path.join(io_dir, "output.md")
    log_path = os.path.join(io_dir, "log.jsonl")

    if not os.path.isfile(input_path):
        print(f"[Error] 输入文件不存在: {input_path}")
        sys.exit(1)

    with open(input_path, encoding="utf-8") as f:
        task = f.read().strip()

    if not task:
        print("[Error] 输入文件为空")
        sys.exit(1)

    # 设置 task_dir 为 io_dir 以启用文件干预
    agent.task_dir = io_dir

    print(f"[Task Mode] 读取任务: {input_path}")
    print(f"[Task Mode] 任务: {task[:100]}...")

    output_lines: list[str] = []
    failed = False
    gen = agent.run(task)
    try:
        for chunk in gen:
            _display_chunk(chunk)
            if isinstance(chunk, str):
                output_lines.append(chunk)
    except KeyboardInterrupt:
        agent.abort()
        output_lines.append("\n[Interrupted]")
    except LLMError as exc:
        message = _format_llm_error(exc)
        _print_llm_error(exc)
        output_lines.append(f"\n[Error] {message}\n")
        failed = True

    # 写入输出
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("".join(output_lines))
    print(f"\n[Task Mode] 输出已写入: {output_path}")

    # 追加日志
    log_entry = _json.dumps({
        "timestamp": datetime.datetime.now().isoformat(),
        "task": task[:500],
        "output_length": sum(len(l) for l in output_lines),
    }, ensure_ascii=False)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(log_entry + "\n")
    print(f"[Task Mode] 日志已追加: {log_path}")

    if failed:
        sys.exit(1)

    return False


def _format_llm_error(exc: LLMError) -> str:
    """Return a compact user-facing LLM error message."""
    message = str(exc).strip()
    replacements = (
        "litellm.APIError: APIError: OpenAIException - ",
        "APIError: OpenAIException - ",
        "OpenAIException - ",
        "litellm.APIError: ",
    )
    for prefix in replacements:
        message = message.replace(prefix, "")
    message = re.sub(r"\s+", " ", message)
    if "Your request was blocked" in message:
        message += (
            " 服务端已拒绝该请求；请检查 API key、模型权限、网关策略或请求内容。"
        )
    return message


def _print_llm_error(exc: LLMError) -> None:
    print(f"\n[LLM Error] {_format_llm_error(exc)}", file=sys.stderr)


def _print_welcome(agent: ZeroAgent) -> None:
    """打印欢迎信息.

    Args:
        agent: ZeroAgent 实例.
    """
    try:
        model = agent.client.name
    except AttributeError:
        model = "unknown"
    workspace = os.path.abspath(agent.config.workspace_dir)
    tools_count = len(agent.registry.list_all())

    print(f"ZeroAgent — {tools_count} tools, model: {model}")
    print(f"Workspace: {workspace}")
    print(f"Max turns: {agent.config.max_turns}")
    print("Type /help for commands, Ctrl+C to interrupt, Ctrl+D to exit")
    print()


def _parse_reflect_args(values: list[str] | None) -> dict[str, str]:
    """Parse repeated KEY=VALUE reflect arguments."""
    parsed: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"--reflect-arg must be KEY=VALUE, got: {value}")
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"--reflect-arg key is empty: {value}")
        parsed[key] = raw
    return parsed


def _run_reflect(agent: ZeroAgent, module_path: str, init_args: Optional[dict[str, str]] = None) -> None:
    """Reflect 反射模式: 周期性检查并执行任务.

    Args:
        agent: ZeroAgent 实例.
        module_path: reflect 模块文件路径.
        init_args: 传给 reflect 模块 init() 的参数.
    """
    from zero_agent.runners.reflect_runner import ReflectRunner

    runner = ReflectRunner(agent, module_path)
    try:
        runner.run_loop(init_args or {})
    except KeyboardInterrupt:
        print("\n[Reflect interrupted]")
        runner.stop()


if __name__ == "__main__":
    main()
