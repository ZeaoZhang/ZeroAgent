"""核心代码签名、重启前校验与进程热重启.

签名只覆盖 ``zero_agent/**/*.py``：这是进程启动时真正加载进内存的代码。
assets / SOP / skill / config.yaml 不参与签名，它们由各自的读取路径生效。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Optional


def _default_root() -> Path:
    """Return the package directory whose modules define the running process."""

    return Path(__file__).resolve().parents[1]


def _signature_files(root: Path) -> list[Path]:
    """Return all core Python files under *root* in stable order."""

    files = [
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    return sorted(files)


def _compute_core_signature(root: Optional[Path] = None) -> str:
    """Hash every core module's path, mtime and size."""

    base = (root or _default_root()).resolve()
    rows: list[str] = []
    for path in _signature_files(base):
        try:
            stat = path.stat()
        except OSError:
            continue
        try:
            relative = path.relative_to(base)
        except ValueError:
            relative = path
        rows.append(f"{relative}|{stat.st_mtime_ns}|{stat.st_size}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


# 进程导入时采样一次：这是「当前进程实际加载的代码」基线。
# 不能在每个 ZeroAgent 构造时重新采样，否则会错过启动后发生的文件变化。
_PROCESS_SIGNATURE = _compute_core_signature()


def core_signature(root: Optional[Path] = None) -> str:
    """Return the current on-disk core signature."""

    return _compute_core_signature(root)


def process_signature() -> str:
    """Return the core signature captured when this process imported the module."""

    return _PROCESS_SIGNATURE


def file_signature(path: Optional[Path]) -> str:
    """Return a cheap change signature for one file (``missing`` when absent)."""

    if path is None:
        return "missing"
    try:
        stat = Path(path).stat()
    except OSError:
        return "missing"
    return hashlib.sha256(
        f"{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8"),
    ).hexdigest()


def validate_core(root: Optional[Path] = None) -> tuple[bool, str]:
    """Validate that the changed core code can be imported before restarting.

    Two cheap layers: ``compile()`` every module (catches syntax errors in
    modules the import smoke test may not touch), then an import smoke test in
    a short-lived subprocess (catches import-time errors).

    Args:
        root: 包目录；默认当前 ``zero_agent`` 包.

    Returns:
        ``(True, "")`` 或 ``(False, 人类可读错误)``.
    """
    base = (root or _default_root()).resolve()
    for path in _signature_files(base):
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            return False, f"{path}:{exc.lineno}: {exc.msg}"
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"{path}: {exc}"
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import zero_agent, zero_agent.core.agent, zero_agent.runners.cli",
            ],
            cwd=str(base.parent),
            env={
                **os.environ,
                "PYTHONPATH": str(base.parent),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-2000:]
        return False, tail or f"import check exited with {result.returncode}"
    return True, ""


def _desktop_fallback_module() -> str:
    """Return the module entry used when no argv fallback is available."""

    main_module = sys.modules.get("__main__")
    filename = getattr(main_module, "__file__", "") or ""
    if Path(filename).name == "desktop_bridge.py":
        return "zero_agent.frontends.desktop_bridge"
    return "zero_agent.runners.cli"


def _restart_argv() -> list[str]:
    """Build the argv used to re-exec the current process.

    Order: the interpreter's original argv (keeps ``-m pkg`` / console-script
    arguments), then this process's argv, then a module fallback.
    """
    orig_argv = list(getattr(sys, "orig_argv", None) or [])
    if orig_argv and Path(orig_argv[0]).name.startswith("python"):
        return [sys.executable, *orig_argv[1:]]
    if sys.argv:
        return [sys.executable, *sys.argv]
    return [sys.executable, "-m", _desktop_fallback_module()]


def clear_bytecode_cache(root: Optional[Path] = None) -> None:
    """Remove core ``__pycache__`` directories before a re-exec.

    CPython validates a cached ``.pyc`` by source mtime (second precision) and
    size. An edit within the same second that keeps the size would otherwise
    make the restarted process load stale bytecode even though the signature
    changed.
    """

    base = (root or _default_root()).resolve()
    for cache_dir in base.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def restart_process() -> NoReturn:
    """Replace the current process with a fresh interpreter (POSIX exec).

    Callers set ``ZA_RESUME_CHECKPOINT`` / ``ZA_HOT_RESTART`` before calling.
    """
    clear_bytecode_cache()
    argv = _restart_argv()
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.flush()
    except Exception:
        pass
    if os.name == "posix":
        os.execv(argv[0], argv)
        raise RuntimeError("os.execv returned")
    subprocess.Popen(
        argv,
        env=os.environ.copy(),
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    os._exit(0)
