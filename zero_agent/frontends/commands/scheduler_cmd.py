"""Scheduler slash command — reflect task & service management.

/scheduler              → list reflect tasks, scheduler tasks, running services
/scheduler start <name>  → start a reflect task as a detached process
/scheduler stop <name>   → stop a running service by name or pid
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from zero_agent.core.config import project_root

# ---------------------------------------------------------------------------
# Paths
_PKG_DIR = Path(__file__).resolve().parents[2]  # zero_agent/ package dir
_ROOT = _PKG_DIR.parent  # repo root (for sche_tasks/)
_REFLECT_DIR = _PKG_DIR / "reflect"
_SCHE_TASKS_DIR = _ROOT / "sche_tasks"
_BOTS_DIR = _PKG_DIR / "bots"

# ---------------------------------------------------------------------------
# Running-service state (psutil-based cmdline scan)
# ---------------------------------------------------------------------------
_RUNNING_CACHE: tuple[float, dict[str, int]] | None = None
_RUNNING_TTL = 2.0


def _invalidate_running_cache() -> None:
    global _RUNNING_CACHE
    _RUNNING_CACHE = None


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _sniff_doc(p: Path) -> str:
    """Best-effort first line of a module docstring."""
    try:
        head = p.read_text(encoding="utf-8", errors="ignore").splitlines()[:40]
        joined = "\n".join(head)
        for q in ('"""', "'''"):
            i = joined.find(q)
            if i != -1:
                j = joined.find(q, i + 3)
                if j != -1:
                    body = joined[i + 3 : j].strip()
                    if body:
                        return body.splitlines()[0].strip()
    except Exception:
        pass
    return ""


def list_reflect_tasks() -> list[dict[str, str]]:
    """Return [{name, path, doc}] for every reflect/*.py task script.

    Excludes __init__.py and non-task utility modules (subagent.py has no
    INTERVAL/ONCE — it is a library, not a runnable reflect task).
    """
    out: list[dict[str, str]] = []
    if not _REFLECT_DIR.is_dir():
        return out
    for p in sorted(_REFLECT_DIR.glob("*.py")):
        if p.name.startswith("_") or p.name == "__init__.py":
            continue
        # Only include files that look like reflect tasks (have INTERVAL)
        try:
            code = p.read_text(encoding="utf-8", errors="ignore")
            if "INTERVAL" not in code:
                continue
        except Exception:
            pass
        out.append({
            "name": p.stem,
            "path": str(p),
            "doc": _sniff_doc(p),
        })
    return out


def list_scheduler_tasks(tasks_dir: str = "") -> list[dict[str, Any]]:
    """Return [{name, path, schedule, enabled}] for sche_tasks/*.json files.

    Args:
        tasks_dir: Override path to sche_tasks dir; empty uses the default.
    """
    out: list[dict[str, Any]] = []
    sd = Path(tasks_dir) if tasks_dir else _SCHE_TASKS_DIR
    if not sd.is_dir():
        return out
    for p in sorted(sd.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        out.append({
            "name": p.stem,
            "path": str(p),
            "schedule": data.get("schedule") or data.get("cron") or data.get("every") or "",
            "enabled": bool(data.get("enabled", True)),
        })
    return out


def list_services() -> list[dict[str, Any]]:
    """Discover launchable services: reflect tasks + bot frontends.

    Returns [{name, cmd, doc, kind}] where name is a hub-style path
    ('reflect/foo.py' or 'bots/bar.py') and cmd is the launch command list.
    """
    out: list[dict[str, Any]] = []

    # Reflect tasks
    if _REFLECT_DIR.is_dir():
        for p in sorted(_REFLECT_DIR.glob("*.py")):
            if p.name.startswith("_") or p.name == "__init__.py":
                continue
            try:
                code = p.read_text(encoding="utf-8", errors="ignore")
                if "INTERVAL" not in code:
                    continue
            except Exception:
                pass
            rel = f"reflect/{p.name}"
            out.append({
                "name": rel,
                "cmd": [sys.executable, "-m", "zero_agent.runners.cli", "--reflect", str(p.resolve())],
                "doc": _sniff_doc(p),
                "kind": "reflect",
            })

    # Bot frontends (*_app.py)
    if _BOTS_DIR.is_dir():
        for p in sorted(_BOTS_DIR.glob("*_app.py")):
            rel = f"bots/{p.name}"
            out.append({
                "name": rel,
                "cmd": [sys.executable, str(_PKG_DIR / rel)],
                "doc": _sniff_doc(p),
                "kind": "bot",
            })

    return out


# ---------------------------------------------------------------------------
# Running-state introspection
# ---------------------------------------------------------------------------

def _match_service(cmdline: list[str], svc: dict[str, Any]) -> bool:
    """Does this OS process belong to `svc`?

    For reflect tasks, match on the reflect/<name>.py path anywhere in cmdline.
    For bot apps, match on bots/<name>.py.
    """
    if not cmdline:
        return False
    name: str = svc["name"]  # e.g. 'reflect/scheduler.py' or 'bots/telegram_app.py'
    name_norm = name.replace("/", os.sep)
    return any(name_norm in (a or "") or name in (a or "") for a in cmdline)


def running_services(use_cache: bool = True) -> dict[str, int]:
    """{service_name: pid} for live services. {} if psutil is missing."""
    global _RUNNING_CACHE
    if use_cache and _RUNNING_CACHE and time.time() - _RUNNING_CACHE[0] < _RUNNING_TTL:
        return dict(_RUNNING_CACHE[1])

    try:
        import psutil  # type: ignore
    except ImportError:
        return {}

    svcs = list_services()
    out: dict[str, int] = {}
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["pid"] == me:
                continue
            nm = (proc.info.get("name") or "").lower()
            if "python" not in nm and "py.exe" not in nm:
                continue
            cmd = proc.cmdline()
        except Exception:
            continue
        for svc in svcs:
            if svc["name"] not in out and _match_service(cmd, svc):
                out[svc["name"]] = int(proc.info["pid"])
                break

    _RUNNING_CACHE = (time.time(), dict(out))
    return out


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------

def start_service(name: str) -> tuple[bool, str]:
    """Launch a service by its hub-style path or reflect stem.

    name can be:
      - 'reflect/scheduler.py' (full hub path)
      - 'bots/telegram_app.py'
      - 'scheduler' (bare reflect stem, resolved to reflect/scheduler.py)
    """
    svcs = list_services()
    svc = next((s for s in svcs if s["name"] == name), None)
    if svc is None:
        # Bare reflect stem fallback
        cand = f"reflect/{name}.py"
        svc = next((s for s in svcs if s["name"] == cand), None)
    if svc is None:
        return False, f"Unknown service: {name}"

    try:
        flags = 0
        if os.name == "nt":
            flags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        proc = subprocess.Popen(
            svc["cmd"],
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
            return False, f"Start failed (exit code {rc}): {svc['name']}"
        _invalidate_running_cache()
        return True, f"Started {svc['name']} (pid={proc.pid})"
    except Exception as exc:
        return False, f"Start failed: {type(exc).__name__}: {exc}"


def stop_service(name: str) -> tuple[bool, str]:
    """Terminate a running service by its hub-style path or bare stem."""
    try:
        import psutil  # type: ignore
    except ImportError:
        return False, "psutil not installed — cannot stop services"

    running = running_services()
    pid = running.get(name)
    if pid is None:
        # Try bare stem resolution
        cand = f"reflect/{name}.py"
        pid = running.get(cand)
        if pid is None:
            return False, f"{name} is not running"

    try:
        parent = psutil.Process(pid)
        kids = parent.children(recursive=True)
        for p in [parent, *kids]:
            try:
                p.terminate()
            except Exception:
                pass
        gone, alive = psutil.wait_procs([parent, *kids], timeout=3.0)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        _invalidate_running_cache()
        return True, f"Stopped {name} (pid={pid})"
    except psutil.NoSuchProcess:
        _invalidate_running_cache()
        return True, f"{name} already exited"
    except Exception as exc:
        return False, f"Stop failed: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

def handle(args: str, agent: Any) -> str:
    """Handle /scheduler commands.

    Args:
        args: Everything after "/scheduler " (may be empty).
        agent: ZeroAgent instance (unused by this handler but accepted for
               consistency with the command interface).

    Returns:
        Display string, or "" when handled via side-effect prints.
    """
    parts = args.strip().split()
    action = parts[0].lower() if parts else ""

    if not action:
        # /scheduler — list status
        lines: list[str] = []

        # Reflect tasks
        reflect_tasks = list_reflect_tasks()
        lines.append("═══ Reflect Tasks ═══")
        if reflect_tasks:
            for t in reflect_tasks:
                doc_suffix = f"  — {t['doc']}" if t.get("doc") else ""
                lines.append(f"  {t['name']}{doc_suffix}")
        else:
            lines.append("  (none found)")

        # Scheduler tasks
        lines.append("")
        lines.append("═══ Scheduler Tasks (sche_tasks/) ═══")
        sched_tasks = list_scheduler_tasks()
        if sched_tasks:
            for t in sched_tasks:
                status = "✓" if t["enabled"] else "✗"
                sched_str = f" @ {t['schedule']}" if t.get("schedule") else ""
                lines.append(f"  [{status}] {t['name']}{sched_str}")
        else:
            lines.append("  (no tasks configured)")

        # Running services
        lines.append("")
        lines.append("═══ Running Services ═══")
        running = running_services()
        if running:
            for name, pid in sorted(running.items()):
                lines.append(f"  {name}  (pid={pid})")
        else:
            lines.append("  (no services running)")

        return "\n".join(lines)

    elif action == "start":
        if len(parts) < 2:
            return "Usage: /scheduler start <name>\n  name = reflect stem (e.g. 'scheduler') or 'bots/telegram_app.py'"
        target = parts[1]
        ok, msg = start_service(target)
        print(f"  {'✓' if ok else '✗'} {msg}")
        return ""

    elif action == "stop":
        if len(parts) < 2:
            return "Usage: /scheduler stop <name>\n  name = reflect stem or service name from 'running services' list"
        target = parts[1]
        ok, msg = stop_service(target)
        print(f"  {'✓' if ok else '✗'} {msg}")
        return ""

    elif action == "help":
        return (
            "/scheduler              — list reflect tasks, scheduler tasks, running services\n"
            "/scheduler start <name>  — start a reflect task (e.g. 'scheduler', 'goal_mode')\n"
            "/scheduler stop <name>   — stop a running service by name"
        )

    else:
        return f"Unknown scheduler sub-command: {action}\nType /scheduler help for usage."
