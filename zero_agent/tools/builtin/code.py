"""code_run — 在子进程中执行 Python/shell 代码.

Python 模式将代码写入临时 .py 文件后执行，适合多行复杂脚本.
Bash/PowerShell 模式直接通过 -c 参数执行，适合单行系统命令.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from importlib import resources
from typing import Any, Dict, Generator, List, Optional

from zero_agent.core.config import AgentConfig
from zero_agent.core.types import StepAction, StepOutcome
from zero_agent.tools.registry import ToolRegistry
from zero_agent.utils.files import resolve_workspace_path
from zero_agent.utils.text import smart_format


_logger = logging.getLogger("zero_agent.tools.code")
_VALID_CODE_TYPES = {"python", "py", "powershell", "bash", "sh", "shell", "ps1", "pwsh"}
_INLINE_SCRIPT_WARN_CHARS = 4096


def _t(zh: str, en: str, lang: str) -> str:
    """根据语言选择中文或英文文本."""
    return zh if lang == "zh" else en


def _load_code_run_header() -> str:
    """Return the optional code-run header bundled with the package."""
    try:
        return (
            resources.files("zero_agent.assets")
            .joinpath("code_run_header.py")
            .read_text(encoding="utf-8")
        )
    except Exception:
        return ""


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
        header = _load_code_run_header()
        if header:
            tmp_file.write(header)
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
        evidence_kind="execute",
        name="code_run",
        description=_t(
            "代码执行器。长代码或大量数据先用 file_write 写入 scripts/，再用 script_path 执行；"
            "script 仅用于短代码（约 4 KiB/20 行以内）。无参数但正文含完整代码块时兼容执行。",
            "Code executor. For long code or bulk data, use file_write into scripts/ and then "
            "run it with script_path. Use script only for short snippets (about 4 KiB/20 lines). "
            "A complete reply code block remains a compatibility fallback when no path/script is given.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "script_path": {
                    "type": "string",
                    "description": _t(
                        "推荐：workspace 内脚本路径，例如 scripts/za_tmp.py。工具侧读取，避免传输长代码。"
                        "与 script 互斥。",
                        "Preferred: path to a script inside the workspace, e.g. scripts/za_tmp.py. "
                        "The tool reads it server-side; mutually exclusive with script.",
                        lang,
                    ),
                },
                "script": {
                    "type": "string",
                    "description": _t(
                        "短代码参数（约 4 KiB/20 行以内）。长代码先 file_write 到 scripts/，再用 script_path。"
                        "与 script_path 互斥。",
                        "Short code only (about 4 KiB/20 lines). For long code use file_write + script_path. "
                        "Mutually exclusive with script_path.",
                        lang,
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": ["python", "bash", "powershell"],
                    "description": _t("代码类型", "Code type", lang),
                    "default": "python",
                },
                "timeout": {
                    "type": "integer",
                    "description": _t(
                        "执行超时时间（秒）",
                        "in seconds",
                        lang,
                    ),
                    "default": 60,
                },
                "cwd": {
                    "type": "string",
                    "description": _t(
                        "工作目录，默认为当前工作目录",
                        "Working directory, defaults to cwd",
                        lang,
                    ),
                },
                "inline_eval": {
                    "type": "boolean",
                    "description": _t(
                        "不允许使用除非明确要求",
                        "DO NOT USE except explicitly specified.",
                        lang,
                    ),
                },
            },
        },
        handler=_make_code_run_handler(config),
        category="builtin",
    ))


def _code_run_error(reason: str, message: str) -> StepOutcome:
    """Return a typed, retry-budget-aware code_run input error."""
    _logger.warning(
        "code_run_input_validation source=invalid_args reason=%s",
        reason,
    )
    return StepOutcome(
        message,
        next_prompt="\n",
        action=StepAction.CONTINUE,
        reason=reason,
    )


def _make_code_run_handler(config: AgentConfig):
    """创建 code_run 的 ToolHandler 适配器.

    返回一个符合 ToolHandler 签名的函数:
        (args, response, handler) -> Generator[str, None, dict]

    Args:
        config: Agent 配置，提供 workspace_dir 作为默认 cwd.

    Returns:
        ToolHandler 函数.
    """
    def _legacy_handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        code_type = args.get("type", "python")
        code = args.get("script", "")
        if not code:
            # 回退：从 LLM 响应代码块中提取。
            code = handler._extract_code_block(_response, code_type)
            if not code:
                # 截断检测：检查响应内容是否含中断标记或未闭合的代码块
                response_text = (getattr(_response, "content", "") or "") + "\n" + (getattr(_response, "thinking", "") or "")
                tail = response_text[-150:]
                has_truncation_marker = (
                    "[!!! 流异常中断" in tail
                    or "!!!Error:" in tail
                    or "max_tokens !!!]" in tail
                    or "[Error:" in tail
                )
                # 检查是否有未闭合的代码块（``` 开头但无闭合）
                backtick_count = response_text.count("```")
                unclosed_code_block = (backtick_count % 2 == 1)
                if has_truncation_marker or unclosed_code_block:
                    return StepOutcome(
                        "[Error] 代码块疑似被截断（检测到中断标记或未闭合代码块）。"
                        "请改用 file_write 将代码写入 scripts/za_tmp.py，"
                        "然后通过 code_run(script_path='scripts/za_tmp.py') 执行。",
                        next_prompt="\n",
                        action=StepAction.CONTINUE,
                    )
                return StepOutcome(
                    "[Error] Code missing. Must use reply code block or 'script' arg.",
                    next_prompt="\n",
                    action=StepAction.CONTINUE,
                )

        # inline_eval: 在进程内执行 Python 代码（用于自省/调试）
        if code_type == "inline_eval" or (code_type == "python" and args.get("inline_eval")):
            yield f"[Action] Running inline_eval\n"
            try:
                local_ns = {
                    "handler": handler,
                    "history": getattr(handler, "history_info", []),
                }
                # 如果可通过 handler 获取，则添加 parent 引用。
                try:
                    local_ns["parent"] = handler.parent
                except Exception:
                    pass
                # eval() 优先：表达式直接返回值，无需 LLM 写赋值语句
                # SyntaxError 时 fallback 到 exec() 处理语句类代码
                try:
                    result = repr(eval(code, {}, local_ns))
                except SyntaxError:
                    exec(code, {}, local_ns)
                    result = str(local_ns.get("result", local_ns.get("_", "")))
                yield f"[Status] OK inline_eval\n[Stdout]\n{result[:5000]}\n"
                return {"status": "success", "stdout": result[:10000], "exit_code": 0}
            except Exception as e:
                yield f"[Status] ERR inline_eval: {e}\n"
                return {"status": "error", "msg": str(e)}

        try:
            timeout = int(args.get("timeout", 60))
        except Exception:
            timeout = 60
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

    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        """Validate code_run inputs before delegating to the legacy executor."""
        if not isinstance(args, dict):
            return _code_run_error(
                "code_run_invalid_arguments",
                "[Error] code_run arguments must be a JSON object.",
            )

        raw_type = args.get("type", "python")
        if not isinstance(raw_type, str) or not raw_type.strip():
            return _code_run_error(
                "code_run_invalid_arguments",
                "[Error] code_run type must be a non-empty string.",
            )
        code_type = raw_type.strip().lower()
        if code_type not in _VALID_CODE_TYPES | {"inline_eval"}:
            return _code_run_error(
                "code_run_invalid_arguments",
                f"[Error] Unsupported code type: {code_type}.",
            )

        raw_timeout = args.get("timeout", 60)
        if isinstance(raw_timeout, bool):
            return _code_run_error(
                "code_run_invalid_arguments",
                "[Error] code_run timeout must be an integer.",
            )
        try:
            timeout = max(1, min(int(raw_timeout), 600))
        except (TypeError, ValueError):
            return _code_run_error(
                "code_run_invalid_arguments",
                "[Error] code_run timeout must be an integer.",
            )

        raw_cwd = args.get("cwd", "./")
        if raw_cwd is None:
            raw_cwd = "./"
        if not isinstance(raw_cwd, str):
            return _code_run_error(
                "code_run_invalid_arguments",
                "[Error] code_run cwd must be a string.",
            )

        raw_script = args.get("script")
        raw_script_path = args.get("script_path")
        if raw_script is not None and not isinstance(raw_script, str):
            return _code_run_error(
                "code_run_invalid_arguments",
                "[Error] code_run script must be a string.",
            )
        if raw_script_path is not None and not isinstance(raw_script_path, str):
            return _code_run_error(
                "code_run_invalid_arguments",
                "[Error] code_run script_path must be a string.",
            )

        script = raw_script.strip() if isinstance(raw_script, str) else ""
        script_path = raw_script_path.strip() if isinstance(raw_script_path, str) else ""
        if script and script_path:
            return _code_run_error(
                "code_run_conflicting_arguments",
                "[Error] code_run script and script_path are mutually exclusive.",
            )

        normalized = dict(args)
        normalized.update({"type": code_type, "timeout": timeout, "cwd": raw_cwd})
        source = "missing"
        if script_path:
            try:
                resolved = resolve_workspace_path(script_path, config.workspace_dir)
            except ValueError as exc:
                return _code_run_error(
                    "code_run_invalid_script_path",
                    f"[Error] Invalid script_path: {exc}",
                )
            if not os.path.isfile(resolved):
                return _code_run_error(
                    "code_run_invalid_script_path",
                    f"[Error] script_path does not name a regular file: {script_path}",
                )
            try:
                with open(resolved, "r", encoding="utf-8") as script_file:
                    script = script_file.read()
            except (OSError, UnicodeError) as exc:
                return _code_run_error(
                    "code_run_invalid_script_path",
                    f"[Error] Cannot read script_path {script_path}: {exc}",
                )
            if not script.strip():
                return _code_run_error(
                    "code_run_invalid_script_path",
                    f"[Error] script_path is empty: {script_path}",
                )
            source = "script_path"
            normalized["script"] = script
            normalized.pop("script_path", None)
            _logger.info(
                "code_run_input_validation source=%s path=%s script_len=%d",
                source,
                script_path,
                len(script),
            )
        elif script:
            source = "inline_script"
            normalized["script"] = script
            normalized.pop("script_path", None)
            _logger.info(
                "code_run_input_validation source=%s script_len=%d oversized=%s",
                source,
                len(script),
                len(script) > _INLINE_SCRIPT_WARN_CHARS,
            )
        else:
            code = handler._extract_code_block(_response, code_type) or ""
            if code:
                source = "reply_code_block"
                normalized["script"] = code
            else:
                response_text = (
                    (getattr(_response, "content", "") or "")
                    + "\n"
                    + (getattr(_response, "thinking", "") or "")
                )
                tail = response_text[-150:]
                stop_reason = str(getattr(_response, "stop_reason", "") or "").lower()
                has_truncation_marker = (
                    "[!!! 流异常中断" in tail
                    or "!!!error:" in tail.lower()
                    or "[error:" in tail.lower()
                    or "max_tokens !!!]" in tail
                    or stop_reason in {
                        "stream_interrupted",
                        "interrupted",
                        "max_tokens",
                        "length",
                    }
                )
                unclosed_code_block = response_text.count("```") % 2 == 1
                if has_truncation_marker or unclosed_code_block:
                    source = "truncated_code_block"
                    _logger.warning(
                        "code_run_input_validation source=%s response_len=%d stop_reason=%s",
                        source,
                        len(response_text),
                        stop_reason,
                    )
                    return _code_run_error(
                        "code_run_truncated_input",
                        "[Error] 代码块疑似被截断。请先用 file_write 将代码写入 "
                        "scripts/za_tmp.py（长内容分块 append），再用 "
                        "code_run(script_path='scripts/za_tmp.py') 执行。",
                    )
                _logger.warning(
                    "code_run_input_validation source=%s response_len=%d",
                    source,
                    len(response_text),
                )
                return _code_run_error(
                    "code_run_missing_input",
                    "[Error] Code missing. Must use reply code block or 'script' arg. "
                    "For long code, use file_write(path='scripts/za_tmp.py', content=...) "
                    "then code_run(script_path='scripts/za_tmp.py').",
                )

        return (yield from _legacy_handler(normalized, _response, handler))

    return _handler
