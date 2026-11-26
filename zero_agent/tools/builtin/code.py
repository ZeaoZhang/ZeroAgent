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
from zero_agent.core.safety import command_denial
from zero_agent.core.types import StepAction, StepOutcome
from zero_agent.tools.registry import ToolRegistry
from zero_agent.utils.files import expand_tool_path, project_root, resolve_workspace_path
from zero_agent.utils.text import smart_format


_logger = logging.getLogger("zero_agent.tools.code")
_VALID_CODE_TYPES = {"python", "py", "powershell", "bash", "sh", "shell", "ps1", "pwsh"}
_INLINE_SCRIPT_WARN_CHARS = 4096


def _sandbox_active(
    root: str,
    mode: str,
    extra_roots: List[str],
) -> bool:
    """Return True when the sandbox really wraps commands for this machine."""

    try:
        from zero_agent.extensions.sandbox import will_apply

        return will_apply(root, mode, extra_roots)
    except Exception:  # noqa: BLE001 - 探测失败按不可用处理（走静态兜底）
        return False


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
    env: Optional[dict[str, str]] = None,
    sandbox_mode: str = "",
    sandbox_root: str = "",
    sandbox_extra_roots: Optional[List[str]] = None,
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
        sandbox_mode: 沙箱模式，空串表示不包装.
        sandbox_root: 沙箱写入根（workspace），不是模型可控的 cwd.
        sandbox_extra_roots: 额外写入根（运行时注入的报告目录等）.

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

    def _spawn(argv: List[str]) -> subprocess.Popen:
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=cwd,
            startupinfo=startupinfo,
            creationflags=creationflags,
            env=env,
        )

    original_cmd = cmd
    sandbox_applied = False
    sandbox_reason = ""
    requested_mode = sandbox_mode
    if sandbox_mode and sandbox_mode != "off":
        try:
            from zero_agent.extensions.sandbox import wrap_command

            # 写入根必须是 workspace，而不是模型可指定的 cwd
            cmd, sandbox_applied, sandbox_reason = wrap_command(
                cmd, sandbox_root or cwd, sandbox_mode, sandbox_extra_roots or (),
            )
            if sandbox_reason:
                yield f"[Sandbox] {sandbox_reason}\n"
        except Exception as sandbox_exc:  # noqa: BLE001 - 沙箱问题不得阻断执行
            sandbox_applied = False
            sandbox_reason = f"wrap failed: {sandbox_exc}"
            yield f"[Sandbox] 包装失败: {sandbox_exc}\n"

    if sandbox_applied:
        from zero_agent.extensions.sandbox import writable_roots

        roots = writable_roots(sandbox_root or cwd, sandbox_extra_roots or ())
        yield f"[Sandbox] active (writable: {', '.join(roots)})\n"

    process = None
    try:
        if requested_mode in {"macos", "linux"} and not sandbox_applied:
            # An explicit platform sandbox is a contract: never silently run
            # unsandboxed. Kept inside the try so the temp script is cleaned up.
            yield f"[Error] Sandbox {requested_mode} unavailable: {sandbox_reason}\n"
            return {
                "status": "error",
                "msg": f"sandbox {requested_mode} unavailable: {sandbox_reason}",
                "exit_code": None,
            }
        try:
            process = _spawn(cmd)
        except (OSError, ValueError):
            if not sandbox_applied or requested_mode != "auto":
                raise
            # auto 模式下沙箱不可用：明确告警后回退为原始命令
            yield "[Sandbox] 沙箱启动失败，回退为直接执行\n"
            process = _spawn(original_cmd)
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
            },
        },
        handler=_make_code_run_handler(config),
        category="builtin",
        effects=("execute",),
        execute_code_keys=("script", "script_path"),
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

        try:
            timeout = int(args.get("timeout", 60))
        except Exception:
            timeout = 60
        workspace_root = str(config.workspace_dir)
        # 提示词用 ``<workspace>/...`` 之类的可移植前缀，cwd 参数必须能解析
        raw_cwd = expand_tool_path(str(args.get("cwd", "./")), workspace_root)
        cwd = os.path.normpath(os.path.join(workspace_root, raw_cwd))
        maxlen = 10000 // max(args.get("_tool_num", 1), 1)
        stop_signal = getattr(handler, "code_stop_signal", None)

        # 路径前缀在 shell 里也能用：显式导出两个根，避免脚本里硬编码主机路径
        subprocess_env = os.environ.copy()
        subprocess_env["ZA_PROJECT_ROOT"] = project_root()
        subprocess_env["ZA_WORKSPACE_DIR"] = workspace_root
        registry_path = getattr(config, "_desktop_subagent_registry_path", None)
        if registry_path:
            from zero_agent.utils.subagent_registry import (
                PARENT_AGENT_ID_ENV,
                REGISTRY_ENV,
                SESSION_ID_ENV,
            )

            subprocess_env[REGISTRY_ENV] = str(registry_path)
            subprocess_env[SESSION_ID_ENV] = str(
                getattr(config, "_desktop_session_id", "") or ""
            )
            subprocess_env[PARENT_AGENT_ID_ENV] = str(
                getattr(config, "_desktop_agent_id", "") or ""
            )

        try:
            from zero_agent.extensions.sandbox import resolve_mode

            sandbox_mode = resolve_mode(config)
        except Exception:  # noqa: BLE001 - 沙箱模块不可用不应影响执行
            sandbox_mode = ""

        extra_roots = list(getattr(config, "extra_write_roots", []) or [])
        # 项目 temp/ 是 autonomous 助手（TODO.txt / autonomous_reports/）的固定落盘
        # 位置，但它在沙箱可写根（workspace）之外，导致 code_run 里写报告直接 EPERM。
        # 这里把 <project>/temp 作为默认可写根放行（不改 config，保持配置语义简单）。
        try:
            _project_temp = os.path.abspath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "temp")
            )
            os.makedirs(_project_temp, exist_ok=True)
            extra_roots.append(_project_temp)
        except OSError:
            _logger.debug("无法准备项目 temp 可写根: %s", locals().get("_project_temp"), exc_info=True)
        # 去重并保持顺序，避免同一根重复进入 sandbox profile
        extra_roots = list(dict.fromkeys(extra_roots))
        if not _sandbox_active(workspace_root, sandbox_mode, extra_roots):
            # 沙箱没真正生效：退回静态兜底，挡住格式化/写块设备/关机/fork 炸弹
            # 与 workspace 之外的递归删除。沙箱生效时不做这一步，保持命令自由。
            denial = command_denial(
                {"script": code, "script_path": args.get("script_path")},
                workspace_root,
            )
            if denial:
                message = f"已拒绝执行：{denial}"
                yield f"[Status] ERR {message}\n"
                return StepOutcome(
                    {"status": "error", "msg": message},
                    next_prompt="\n",
                    action=StepAction.CONTINUE,
                )

        return (yield from code_run(
            code=code,
            code_type=code_type,
            timeout=timeout,
            cwd=cwd,
            maxlen=maxlen,
            stop_signal=stop_signal,
            env=subprocess_env,
            sandbox_mode=sandbox_mode,
            sandbox_root=workspace_root,
            sandbox_extra_roots=extra_roots,
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
        if code_type not in _VALID_CODE_TYPES:
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
        try:
            resolved_cwd = resolve_workspace_path(
                raw_cwd or "./", config.workspace_dir
            )
        except ValueError as exc:
            return _code_run_error(
                "code_run_invalid_cwd",
                f"[Error] cwd must stay inside workspace: {exc}",
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
        normalized.update(
            {"type": code_type, "timeout": timeout, "cwd": resolved_cwd}
        )
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
