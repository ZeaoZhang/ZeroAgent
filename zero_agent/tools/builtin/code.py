"""code_run — 在子进程中执行 Python/shell 代码.

Python 模式将代码写入临时 .py 文件后执行，适合多行复杂脚本.
Bash/PowerShell 模式直接通过 -c 参数执行，适合单行系统命令.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Generator, List, Optional

from zero_agent.core.config import AgentConfig
from zero_agent.core.exceptions import ToolError
from zero_agent.tools.registry import ToolRegistry
from zero_agent.utils.text import smart_format


def _t(zh: str, en: str, lang: str) -> str:
    """根据语言选择中文或英文文本."""
    return zh if lang == "zh" else en


def code_run(
    code: str,
    code_type: str = "python",
    timeout: int = 60,
    cwd: Optional[str] = None,
    code_cwd: Optional[str] = None,
    stop_signal: Optional[List[bool]] = None,
    maxlen: int = 10000,
) -> Generator[str, None, dict]:
    """在子进程中执行 Python 或 shell 代码.

    Python 模式将代码写入临时 .py 文件后执行，适合多行复杂脚本.
    Bash/PowerShell 模式直接通过 -c 参数执行，适合单行系统命令.

    Args:
        code: 待执行的代码文本.
        code_type: 代码类型 "python" | "bash" | "powershell".
        timeout: 执行超时秒数，超时后强制终止子进程.
        cwd: 子进程工作目录，默认使用当前目录.
        code_cwd: Python 临时 .py 文件存放目录，默认使用系统临时目录.
        stop_signal: 外部停止信号，传入可变列表 [True] 时终止执行.
        maxlen: 返回 stdout 的最大字符数，超出部分截断.

    Yields:
        执行过程中的状态信息字符串.

    Returns:
        {"status": "success"|"error", "stdout": str, "exit_code": int}
    """
    preview = (code[:60].replace("\n", " ") + "...") if len(code) > 60 else code.replace("\n", " ").strip()
    cwd = cwd or os.getcwd()
    tmp_path: Optional[str] = None

    yield f"[Action] Running {code_type}: {preview}\n"

    # 构建命令
    if code_type in ("python", "py"):
        tmp_file = tempfile.NamedTemporaryFile(
            suffix=".ai.py", delete=False, mode="w", encoding="utf-8",
            dir=code_cwd,
        )
        # 注入 code_run_header（若存在）
        header_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "assets", "code_run_header.py",
        )
        if os.path.exists(header_path):
            try:
                with open(header_path, "r", encoding="utf-8") as hf:
                    header = hf.read()
                tmp_file.write(header)
            except Exception:
                pass
        tmp_file.write(code)
        tmp_path = tmp_file.name
        tmp_file.close()
        cmd = [sys.executable, "-X", "utf8", "-u", tmp_path]
    elif code_type in ("powershell", "bash", "sh", "shell", "ps1", "pwsh"):
        if os.name == "nt":
            ps = "pwsh" if shutil.which("pwsh") else "powershell"
            utf8_prefix = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            cmd = [ps, "-NoProfile", "-NonInteractive", "-Command", utf8_prefix + code]
        else:
            cmd = ["bash", "-c", code]
    else:
        return {"status": "error", "msg": f"不支持的类型: {code_type}"}

    # Windows 下隐藏控制台窗口
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        creationflags = 0x08000000

    full_stdout: List[str] = []

    def _stream_reader(proc: subprocess.Popen) -> None:
        """读取子进程 stdout 到 full_stdout 列表."""
        if proc.stdout is None:
            return
        try:
            for line_bytes in iter(proc.stdout.readline, b""):
                try:
                    line = line_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    line = line_bytes.decode("gbk", errors="ignore")
                full_stdout.append(line)
        except Exception:
            pass

    process = None
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=cwd,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        start_t = time.time()
        reader_thread = threading.Thread(
            target=_stream_reader, args=(process,), daemon=True,
        )
        reader_thread.start()

        while reader_thread.is_alive():
            is_timeout = time.time() - start_t > timeout
            should_stop = stop_signal and stop_signal[0] if stop_signal else False
            if is_timeout or should_stop:
                process.kill()
                if is_timeout:
                    full_stdout.append("\n[Timeout Error] 超时强制终止")
                else:
                    full_stdout.append("\n[Stopped] 用户强制终止")
                break
            time.sleep(1)

        reader_thread.join(timeout=1)
        exit_code = process.poll()

        stdout_str = "".join(full_stdout)

        # 处理过长输出中的反引号（防止 markdown 代码块解析异常）
        output_snippet = smart_format(stdout_str, max_str_len=600, omit_str="\n\n[omitted long output]\n\n")
        output_snippet = re.sub(
            r"`{4,}",
            lambda m: m.group(0)[:3] + "​" + m.group(0)[3:],
            output_snippet,
        )

        status = "success" if exit_code == 0 else "error"
        status_icon = "OK" if exit_code == 0 else "ERR"
        yield f"[Status] {status_icon} Exit Code: {exit_code}\n[Stdout]\n{output_snippet}\n"

        if process.stdout:
            threading.Thread(target=process.stdout.close, daemon=True).start()

        return {
            "status": status,
            "stdout": smart_format(stdout_str, max_str_len=maxlen, omit_str="\n\n[omitted long output]\n\n"),
            "exit_code": exit_code,
        }
    except Exception as e:
        if process is not None:
            process.kill()
        return {"status": "error", "msg": str(e)}
    finally:
        if code_type in ("python", "py") and tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def register_code_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """注册 code_run 工具到 ToolRegistry.

    Args:
        registry: 工具注册中心.
        config: Agent 配置.
    """
    from zero_agent.tools.registry import ToolDefinition

    lang = config.resolved_tool_language

    registry.register(ToolDefinition(
        name="code_run",
        description=_t(
            "在隔离子进程中执行 Python 或 Shell 代码。"
            "Python 模式 (type=python): 将代码写入临时 .py 文件后执行，适合多行复杂脚本。"
            "Bash 模式 (type=bash): 通过 bash -c 执行，适合单行系统命令。"
            "PowerShell 模式 (type=powershell): Windows PowerShell 命令。",
            "Execute Python or Shell code in an isolated subprocess. "
            "Python mode (type=python): writes code to a temp .py file and runs it, "
            "suited for multi-line complex scripts. "
            "Bash mode (type=bash): runs via bash -c, suited for one-line system commands. "
            "PowerShell mode (type=powershell): Windows PowerShell commands.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["python", "bash", "powershell", "inline_eval"],
                    "description": _t(
                        "代码类型: python(默认) / bash / powershell",
                        "Code type: python(default) / bash / powershell",
                        lang,
                    ),
                },
                "code": {
                    "type": "string",
                    "description": _t(
                        "待执行的代码文本",
                        "Code text to execute",
                        lang,
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": _t(
                        "执行超时秒数，默认 60",
                        "Execution timeout in seconds, default 60",
                        lang,
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": _t(
                        "子进程工作目录路径",
                        "Working directory path for the subprocess",
                        lang,
                    ),
                },
            },
            "required": ["code"],
        },
        handler=_make_code_run_handler(config),
        category="builtin",
    ))


def _make_code_run_handler(config: AgentConfig):
    """创建 code_run 的 ToolHandler 适配器.

    返回一个符合 ToolHandler 签名的函数:
        (args, response, handler) -> Generator[str, None, dict]

    Args:
        config: Agent 配置，提供 workspace_dir 作为默认 cwd.

    Returns:
        ToolHandler 函数.
    """
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        code_type = args.get("type", "python")
        code = args.get("code") or args.get("script", "")
        if not code:
            raise ToolError("缺少 code 或 script 参数")

        # inline_eval: 在进程内执行 Python 代码（用于自省/调试）
        if code_type == "inline_eval":
            yield f"[Action] Running inline_eval\n"
            try:
                local_ns = {
                    "handler": handler,
                    "history": getattr(handler, "history_info", []),
                }
                exec(code, {}, local_ns)
                result = str(local_ns.get("result", local_ns.get("_", "")))
                yield f"[Status] OK inline_eval\n[Stdout]\n{result[:5000]}\n"
                return {"status": "success", "stdout": result[:10000], "exit_code": 0}
            except Exception as e:
                yield f"[Status] ERR inline_eval: {e}\n"
                return {"status": "error", "msg": str(e)}

        timeout = int(args.get("timeout", 60))
        cwd = os.path.normpath(os.path.join(
            config.workspace_dir, args.get("cwd", "./"),
        ))
        maxlen = 10000 // max(args.get("_tool_num", 1), 1)
        stop_signal = getattr(handler, "code_stop_signal", None)

        return (yield from code_run(
            code=code,
            code_type=code_type,
            timeout=timeout,
            cwd=cwd,
            maxlen=maxlen,
            stop_signal=stop_signal,
        ))

    return _handler
