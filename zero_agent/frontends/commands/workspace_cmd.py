"""Workspace 命令 — junction/symlink-based workspace management for ZeroAgent.

设计要点:
  * 在 <project_root>/temp/projects/<name> 下建指向用户真实项目目录的目录联接
    (Windows mklink /J 或 POSIX symlink),实现 project_mode 插件与 SOP 的文件共享。
  * 维护注册表 temp/workspaces.json(原子写)与 session→workspace 映射。
  * pid 锚 temp/.active_project.<pid> 兼容 project_mode 插件。
  * 命名 name = f"{basename}-{blake2b_hex[:8]}",同一 workspace 恒定同名(幂等复用)。
  * junction 安全:删除用 os.rmdir/os.unlink,绝不递归删目标文件。
  * cleanup 只动已确认是链接且悬空/未注册的条目,真实目录一概不碰。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from zero_agent.core.config import project_root

if TYPE_CHECKING:
    from zero_agent.core.agent import ZeroAgent

# --------------------------------------------------------------------------- #
# 路径基准 (与 plugins/project_mode.py 的 _TEMP 保持一致)
# --------------------------------------------------------------------------- #

def _temp_root() -> Path:
    """Return the temp directory under the project root."""
    return project_root() / "temp"


def _projects_root() -> Path:
    """Directory for workspace junction/symlink entries."""
    return _temp_root() / "projects"


def _anchor_path() -> Path:
    """Activation anchor file, keyed by PID (compatible with project_mode plugin)."""
    return _temp_root() / f".active_project.{os.getpid()}"


def _registry_path() -> Path:
    """Persistent workspace registry JSON file."""
    return _temp_root() / "workspaces.json"


_REGISTRY_VERSION = 1


# --------------------------------------------------------------------------- #
# 命名
# --------------------------------------------------------------------------- #

def _norm_abspath(p: str) -> str:
    """Canonicalize absolute path for hashing: abspath + normcase.

    On Windows, normcase makes case-insensitive paths equivalent.
    Does NOT follow symlinks/junctions (unlike Path.resolve()).
    """
    return os.path.normcase(os.path.abspath(p))


def _ws_name(abs_path: str) -> str:
    """Derive a stable workspace name: basename + blake2b digest prefix.

    Same directory always maps to same name (idempotent reuse).
    """
    base = os.path.basename(abs_path.rstrip("/\\")) or "ws"
    digest = hashlib.blake2b(
        _norm_abspath(abs_path).encode("utf-8")
    ).hexdigest()[:8]
    return f"{base}-{digest}"


def _link_path(name: str) -> Path:
    """Full path of a workspace junction/symlink entry."""
    return _projects_root() / name


# --------------------------------------------------------------------------- #
# junction / symlink 跨平台封装 (reparse 安全)
# --------------------------------------------------------------------------- #

def make_dir_link(target_abs: str, link_path: Path) -> bool:
    """Create a directory junction or symlink.

    Windows uses ``mklink /J`` (no admin required); POSIX uses ``os.symlink``.
    Returns True on success; prints error to stderr and returns False on failure.
    """
    target_abs = os.path.abspath(target_abs)
    parent = link_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"[workspace] mkdir {parent} failed: {e}\n")
        return False

    link_str = str(link_path)
    if os.name == "nt":
        # mklink is a cmd builtin; must invoke via cmd.
        try:
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", link_str, target_abs],
                capture_output=True, text=True,
            )
        except OSError as e:
            sys.stderr.write(f"[workspace] mklink invoke failed: {e}\n")
            return False
        if r.returncode != 0 or not link_path.exists():
            msg = (r.stderr or r.stdout or "").strip()
            sys.stderr.write(f"[workspace] mklink /J failed: {msg}\n")
            return False
        return True

    # POSIX
    try:
        os.symlink(target_abs, link_str, target_is_directory=True)
        return True
    except OSError as e:
        sys.stderr.write(f"[workspace] symlink failed: {e}\n")
        return False


def is_dir_link(path: Path) -> bool:
    """Check if path is a directory junction or symlink.

    Does NOT rely solely on os.path.islink — that returns False for
    Windows junctions. Uses reparse-point attribute detection instead.
    """
    ps = str(path)
    try:
        if os.path.islink(ps):  # POSIX symlink, Windows symbolic link
            return True
    except OSError:
        return False

    if os.name != "nt":
        return False

    try:
        st = os.lstat(ps)
    except OSError:
        return False

    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if not (attrs & reparse):
        return False

    # Check reparse tag: mount point (junction) or symbolic link
    tag = getattr(st, "st_reparse_tag", 0)
    mount = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    syml = getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C)
    if tag:
        return tag in (mount, syml)
    return True  # has reparse attribute but no tag; conservatively treat as link


def link_target(path: Path) -> Optional[str]:
    """Read the link target, stripping Windows ``\\??\\`` / ``\\\\?\\`` prefixes.

    Returns None on failure.
    """
    try:
        t = os.readlink(str(path))
    except OSError:
        return None
    for pre in ("\\??\\", "\\\\?\\"):
        if t.startswith(pre):
            t = t[len(pre):]
            break
    return t


def remove_dir_link(path: Path) -> bool:
    """Remove only the link itself, NEVER recursing into target.

    Windows junctions/symlink-directories use ``os.rmdir``;
    POSIX symlinks use ``os.unlink``.

    Caller MUST confirm via ``is_dir_link`` first.
    """
    try:
        if os.name == "nt":
            os.rmdir(str(path))
        else:
            os.unlink(str(path))
        return True
    except OSError as e:
        sys.stderr.write(f"[workspace] remove link {path} failed: {e}\n")
        return False


# --------------------------------------------------------------------------- #
# 注册表 temp/workspaces.json (原子写)
# --------------------------------------------------------------------------- #

def registry_load() -> dict:
    """Load the workspace registry from the JSON file.

    Returns empty dict if the file is missing or corrupt.
    """
    rp = _registry_path()
    try:
        data = json.loads(rp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and data.get("version") == _REGISTRY_VERSION:
        items = data.get("items")
        if isinstance(items, dict):
            return items
    return {}


def _registry_save(items: dict) -> None:
    """Atomically save registry items via temp-file + replace."""
    rp = _registry_path()
    try:
        rp.parent.mkdir(parents=True, exist_ok=True)
        tmp = rp.parent / f"workspaces.json.{os.getpid()}.tmp"
        tmp.write_text(
            json.dumps(
                {"version": _REGISTRY_VERSION, "items": items},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(rp))
    except OSError as e:
        sys.stderr.write(f"[workspace] registry save failed: {e}\n")



registry_save = _registry_save  # public alias for direct registry writes
def registry_upsert(name: str, abs_path: str) -> None:
    """Insert or update a workspace entry in the registry."""
    items = registry_load()
    items[name] = {"path": os.path.abspath(abs_path), "last_used": int(time.time())}
    _registry_save(items)


def registry_remove(name: str) -> None:
    """Remove a workspace entry from the registry."""
    items = registry_load()
    if items.pop(name, None) is not None:
        _registry_save(items)


def _mem_lines(link: Path) -> int:
    """Count lines in project_memory.md (read through junction). Returns 0 on error."""
    mp = link / "project_memory.md"
    try:
        with open(str(mp), encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def registry_list() -> list[dict]:
    """Return workspace list for picker UI: [{name, path, last_used, mem_lines, dangling}].

    Sorted by last_used descending.
    """
    out: list[dict] = []
    for name, ent in registry_load().items():
        path = (ent or {}).get("path") or ""
        out.append({
            "name": name,
            "path": path,
            "last_used": int((ent or {}).get("last_used") or 0),
            "mem_lines": _mem_lines(_link_path(name)) if path else 0,
            "dangling": not (path and os.path.isdir(path)),
        })
    out.sort(key=lambda x: x["last_used"], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# 会话→工作区映射 temp/session_workspaces.json
# --------------------------------------------------------------------------- #

def _session_map_path() -> Path:
    """Path to the session→workspace mapping file."""
    return _temp_root() / "session_workspaces.json"


def _session_map_load() -> dict:
    """Load session→workspace mapping. Returns empty dict on missing/corrupt."""
    smp = _session_map_path()
    try:
        data = json.loads(smp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and data.get("version") == _REGISTRY_VERSION:
        items = data.get("items")
        if isinstance(items, dict):
            return items
    return {}


def _session_map_save(items: dict) -> None:
    """Atomically save session→workspace mapping."""
    smp = _session_map_path()
    try:
        smp.parent.mkdir(parents=True, exist_ok=True)
        tmp = smp.parent / f"session_workspaces.json.{os.getpid()}.tmp"
        tmp.write_text(
            json.dumps(
                {"version": _REGISTRY_VERSION, "items": items},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(smp))
    except OSError as e:
        sys.stderr.write(f"[workspace] session map save failed: {e}\n")


def session_ws_set(log_path: str, target: str) -> None:
    """Record a session's workspace binding.

    ``target`` = real workspace path; ``""`` means the session is explicitly off.
    """
    key = os.path.basename(log_path or "")
    if not key:
        return
    items = _session_map_load()
    items[key] = target or ""
    _session_map_save(items)


def session_ws_get(log_path: str) -> Optional[str]:
    """Get a session's workspace binding.

    Returns: real path (bound), ``""`` (explicitly off), or None (no record).
    """
    key = os.path.basename(log_path or "")
    return _session_map_load().get(key) if key else None


def session_map_prune() -> None:
    """Remove orphan entries whose log files no longer exist (call once at startup)."""
    items = _session_map_load()
    logdir = _temp_root() / "model_responses"
    alive = {
        k: v for k, v in items.items()
        if (logdir / k).is_file()
    }
    if len(alive) != len(items):
        _session_map_save(alive)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def validate_path(abs_path: str) -> tuple[bool, str]:
    """Validate a workspace candidate path.

    Returns:
        (is_valid, error_message). Error message is empty when valid.
    """
    if not abs_path or not abs_path.strip():
        return False, "路径为空"
    p = abs_path.strip().strip('"').strip("'")
    if not os.path.isabs(p):
        return False, "需要绝对路径"
    if os.name == "nt" and p.startswith("\\\\"):
        return False, "不支持网络路径 (UNC): junction 无法指向网络位置"
    if not os.path.exists(p):
        return False, f"路径不存在: {p}"
    if not os.path.isdir(p):
        return False, "不是目录"
    temp_str = str(_temp_root())
    if _norm_abspath(p).startswith(_norm_abspath(temp_str)):
        return False, "该路径已在 temp 内, 无需 workspace"
    return True, ""


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def prepare(abs_path: str) -> dict:
    """Prepare a workspace without writing the PID activation anchor.

    Flow: validate → name → idempotent link → ensure project_memory.md →
    register → read memory.

    Returns:
        {ok, name, link, target, mem_text, warning, error}.
        TUI multi-session isolation uses this to avoid PID-anchor contention.
    """
    p = abs_path.strip().strip('"').strip("'") if abs_path else ""
    ok, msg = validate_path(p)
    if not ok:
        return {"ok": False, "error": msg}

    target = os.path.abspath(p)
    name = _ws_name(target)
    link = _link_path(name)
    warning = ""

    # Idempotent link creation
    if link.exists(follow_symlinks=False):
        if is_dir_link(link):
            cur = link_target(link)
            if cur and _norm_abspath(cur) == _norm_abspath(target):
                pass  # already points to same target → reuse
            else:
                remove_dir_link(link)
                if not make_dir_link(target, link):
                    return {"ok": False, "error": "重建 junction 失败 (见 stderr)"}
        else:
            # Rare: a real directory with the same name (other UI's project).
            return {
                "ok": False,
                "error": f"{link} 已是真实目录 (可能是其它项目), 拒绝覆盖",
            }
    else:
        if not make_dir_link(target, link):
            return {"ok": False, "error": "创建 junction 失败 (见 stderr)"}

    # Ensure project_memory.md exists (via junction → real repo root)
    mem_path = link / "project_memory.md"
    if not mem_path.is_file():
        try:
            mem_path.touch(exist_ok=True)
        except OSError as e:
            warning = f"无法创建 project_memory.md: {e}"

    mem_text = ""
    try:
        mem_text = mem_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass

    registry_upsert(name, target)

    return {
        "ok": True,
        "name": name,
        "link": str(link),
        "target": target,
        "mem_text": mem_text,
        "warning": warning,
        "error": "",
    }


def activate(abs_path: str) -> dict:
    """Set up and activate a process-level workspace.

    Writes the PID anchor for project_mode plugin detection.
    Kept for legacy SOP / non-multi-session UI usage.
    """
    r = prepare(abs_path)
    if not r.get("ok"):
        return r
    try:
        _anchor_path().write_text(r["name"], encoding="utf-8")
    except OSError as e:
        r = dict(r)
        r.update({"ok": False, "error": f"写激活锚失败: {e}"})
    return r


def deactivate() -> bool:
    """Remove the PID activation anchor only (link + registry preserved).

    Returns:
        True if there was an active workspace to deactivate.
    """
    anchor = _anchor_path()
    if anchor.is_file():
        try:
            anchor.unlink()
            return True
        except OSError as e:
            sys.stderr.write(f"[workspace] deactivate failed: {e}\n")
    return False


def current() -> Optional[dict]:
    """Get the currently active workspace: {name, path}.

    Returns None if no workspace is active.
    """
    anchor = _anchor_path()
    try:
        name = anchor.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name:
        return None
    ent = registry_load().get(name) or {}
    return {"name": name, "path": ent.get("path") or ""}


def is_dangling(name: str) -> bool:
    """Check if a workspace junction points to a removed/inaccessible target."""
    link = _link_path(name)
    if not is_dir_link(link):
        return True
    t = link_target(link)
    return not (t and os.path.isdir(t))


def remove(name: str) -> None:
    """Explicitly unregister: delete junction (NOT real files) + registry entry.

    Also removes the PID anchor if this workspace is currently active.
    """
    link = _link_path(name)
    if is_dir_link(link):
        remove_dir_link(link)
    registry_remove(name)
    cur = current()
    if cur and cur["name"] == name:
        deactivate()


# project_mode 插件注入标记: ``[PROJECT MODE: <name>]``
_PM_RE = re.compile(r"\[PROJECT MODE:\s*([^\]\n]+?)\s*\]")


def workspace_from_log(log_path: str) -> Optional[dict]:
    """Scan a model_responses log for the last active workspace.

    Only returns results for names present in the registry (excludes
    plain SOP projects without hash-suffixed names).

    Returns:
        {name, path} or None.
    """
    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    names = _PM_RE.findall(content)
    if not names:
        return None
    name = names[-1].strip()  # last activation wins
    ent = registry_load().get(name)
    if not ent or not ent.get("path"):
        return None
    return {"name": name, "path": ent["path"]}


def cleanup() -> None:
    """Startup cleanup: remove dangling or unregistered junctions from temp/projects/.

    Safety: only touches entries confirmed as directory links via ``is_dir_link``.
    Real directories (other UI projects) are never touched.
    """
    proot = _projects_root()
    if not proot.is_dir():
        return
    registered = set(registry_load().keys())
    try:
        entries = list(proot.iterdir())
    except OSError:
        return
    for entry in entries:
        if not is_dir_link(entry):
            continue  # real directory → don't touch
        t = link_target(entry)
        dangling = not (t and os.path.isdir(t))
        if dangling or entry.name not in registered:
            remove_dir_link(entry)


# --------------------------------------------------------------------------- #
# Slash command handler
# --------------------------------------------------------------------------- #

def handle(args: str, agent: ZeroAgent) -> str:
    """Handle ``/workspace`` slash commands.

    ``/workspace``              — list registered workspaces
    ``/workspace /path/to/dir`` — activate that workspace (abs path)
    ``/workspace off``          — deactivate current workspace
    ``/workspace rm <name>``    — remove a registered workspace
    """
    args = (args or "").strip()

    if not args:
        # List registered workspaces
        items = registry_list()
        if not items:
            return "暂无已登记 workspace。用 /workspace <绝对路径> 新建/进入。"
        lines: list[str] = []
        for it in items:
            flag = " ⚠失效" if it["dangling"] else ""
            mem = f"{it['mem_lines']}行记忆" if it["mem_lines"] else "空"
            lines.append(f"  {it['name']}{flag}  →  {it['path']}  ({mem})")
        return "\n".join(lines)

    # /workspace off
    if args.lower() == "off":
        cur = current()
        if cur:
            was_name = cur.get("name", "")
            deactivate()
            return f"已退出 workspace「{was_name}」"
        return "当前未处于 workspace 模式。"

    # /workspace rm <name>
    if args.startswith("rm "):
        name = args[3:].strip()
        if not name:
            return "用法: /workspace rm <name>"
        remove(name)
        return f"已注销 workspace「{name}」"

    # /workspace <abs_path>
    r = activate(args)
    if not r.get("ok"):
        return f"❌ workspace 设定失败: {r.get('error')}"
    warning = r.get("warning", "")
    msg = f"✅ 已进入 workspace「{r['name']}」→ {r['target']}"
    if warning:
        msg += f"\n⚠ {warning}"
    return msg


# Command-to-handler mapping for integration with slash_commands.py
COMMANDS = {"/workspace": handle}
