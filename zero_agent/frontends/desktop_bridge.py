#!/usr/bin/env python3
"""
ZeroAgent Web2 Bridge.

Clear split:
1) AgentManager: owns AgentRunner instances, sessions and histories.
2) Transport: HTTP is the command/data channel; WebSocket only pushes small
   session-state notifications.

HTTP API:
  GET    /status
  GET    /channels
  POST   /channels/{channel_id}/start
  POST   /channels/{channel_id}/stop
  POST   /channels/{channel_id}/link
  GET    /channels/{channel_id}/config
  POST   /channels/{channel_id}/config
  POST   /channels/wechat/login
  GET    /channels/wechat/login/{session_id}
  POST   /channels/wechat/login/{session_id}/cancel
  GET    /config
  POST   /config
  GET    /model-profiles
  GET    /slash/commands
  POST   /slash/resolve
  GET    /history/sessions
  POST   /history/resume
  GET    /sessions
  POST   /session/new
  GET    /session/{sid}
  DELETE /session/{sid}
  POST   /session/{sid}/prompt
  GET    /session/{sid}/messages?after=0&limit=200
  POST   /session/{sid}/cancel

WS API:
  GET /ws -> events only, e.g.
  {"type":"session-state","sessionId":"sess-...","state":"running","seq":3,"updatedAt":...}
"""
from __future__ import annotations

import asyncio, contextlib, copy, hmac, importlib, ipaddress, json, os, re, secrets, shutil, signal, sqlite3, sys, urllib.parse, webbrowser
import threading, time, traceback, uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from aiohttp import web, WSMsgType

from zero_agent.runners.agent_runner import AgentRunner
from zero_agent.core.agent import ZeroAgent
from zero_agent.core import checkpoint as checkpoint_store
from zero_agent.core import hot_restart
from zero_agent.core.checkpoint import CONTINUATION_PROMPT
from zero_agent.core.config import AgentConfig, default_config_path, load_default_config
from zero_agent.core.types import TaskMode
from zero_agent.core.localization import PROMPT_CAPABILITY_FILE_DELIVERY
from zero_agent.frontends.plan_command import create_plan_workspace
from zero_agent.bots import channel_control
from zero_agent.bots.channel_config import (
    ChannelConfigError,
    channel_config_snapshot,
    update_channel_config,
)
from zero_agent.frontends.session_store import SessionStore
from zero_agent.utils.subagent_registry import (
    process_is_running,
    read_subagent_events,
)

APP_DIR = Path(__file__).resolve().parent


def find_default_project_root() -> Path:
    candidates = [
        APP_DIR / "..",
        APP_DIR / ".." / "..",
    ]
    for p in candidates:
        root = p.resolve()
        if (root / "pyproject.toml").exists() and (root / "zero_agent").is_dir():
            return root
    return APP_DIR.parent.parent.resolve()


DEFAULT_PROJECT_ROOT = find_default_project_root()

MAX_SESSION_IDEMPOTENCY_RESULTS = 256
SESSION_PERSISTENCE_LOCK = threading.Lock()


def remember_session_result(results: OrderedDict[tuple[str, str], dict], operation: str, session_id: str, result: dict) -> None:
    key = (operation, session_id)
    results[key] = result
    results.move_to_end(key)
    while len(results) > MAX_SESSION_IDEMPOTENCY_RESULTS:
        results.popitem(last=False)


@dataclass(frozen=True)
class BridgeSecurity:
    host: str
    port: int
    token: str
    token_explicit: bool
    allowed_origins: frozenset[str]
    allow_remote: bool


def _is_loopback_host(host: str) -> bool:
    normalized = (host or "").strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _loopback_origins(port: int) -> set[str]:
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }


def _validate_bridge_security(security: BridgeSecurity) -> BridgeSecurity:
    if not security.token:
        raise ValueError("Desktop bridge security token must be non-empty")
    if not _is_loopback_host(security.host) and not (
        security.allow_remote and security.token_explicit
    ):
        raise ValueError(
            "Remote desktop bridge host requires ZA_DESKTOP_BRIDGE_ALLOW_REMOTE=1 "
            "and an explicit non-empty ZA_DESKTOP_BRIDGE_TOKEN"
        )
    return security


def adopt_bridge_token(security: BridgeSecurity) -> None:
    """Keep the bridge token in memory only.

    环境变量会被 ``code_run`` / MCP / 插件拉起的子进程继承，读到它就等于拿到
    bridge 的全部权限；因此启动时读走 token 后立刻从环境删除，只有在热重启
    exec 前一刻才写回（见 :meth:`AgentManager._exec_restart`）。

    Args:
        security: 已解析的 bridge 安全配置.
    """

    manager.bridge_token = security.token
    os.environ.pop("ZA_DESKTOP_BRIDGE_TOKEN", None)


def load_bridge_security(host: str, port: int) -> BridgeSecurity:
    token_env = os.environ.get("ZA_DESKTOP_BRIDGE_TOKEN")
    token = token_env.strip() if token_env is not None else ""
    token_explicit = bool(token)
    if not token:
        token = secrets.token_urlsafe(32)

    allowed_origins = _loopback_origins(port)
    for origin in os.environ.get("ZA_DESKTOP_BRIDGE_ALLOWED_ORIGINS", "").replace("\n", ",").split(","):
        origin = origin.strip().rstrip("/")
        if origin:
            allowed_origins.add(origin)

    allow_remote = os.environ.get("ZA_DESKTOP_BRIDGE_ALLOW_REMOTE") == "1"
    return _validate_bridge_security(BridgeSecurity(
        host=host,
        port=port,
        token=token,
        token_explicit=token_explicit,
        allowed_origins=frozenset(allowed_origins),
        allow_remote=allow_remote,
    ))


def _ancestor_pids() -> Optional[set]:
    """Return the PIDs of this process and all of its ancestors.

    ``None`` means the ancestry could not be determined (no psutil, or the
    platform refuses to expose the parent chain).
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        proc = psutil.Process(os.getpid())
        pids = {proc.pid}
        pids.update(parent.pid for parent in proc.parents())
    except Exception:
        return None
    return pids


def desktop_parent_is_alive(expected_parent_pid: int, *, current_parent_pid: Optional[int] = None) -> bool:
    """Whether the desktop shell that launched us is still running.

    The desktop process is an *ancestor* of this process, not necessarily the
    direct parent: when the bridge is started from a terminal, pytest or any
    other wrapper spawned by the desktop app, ``os.getppid()`` points at the
    wrapper. Comparing a single ppid therefore made those processes conclude
    they were orphaned and SIGTERM themselves a second after startup
    (exit code 143 with no traceback). Walk the whole ancestor chain instead,
    and stay conservative when the chain is unknown.
    """
    if expected_parent_pid <= 1:
        return False
    if current_parent_pid is not None:
        return current_parent_pid == expected_parent_pid
    ancestors = _ancestor_pids()
    if ancestors is None:
        return os.getppid() == expected_parent_pid
    return expected_parent_pid in ancestors


def _windows_parent_handle(parent_pid: int):
    import ctypes

    return ctypes.windll.kernel32.OpenProcess(0x00100000, False, parent_pid)


def _windows_parent_is_alive(handle) -> bool:
    import ctypes

    return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 258


def _close_windows_parent_handle(handle) -> None:
    import ctypes

    ctypes.windll.kernel32.CloseHandle(handle)


def desktop_parent_pid() -> Optional[int]:
    value = os.environ.get("ZA_DESKTOP_PARENT_PID", "").strip()
    try:
        parent_pid = int(value)
    except ValueError:
        return None
    if parent_pid <= 1:
        return None
    ancestors = _ancestor_pids()
    if ancestors is not None and parent_pid not in ancestors:
        # The variable leaked into an unrelated process (e.g. a subprocess of the
        # desktop shell). Arming the monitor here would make that process kill
        # itself as soon as it looks at its own parent chain.
        return None
    return parent_pid


async def monitor_desktop_parent(parent_pid: int) -> None:
    if os.name == "nt":
        handle = _windows_parent_handle(parent_pid)
        try:
            while handle and _windows_parent_is_alive(handle):
                await asyncio.sleep(0.1)
        finally:
            if handle:
                _close_windows_parent_handle(handle)
    else:
        while desktop_parent_is_alive(parent_pid):
            await asyncio.sleep(0.1)
    os.kill(os.getpid(), signal.SIGTERM)

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Agent management layer
# ---------------------------------------------------------------------------

_DEFAULT_TOKEN_USAGE: Dict[str, Any] = {
    "input": 0,
    "output": 0,
    "total": 0,
    "limit": 200000,
    "cacheRead": 0,
    "cacheCreation": 0,
    "cacheMiss": 0,
    "cacheHitRate": 0.0,
    "cacheMetricsAvailable": False,
}


def _normalize_token_usage(value: Any) -> Dict[str, Any]:
    """Return typed desktop token usage, tolerating old or malformed payloads."""
    raw = value if isinstance(value, dict) else {}
    result = dict(_DEFAULT_TOKEN_USAGE)
    integer_fields = ("input", "output", "total", "limit", "cacheRead", "cacheCreation", "cacheMiss")
    for key in integer_fields:
        try:
            parsed = int(raw.get(key, result[key]))
        except (TypeError, ValueError, OverflowError):
            continue
        if parsed >= 0:
            result[key] = parsed
    try:
        rate = float(raw.get("cacheHitRate", result["cacheHitRate"]))
        if rate >= 0:
            result["cacheHitRate"] = rate
    except (TypeError, ValueError, OverflowError):
        pass
    if isinstance(raw.get("cacheMetricsAvailable"), bool):
        result["cacheMetricsAvailable"] = raw["cacheMetricsAvailable"]
    return result


@dataclass
class Session:
    id: str
    title: str = "New chat"
    cwd: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: List[dict] = field(default_factory=list)
    msg_seq: int = 0
    partial: Optional[dict] = None
    status: str = "idle"  # idle|running|waiting|error|cancelled
    agent: Any = None
    thread: Optional[threading.Thread] = None
    pending_turns: List[dict] = field(default_factory=list)
    last_error: str = ""
    terminal_status: str = ""
    terminal_reason: str = ""
    # New fields for frontend redesign
    model_override: Optional[str] = None  # Override model for this session
    token_usage: Dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_TOKEN_USAGE))
    group_id: Optional[str] = None  # Session group ID
    log_path: Optional[str] = None
    sub_agents: List[Dict[str, Any]] = field(default_factory=list)
    agent_turn_token: str = ""
    restore_history: bool = False
    plan_path: Optional[str] = None
    plan_status: str = "inactive"  # inactive|planning|ready|executing|failed|cancelled
    plan_task: str = ""


_PLAN_STATUSES = {"inactive", "planning", "ready", "executing", "failed", "cancelled"}

_SESSION_STATE_FIELDS = (
    "title",
    "cwd",
    "created_at",
    "updated_at",
    "messages",
    "msg_seq",
    "partial",
    "status",
    "last_error",
    "terminal_status",
    "terminal_reason",
    "model_override",
    "token_usage",
    "group_id",
    "sub_agents",
    "agent_turn_token",
    "plan_path",
    "plan_status",
    "plan_task",
)


@dataclass
class SessionGroup:
    id: str
    name: str
    created_at: float = field(default_factory=time.time)
    position: int = 0

_DESKTOP_SESSION_ID_RE = re.compile(r"^sess-[0-9a-f]{12}$")
_QUEUE_KEY_PREFIX = "pending_turns:"
_ACTIVE_TURN_KEY_PREFIX = "active_turn:"


def _desktop_log_path(sessions_dir: str, sid: str) -> str:
    """Return the owned response-log path for one generated desktop ID."""
    if not _DESKTOP_SESSION_ID_RE.fullmatch(sid):
        raise ValueError(f"invalid desktop session id: {sid}")
    return str(Path(sessions_dir).expanduser().resolve() / f"model_responses_session_{sid}.txt")


def _resolve_runtime_path(path: str | os.PathLike[str] | None) -> str:
    if not path:
        return ""
    return str(Path(path).expanduser().resolve())


# ---------------------------------------------------------------------------
# Session persistence (SQLite) so conversations survive app restarts.
# ---------------------------------------------------------------------------

def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or block.get("content") or "").strip()
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "input_text"}
        ).strip()
    return ""


def session_has_user_message(session_or_messages: Session | list[dict]) -> bool:
    messages = getattr(session_or_messages, "messages", session_or_messages)
    return any(
        isinstance(message, dict)
        and message.get("role") == "user"
        and bool(_message_text(message.get("content")))
        for message in (messages or [])
    )


def _clone_agent_records(records: list[dict]) -> list[dict]:
    return [copy.deepcopy(agent) for agent in records]


def _normalize_sub_agents(value: Any) -> List[Dict[str, Any]]:
    agents: List[Dict[str, Any]] = []
    for raw in value or []:
        if not isinstance(raw, dict):
            continue
        agent = copy.deepcopy(raw)
        if agent.get("status") in {"running", "waiting"}:
            if agent.get("type") == "subagent" and process_is_running(agent.get("pid")):
                pass
            else:
                agent["status"] = "failed" if agent.get("type") == "subagent" else "cancelled"
                agent["updated_at"] = time.time()
                agent["reason"] = (
                    "process_exited_without_final_event"
                    if agent.get("type") == "subagent"
                    else "bridge_restarted"
                )
        agents.append(agent)
    return agents

def _session_to_persistable(sess: Session) -> dict:
    return {
        "id": sess.id,
        "title": sess.title,
        "cwd": sess.cwd,
        "created_at": sess.created_at,
        "updated_at": sess.updated_at,
        "messages": copy.deepcopy(sess.messages),
        "msg_seq": sess.msg_seq,
        "log_path": sess.log_path,
        "status": "idle",
        "last_error": sess.last_error,
        "terminal_status": sess.terminal_status,
        "terminal_reason": sess.terminal_reason,
        "model_override": sess.model_override,
        "token_usage": _normalize_token_usage(sess.token_usage),
        "group_id": sess.group_id,
        "sub_agents": _clone_agent_records(sess.sub_agents),
        "planPath": sess.plan_path,
        "planStatus": sess.plan_status,
        "planTask": sess.plan_task,
    }


def _session_to_store_dict(sess: Session) -> dict:
    """Return only durable session fields using the SQLite store names."""
    return {
        "id": sess.id,
        "title": sess.title,
        "cwd": sess.cwd,
        "created_at": sess.created_at,
        "updated_at": sess.updated_at,
        "msg_seq": sess.msg_seq,
        "last_error": sess.last_error,
        "terminal_status": sess.terminal_status,
        "terminal_reason": sess.terminal_reason,
        "model_override": sess.model_override,
        "token_usage": _normalize_token_usage(sess.token_usage),
        "group_id": sess.group_id,
        "sub_agents": _clone_agent_records(sess.sub_agents),
        "plan_path": sess.plan_path,
        "plan_status": sess.plan_status,
        "plan_task": sess.plan_task,
    }


def _message_to_store_dict(message: dict) -> dict:
    return copy.deepcopy(message)


def _session_from_persisted(
    data: dict,
    sessions_dir: str | None = None,
) -> Session:
    sid = str(data.get("id") or ("sess-" + uuid.uuid4().hex[:12]))
    owned_path = _desktop_log_path(sessions_dir, sid) if sessions_dir and _DESKTOP_SESSION_ID_RE.fullmatch(sid) else None
    plan_status = str(data.get("planStatus", data.get("plan_status")) or "inactive")
    if plan_status not in _PLAN_STATUSES:
        plan_status = "inactive"
    sess = Session(
        id=sid,
        title=str(data.get("title") or "New chat"),
        cwd=str(data.get("cwd") or ""),
        created_at=float(data.get("created_at") or time.time()),
        updated_at=float(data.get("updated_at") or time.time()),
        msg_seq=int(data.get("msg_seq") or 0),
        status="idle",
        last_error=str(data.get("last_error") or ""),
        terminal_status=str(data.get("terminal_status") or ""),
        terminal_reason=str(data.get("terminal_reason") or ""),
        model_override=data.get("model_override"),
        log_path=owned_path,
        group_id=data.get("group_id"),
        sub_agents=_normalize_sub_agents(data.get("sub_agents")),
        token_usage=_normalize_token_usage(data.get("token_usage")),
        plan_path=data.get("planPath", data.get("plan_path")),
        plan_status=plan_status,
        plan_task=str(data.get("planTask", data.get("plan_task")) or ""),
    )
    sess.messages = list(data.get("messages") or [])
    # A stored msg_seq below MAX(message_id) would make the next append reuse a
    # primary key; trust whichever source is higher.
    stored_max = 0
    for message in sess.messages:
        try:
            stored_max = max(stored_max, int(message.get("id") or 0))
        except (TypeError, ValueError):
            continue
    sess.msg_seq = max(int(sess.msg_seq or 0), stored_max)
    sess.restore_history = bool(sess.messages)
    return sess
def _public_config_snapshot(config: AgentConfig) -> Dict[str, Any]:
    return {
        "default_backend": config.default_backend,
        "max_turns": config.max_turns,
        "workspace_dir": config.workspace_dir,
        "memory_dir": config.memory_dir,
        "sessions_dir": config.sessions_dir,
        "log_dir": config.log_dir,
        "language": config.language,
        "verbose": config.verbose,
        "incremental_output": config.incremental_output,
        "failover_backends": list(config.failover_backends),
        "litellm_model_cost_map": config.litellm_model_cost_map,
        "llm_backends": {
            name: {
                "provider": backend.provider,
                "api_base": backend.api_base,
                "model": backend.model,
            }
            for name, backend in config.llm_backends.items()
        },
    }


class AgentManager:
    def __init__(self):
        self.lock = threading.RLock()
        self._hot_restart_lock = threading.RLock()
        self._hot_restarting = False
        # 重新 exec 后要继续服务同一个前端，token 必须跨重启保持一致；
        # 它只存在于内存，不写环境（见 __main__）。
        self.bridge_token = ""
        base_config = self._load_base_config()
        self.workspace_dir = _resolve_runtime_path(base_config.workspace_dir)
        self.memory_dir = _resolve_runtime_path(base_config.memory_dir)
        self.sessions_dir = _resolve_runtime_path(base_config.sessions_dir)
        self.config_path = str(getattr(base_config, "_source_path", default_config_path()))
        self.config: Dict[str, Any] = _public_config_snapshot(base_config)
        self.sessions: Dict[str, Session] = {}
        self.active_session_id: Optional[str] = None
        # The desktop model selection is inherited by newly-created sessions.
        # Existing sessions retain their own override.
        self.current_model_override: Optional[str] = None
        self.session_idempotency_results: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._subagent_registry_offsets: Dict[str, int] = {}
        self._subagent_monitor_thread: Optional[threading.Thread] = None
        self.groups: OrderedDict[str, SessionGroup] = OrderedDict()
        self.session_store = SessionStore(
            os.path.join(self.sessions_dir, "sessions.sqlite3")
        )
        self.session_store.initialize()
        self._load_persisted_groups()
        self._load_persisted_sessions()
        if self.active_session_id in self.sessions:
            self.current_model_override = self.sessions[
                self.active_session_id
            ].model_override
        self._last_persisted_state = self._capture_session_state()
        if any(
            agent.get("type") == "subagent" and agent.get("status") in {"running", "waiting"}
            for sess in self.sessions.values()
            for agent in sess.sub_agents
        ):
            self._ensure_subagent_monitor()

    def _persist_groups(self, *, raise_on_error: bool = False) -> None:
        """Persist group entities to SQLite."""
        try:
            with self.lock, SESSION_PERSISTENCE_LOCK:
                for group in self.groups.values():
                    self.session_store.save_group({
                        "id": group.id,
                        "name": group.name,
                        "created_at": group.created_at,
                        "position": group.position,
                    })
        except Exception as exc:
            print(f"persist groups failed: {exc}", file=sys.stderr)
            if raise_on_error:
                raise

    def _load_persisted_groups(self) -> None:
        try:
            group_entries = self.session_store.load_groups()
        except Exception as exc:
            print(f"load persisted groups failed: {exc}", file=sys.stderr)
            return
        for entry in group_entries:
            try:
                g = SessionGroup(
                    id=str(entry["id"]),
                    name=str(entry.get("name", entry["id"])),
                    created_at=float(entry.get("created_at", time.time())),
                    position=int(entry.get("position", 0)),
                )
                self.groups[g.id] = g
            except Exception as exc:
                print(f"skip broken persisted group: {exc}", file=sys.stderr)

    def _owned_desktop_logs(self) -> set[str]:
        """Return absolute paths currently owned by persisted desktop sessions."""
        return {
            os.path.abspath(sess.log_path)
            for sess in self.sessions.values()
            if sess.log_path and _DESKTOP_SESSION_ID_RE.fullmatch(sess.id)
        }
    def _newest_session_id(self) -> Optional[str]:
        if not self.sessions:
            return None
        return max(
            self.sessions.values(),
            key=lambda sess: (float(sess.updated_at or 0), sess.id),
        ).id

    def _capture_session_state(self) -> dict[str, dict]:
        return {
            sid: {
                field: copy.deepcopy(getattr(sess, field))
                for field in _SESSION_STATE_FIELDS
            }
            for sid, sess in self.sessions.items()
        }

    def _restore_session_state(self, state: dict[str, dict]) -> None:
        for sid, values in state.items():
            sess = self.sessions.get(sid)
            if sess is None:
                continue
            for field, value in values.items():
                setattr(sess, field, copy.deepcopy(value))

    def _persist_pending_turns(
        self, sess: Session, *, raise_on_error: bool = False
    ) -> None:
        """Mirror the in-memory queue so a restart can report lost turns.

        Args:
            sess: 会话.
            raise_on_error: True 时把存储失败抛给调用者，用于「接口已承诺
                入队」的路径；内部清理路径保持只记日志。

        Raises:
            Exception: 仅当 ``raise_on_error`` 为 True 且写入失败.
        """
        key = f"{_QUEUE_KEY_PREFIX}{sess.id}"
        try:
            if sess.pending_turns:
                self.session_store.set_app_state(
                    key, json.dumps(sess.pending_turns, ensure_ascii=False)
                )
            else:
                self.session_store.delete_app_state(key)
        except Exception as exc:
            print(f"persist pending turns failed: {exc}", file=sys.stderr)
            if raise_on_error:
                raise

    def _flush_partial_assistant(self, sess: Session) -> None:
        """Persist the streamed text of an in-flight turn, best effort.

        Hot restart aborts the runner and execs a new process shortly after, so
        the cancelled terminal usually never reaches ``_drain_turn``; without
        this flush the text the user was reading disappears from the UI.
        """
        partial_text = str((sess.partial or {}).get("content") or "")
        sess.partial = None
        if partial_text.strip():
            with contextlib.suppress(Exception):
                self.add_message(sess, "assistant", partial_text)

    @staticmethod
    def _last_message_is(sess: Session, role: str, content: str) -> bool:
        """Return whether the newest message already is this system notice."""
        if not sess.messages:
            return False
        last = sess.messages[-1]
        return (
            str(last.get("role") or "") == role
            and str(last.get("content") or "") == content
        )

    def _mark_turn_active(self, sess: Session, active: bool) -> None:
        """Track whether a turn is in flight, so restarts do not lie."""
        key = f"{_ACTIVE_TURN_KEY_PREFIX}{sess.id}"
        try:
            if active:
                self.session_store.set_app_state(key, "1")
            else:
                self.session_store.delete_app_state(key)
        except Exception as exc:
            print(f"mark turn active failed: {exc}", file=sys.stderr)

    def _persist_sessions(self, *, raise_on_error: bool = True) -> None:
        """Persist non-empty in-memory sessions to SQLite."""
        try:
            with self.lock, SESSION_PERSISTENCE_LOCK:
                persisted = [
                    sess for sess in self.sessions.values()
                    if session_has_user_message(sess)
                ]
                persisted_ids = {sess.id for sess in persisted}
                active_id = (
                    self.active_session_id
                    if self.active_session_id in persisted_ids
                    else (persisted[-1].id if persisted else None)
                )
                self.session_store.sync_sessions(
                    [_session_to_store_dict(sess) for sess in persisted],
                    active_id,
                )
                self._last_persisted_state = self._capture_session_state()
        except Exception as exc:
            self._restore_session_state(self._last_persisted_state)
            print(f"persist sessions failed: {exc}", file=sys.stderr)
            if raise_on_error:
                raise

    def _load_persisted_sessions(self) -> None:
        try:
            state = self.session_store.load_state()
        except Exception as exc:
            print(f"load persisted sessions failed: {exc}", file=sys.stderr)
            return

        for data in (state.get("sessions") or {}).values():
            try:
                sess = _session_from_persisted(data, self.sessions_dir)
                if not session_has_user_message(sess):
                    continue
                self.sessions[sess.id] = sess
            except Exception as exc:
                print(f"skip broken persisted session: {exc}", file=sys.stderr)

        active = state.get("active_session_id")
        if active and active in self.sessions:
            self.active_session_id = active
        elif self.sessions:
            self.active_session_id = next(iter(self.sessions))
        else:
            self.active_session_id = None

        self._restore_interrupted_turns()

    def _restore_interrupted_turns(self) -> None:
        """Report turns the previous process never finished, then clear markers."""
        for sess in list(self.sessions.values()):
            try:
                queue_key = f"{_QUEUE_KEY_PREFIX}{sess.id}"
                active_key = f"{_ACTIVE_TURN_KEY_PREFIX}{sess.id}"
                try:
                    raw_queue = self.session_store.get_app_state(queue_key)
                except Exception:
                    raw_queue = None
                queued: list = []
                if raw_queue:
                    try:
                        parsed = json.loads(raw_queue)
                        queued = parsed if isinstance(parsed, list) else []
                    except (TypeError, ValueError):
                        queued = []
                if queued:
                    started = {
                        str(message.get("queue_id"))
                        for message in sess.messages
                        if message.get("queue_id")
                    }
                    for item in queued:
                        queue_id = str((item or {}).get("queue_id") or "")
                        if queue_id and queue_id in started:
                            # Already promoted into a real turn before the crash.
                            continue
                        prompt_text = str((item or {}).get("prompt") or "").strip()
                        if prompt_text:
                            self.add_message(
                                sess, "system",
                                f"未执行（服务重启）：{prompt_text}",
                                kind="queue_interrupted",
                            )
                    self.session_store.delete_app_state(queue_key)
                try:
                    active_marker = self.session_store.get_app_state(active_key)
                except Exception:
                    active_marker = None
                if active_marker:
                    sess.terminal_status = "interrupted"
                    sess.terminal_reason = "bridge_restart"
                    if sess.plan_status in {"planning", "executing"}:
                        sess.plan_status = "failed"
                    # When a checkpoint survives, resume_restarted_sessions will
                    # continue the task; the "please resend" note would then
                    # contradict the resume note right below it.
                    resumable = False
                    try:
                        cp = checkpoint_store.load(
                            checkpoint_store.checkpoint_path(self.sessions_dir, sess.id)
                        )
                        resumable = (
                            bool(cp)
                            and cp.get("source") == "desktop"
                            and cp.get("status") in {"running", "waiting"}
                        )
                    except Exception:
                        resumable = False
                    if not resumable:
                        self.add_message(
                            sess, "system",
                            "上次任务在服务重启时中断，请重新发送。",
                            kind="turn_interrupted",
                        )
                    self.session_store.delete_app_state(active_key)
            except Exception as exc:
                print(f"restore interrupted turns failed: {exc}", file=sys.stderr)



    def _load_base_config(self) -> AgentConfig:
        try:
            return load_default_config()
        except Exception as exc:
            print(f"load default config failed: {exc}", file=sys.stderr)
            return AgentConfig(
                workspace_dir=str(DEFAULT_PROJECT_ROOT),
                sessions_dir=str(DEFAULT_PROJECT_ROOT / "workspace" / "sessions"),
            )

    def ensure_project_import_path(self) -> Path:
        root = Path(DEFAULT_PROJECT_ROOT).resolve()
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        return root

    def make_agent(self, sess: Session):
        self.ensure_project_import_path()
        old_cwd = os.getcwd()
        try:
            os.chdir(sess.cwd or self.workspace_dir)
            config = copy.deepcopy(load_default_config())
            config.workspace_dir = _resolve_runtime_path(sess.cwd or config.workspace_dir)
            config.sessions_dir = _resolve_runtime_path(config.sessions_dir)
            if sess.model_override:
                config.default_backend = sess.model_override
            config._desktop_session_id = sess.id
            config._checkpoint_session_id = sess.id
            config._checkpoint_source = "desktop"
            if not sess.log_path and _DESKTOP_SESSION_ID_RE.fullmatch(sess.id):
                sess.log_path = _desktop_log_path(self.sessions_dir, sess.id)
            agent = ZeroAgent(config=config, session_log_path=sess.log_path)
            set_hook = getattr(agent, "set_hot_restart_hook", None)
            if callable(set_hook):
                set_hook(self.request_hot_restart)
            if sess.restore_history:
                history = [
                    {"role": message["role"], "content": message["content"]}
                    for message in sess.messages
                    if message.get("role") in {"user", "assistant", "tool"}
                    and isinstance(message.get("content"), str)
                    and message["content"].strip()
                ]
                if history:
                    agent.client.history = history
                    # The first message after a reload continues that
                    # conversation; do not compact it away.
                    agent._skip_new_task_compaction_once = True
                sess.restore_history = False
            effective_path = getattr(getattr(agent, "client", None), "log_path", None)
            if effective_path:
                sess.log_path = effective_path
            self._persist_sessions()
            return AgentRunner(agent)
        finally:
            with contextlib.suppress(Exception):
                os.chdir(old_cwd)

    # ---- 核心热重启 / 检查点恢复 ---------------------------------------

    @staticmethod
    def _pending_waiting_payload(pending: Any) -> tuple[str, list[str]]:
        """Return ``(question, candidates)`` from a restored waiting state."""

        if pending is None:
            return "Waiting for user input", []
        return pending.waiting_prompt()

    def request_hot_restart(self, reason: str, reset_budget: bool = False) -> None:
        """Request a process hot restart on the bridge event loop."""

        loop = getattr(hub, "loop", None)
        if (
            loop is not None
            and not loop.is_closed()
            and threading.current_thread() is not threading.main_thread()
        ):
            try:
                loop.call_soon_threadsafe(
                    lambda: self._perform_hot_restart(reason, reset_budget),
                )
                return
            except RuntimeError:
                pass
        self._perform_hot_restart(reason, reset_budget)

    def _perform_hot_restart(self, reason: str, reset_budget: bool) -> None:
        """Checkpoint running sessions, close runtimes, then exec a new process."""

        with self._hot_restart_lock:
            if self._hot_restarting:
                return
            self._hot_restarting = True
        try:
            hub.emit({"type": "bridge-log", "payload": f"hot restart: {reason}"})
            closed_ids: list[str] = []
            with self.lock:
                for sess in list(self.sessions.values()):
                    runner = sess.agent
                    za = getattr(runner, "_agent", None)
                    if za is None:
                        continue
                    if sess.status == "running":
                        # exec happens 0.3s after abort(): the cancelled terminal
                        # never reaches _drain_turn, so persist what the user has
                        # already seen before the process is replaced.
                        self._flush_partial_assistant(sess)
                        if not za.restart_checkpoint_armed:
                            with contextlib.suppress(Exception):
                                za.save_checkpoint(
                                    status="running",
                                    restart=True,
                                    reset_budget=reset_budget,
                                )
                        elif reset_budget:
                            # 手动重启也要重置预算，即使自动检查点已写好
                            with contextlib.suppress(Exception):
                                za.save_checkpoint(
                                    status="running",
                                    reset_budget=True,
                                )
                        with contextlib.suppress(Exception):
                            za.abort()
                    closed_ids.append(sess.id)
                for sid in closed_ids:
                    sess = self.sessions.get(sid)
                    za = getattr(getattr(sess, "agent", None), "_agent", None)
                    if za is not None:
                        with contextlib.suppress(Exception):
                            za.close()
            with contextlib.suppress(Exception):
                self._persist_sessions()

            os.environ["ZA_HOT_RESTART"] = "1"
            os.environ["ZA_DESKTOP_BRIDGE_NO_BROWSER"] = "1"

            def _exec() -> None:
                try:
                    self._exec_restart()
                except Exception as exc:  # noqa: BLE001 - exec 失败必须恢复可服务状态
                    with self._hot_restart_lock:
                        self._hot_restarting = False
                    hub.emit({
                        "type": "bridge-log",
                        "payload": f"hot restart exec failed: {exc}",
                    })
                    print(f"hot restart exec failed: {exc}", file=sys.stderr)
                    self._recover_after_failed_restart(closed_ids)

            loop = getattr(hub, "loop", None)
            if loop is not None and not loop.is_closed():
                loop.call_later(0.3, _exec)
            else:
                time.sleep(0.3)
                _exec()
        except Exception as exc:  # noqa: BLE001 - 失败必须恢复可服务状态
            with self._hot_restart_lock:
                self._hot_restarting = False
            hub.emit({"type": "bridge-log", "payload": f"hot restart failed: {exc}"})
            print(f"hot restart failed: {exc}", file=sys.stderr)
            self._recover_after_failed_restart([])

    def _exec_restart(self) -> None:
        """Close the session store, hand the token to the new process, exec.

        token 只在 exec 前的这一瞬间进环境：新进程启动时读走并立刻清除，
        工具 / MCP / ``code_run`` 子进程因此看不到它。

        Raises:
            Exception: exec 本身失败（调用方负责恢复服务）.
        """

        with contextlib.suppress(Exception):
            self.session_store.close()
        token = getattr(self, "bridge_token", "")
        if token:
            os.environ["ZA_DESKTOP_BRIDGE_TOKEN"] = token
        hot_restart.restart_process()

    def _recover_after_failed_restart(self, session_ids: list[str]) -> None:
        """Rebuild closed session runtimes after a restart that never exec'd.

        进程没被替换时，之前 close() 掉的运行时必须重建，否则会话指向已关闭的
        agent，bridge 名义上还在服务、实际已经不能跑任务。

        Args:
            session_ids: 本次被关闭的会话；为空表示按 sessions 全量兜底.
        """
        checkpoints: dict[str, dict[str, Any]] = {}
        try:
            for _path, data in checkpoint_store.list_checkpoints(self.sessions_dir):
                if data.get("source") == "desktop":
                    checkpoints[str(data.get("session_id") or "")] = data
        except Exception as exc:  # noqa: BLE001 - 恢复失败不得再抛
            print(f"load checkpoints failed: {exc}", file=sys.stderr)

        targets = session_ids or [sess.id for sess in list(self.sessions.values())]
        for sid in targets:
            sess = self.sessions.get(sid)
            if sess is None:
                continue
            try:
                with self.lock:
                    sess.agent = self.make_agent(sess)
                    za = getattr(sess.agent, "_agent", sess.agent)
                    data = checkpoints.get(sid)
                    restored = bool(data) and za.restore_checkpoint(data)
                    if restored:
                        sess.status = "waiting"
                        note = (
                            "内核热重启失败，已保持原进程运行；"
                            "任务已从检查点恢复，发送任意消息即可继续。"
                        )
                    else:
                        note = "内核热重启失败，已保持原进程运行。"
                    sess.terminal_status = ""
                    sess.terminal_reason = ""
                    self.add_message(sess, "system", note)
                    self._persist_sessions()
                emit_session_state(sess, sess.status)
            except Exception as exc:  # noqa: BLE001 - 单会话失败不影响其他
                print(f"hot restart recovery failed for {sid}: {exc}", file=sys.stderr)

    def resume_restarted_sessions(self) -> None:
        """Resume desktop sessions whose checkpoints survived a hot restart."""

        try:
            entries = checkpoint_store.list_checkpoints(self.sessions_dir)
        except Exception as exc:  # noqa: BLE001 - 恢复失败不得阻断启动
            print(f"load checkpoints failed: {exc}", file=sys.stderr)
            return
        print(f"[resume] checkpoints found: {len(entries)}", file=sys.stderr)
        for path, data in entries:
            try:
                if data.get("source") != "desktop":
                    continue
                sid = str(data.get("session_id") or "")
                with self.lock:
                    sess = self.sessions.get(sid)
                if sess is None:
                    print(f"[resume] skip {sid}: session no longer exists", file=sys.stderr)
                    with contextlib.suppress(OSError):
                        path.unlink()
                    continue
                if checkpoint_store.restart_budget_exhausted(data):
                    with self.lock:
                        sess.status = "error"
                        sess.terminal_status = "failed"
                        sess.terminal_reason = "hot_restart_loop"
                        self.add_message(
                            sess,
                            "system",
                            f"内核 {int(checkpoint_store.RESTART_BUDGET_WINDOW // 60)}"
                            f" 分钟内已热重启 {checkpoint_store.RESTART_BUDGET_LIMIT} 次，"
                            "疑似重启循环；已停止自动续跑，请检查核心代码后重试。",
                        )
                        self._persist_sessions()
                    with contextlib.suppress(OSError):
                        path.unlink()
                    emit_session_state(sess, "error")
                    continue

                should_resume = False
                with self.lock:
                    sess.agent = self.make_agent(sess)
                    za = getattr(sess.agent, "_agent", sess.agent)
                    if not za.restore_checkpoint(data):
                        print(
                            f"[resume] skip {sid}: checkpoint could not be restored",
                            file=sys.stderr,
                        )
                        with contextlib.suppress(OSError):
                            path.unlink()
                        continue
                    pending = getattr(za, "pending_task_state", None)
                    waiting_kind = getattr(pending, "waiting_kind", "")
                    if data.get("status") == "running" or (
                        data.get("status") == "waiting"
                        and waiting_kind == "restart"
                    ):
                        sess.status = "running"
                        sess.last_error = ""
                        sess.terminal_status = ""
                        sess.terminal_reason = ""
                        resume_note = "内核已热重启，任务从检查点继续。"
                        if not self._last_message_is(sess, "system", resume_note):
                            self.add_message(sess, "system", resume_note)
                        # The resumed turn streams through the same
                        # /session/{sid}/messages `partial` channel as a normal
                        # turn. Without it, _drain_turn drops every chunk (it
                        # only writes into an existing partial) and the UI shows
                        # nothing until the terminal message lands at the end.
                        sess.partial = {
                            "id": sess.msg_seq + 1,
                            "role": "assistant",
                            "content": "",
                            "ts": time.time(),
                            "partial": True,
                        }
                        should_resume = True
                    else:
                        sess.status = "waiting"
                        question, candidates = self._pending_waiting_payload(pending)
                        self.add_message(
                            sess,
                            "system",
                            question,
                            kind="input_required",
                            candidates=candidates,
                        )
                    self._persist_sessions()
                emit_session_state(sess, "running" if should_resume else "waiting")
                if should_resume:
                    print(f"[resume] starting continuation turn for {sess.id}", file=sys.stderr)
                    with self.lock:
                        self._mark_turn_active(sess, True)
                    threading.Thread(
                        target=self.run_agent_turn,
                        args=(sess, CONTINUATION_PROMPT),
                        daemon=True,
                        name=f"Resume-{sess.id}",
                    ).start()
            except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响其他
                print(
                    f"resume checkpoint {path} failed: {exc}\n{traceback.format_exc()}",
                    file=sys.stderr,
                )

    def _subagent_registry_path(self, sess: Session) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", sess.id)[:100] or "session"
        registry_dir = Path(self.sessions_dir) / ".subagents"
        return str(registry_dir / f"{safe_id}.jsonl")

    def _configure_subagent_tracking(self, agent: Any, sess: Session) -> bool:
        runner_agent = getattr(agent, "_agent", agent)
        config = getattr(runner_agent, "config", None)
        if config is None:
            return False
        registry_path = self._subagent_registry_path(sess)
        Path(registry_path).parent.mkdir(parents=True, exist_ok=True)
        config._desktop_subagent_registry_path = registry_path
        config._desktop_session_id = sess.id
        config._desktop_agent_id = f"session:{sess.id}"
        return True

    def _ensure_subagent_monitor(self) -> None:
        with self.lock:
            if self._subagent_monitor_thread and self._subagent_monitor_thread.is_alive():
                return
            for sess in self.sessions.values():
                if sess.id in self._subagent_registry_offsets:
                    continue
                try:
                    size = os.path.getsize(self._subagent_registry_path(sess))
                except OSError:
                    size = 0
                self._subagent_registry_offsets[sess.id] = size
            self._subagent_monitor_thread = threading.Thread(
                target=self._monitor_subagent_registries,
                name="DesktopSubagentMonitor",
                daemon=True,
            )
            self._subagent_monitor_thread.start()

    def _monitor_subagent_registries(self) -> None:
        while True:
            with self.lock:
                sessions = list(self.sessions.values())
            for sess in sessions:
                registry_path = self._subagent_registry_path(sess)
                offset = self._subagent_registry_offsets.get(sess.id, 0)
                events, next_offset = read_subagent_events(registry_path, offset)
                self._subagent_registry_offsets[sess.id] = next_offset
                changed = False
                with self.lock:
                    if self.sessions.get(sess.id) is not sess:
                        continue
                    for event in events:
                        if event.get("type") != "subagent" or not event.get("id"):
                            continue
                        event_id = str(event["id"])
                        try:
                            event_time = float(event.get("updated_at") or event.get("created_at") or 0)
                        except (TypeError, ValueError):
                            continue
                        existing = next(
                            (record for record in sess.sub_agents if record.get("id") == event_id),
                            None,
                        )
                        if existing is not None:
                            try:
                                existing_time = float(existing.get("updated_at") or 0)
                            except (TypeError, ValueError):
                                existing_time = 0
                            if existing_time > event_time:
                                continue
                            existing.update(copy.deepcopy(event))
                        else:
                            record = copy.deepcopy(event)
                            if sess.sub_agents and sess.sub_agents[0].get("type") == "session-agent":
                                sess.sub_agents.insert(1, record)
                            else:
                                sess.sub_agents.insert(0, record)
                        changed = True

                    for record in sess.sub_agents:
                        if (
                            record.get("type") == "subagent"
                            and record.get("status") in {"running", "waiting"}
                            and not process_is_running(record.get("pid"))
                        ):
                            record["status"] = "failed"
                            record["updated_at"] = time.time()
                            record["reason"] = "process_exited_without_final_event"
                            changed = True
                    if changed:
                        sess.updated_at = time.time()
                        self._persist_sessions()
                if changed:
                    emit_session_state(sess, sess.status)
            time.sleep(0.5)
    def list_model_profiles(self):
        self.ensure_project_import_path()
        try:
            runner = AgentRunner(ZeroAgent())
            return runner.list_llm_profiles()
        except Exception as e:
            print(f"get model profiles failed: {e}", file=sys.stderr)
        return []

    @staticmethod
    def _pending_turns_payload(sess: Session) -> list[dict]:
        """Serialize prompts queued behind the running turn for the UI."""
        out = []
        for item in sess.pending_turns:
            text = str(item.get("display_content") or item.get("prompt") or "").strip()
            if not text:
                continue
            out.append({
                "queueId": str(item.get("queue_id") or ""),
                "content": text,
                "imageIds": list(item.get("image_ids") or []),
            })
        return out

    def snapshot(self, sess: Session, include_messages: bool = True) -> dict:
        with self.lock:
            out = {
                "sessionId": sess.id,
                "id": sess.id,
                "title": sess.title,
                "cwd": sess.cwd,
                "status": sess.status,
                "terminalStatus": sess.terminal_status,
                "reason": sess.terminal_reason,
                "createdAt": sess.created_at,
                "updatedAt": sess.updated_at,
                "lastError": sess.last_error,
                "modelOverride": sess.model_override,
                "tokenUsage": copy.deepcopy(sess.token_usage),
                "groupId": sess.group_id,
                "subAgents": _clone_agent_records(sess.sub_agents),
                "planPath": sess.plan_path,
                "planStatus": sess.plan_status,
                "planTask": sess.plan_task,
                "pendingTurns": self._pending_turns_payload(sess),
            }
            if include_messages:
                out["messages"] = copy.deepcopy(sess.messages)
                out["partial"] = copy.deepcopy(sess.partial) if sess.partial else None
            return out

    @staticmethod
    def _highest_message_id(sess: Session) -> int:
        """Return the highest message id currently held in memory."""
        highest = int(sess.msg_seq or 0)
        for message in sess.messages:
            try:
                highest = max(highest, int(message.get("id") or 0))
            except (TypeError, ValueError):
                continue
        return highest

    def add_message(self, sess: Session, role: str, content: str, **extra) -> dict:
        """Append one UI message and persist it, never reusing an id.

        ``msg_seq`` can lag behind SQLite after a history restore or a crashed
        writer; blindly bumping it then collides with the primary key and makes
        every prompt fail with a 500. Allocate from the highest id seen in
        memory, and if SQLite still disagrees, re-derive from the store and
        retry once before giving up.
        """
        previous = (
            sess.msg_seq,
            len(sess.messages),
            sess.title,
            sess.updated_at,
        )
        was_valid = session_has_user_message(sess)
        sess.msg_seq = self._highest_message_id(sess) + 1
        msg = {"id": sess.msg_seq, "role": role, "content": content, "ts": time.time()}
        msg.update(extra)
        sess.messages.append(msg)
        sess.updated_at = time.time()
        if role == "user" and content.strip() and sess.title == "New chat":
            sess.title = content.strip().replace("\n", " ")[:40]

        def persist() -> None:
            if was_valid:
                self.session_store.append_message(
                    _session_to_store_dict(sess),
                    _message_to_store_dict(msg),
                    active_session_id=self.active_session_id,
                )
            else:
                self.session_store.persist_session(
                    _session_to_store_dict(sess),
                    [_message_to_store_dict(message) for message in sess.messages],
                    active_session_id=self.active_session_id,
                )
            self._last_persisted_state = self._capture_session_state()

        if session_has_user_message(sess):
            try:
                persist()
            except sqlite3.IntegrityError:
                stored_max = self.session_store.max_message_id(sess.id)
                recovered_id = max(stored_max, self._highest_message_id(sess)) + 1
                if recovered_id == msg["id"]:
                    sess.msg_seq, message_count, sess.title, sess.updated_at = previous
                    del sess.messages[message_count:]
                    raise
                # The store knew about ids this process never saw: move past
                # them instead of failing the whole turn.
                sess.msg_seq = recovered_id
                msg["id"] = recovered_id
                sess.updated_at = time.time()
                try:
                    persist()
                except Exception:
                    sess.msg_seq, message_count, sess.title, sess.updated_at = previous
                    del sess.messages[message_count:]
                    raise
            except Exception:
                sess.msg_seq, message_count, sess.title, sess.updated_at = previous
                del sess.messages[message_count:]
                raise
        return msg
    def create_session(
        self,
        cwd: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> Session:
        sid = "sess-" + uuid.uuid4().hex[:12]
        os.makedirs(self.sessions_dir, exist_ok=True)
        selected_model = (
            str(model_override).strip()
            if model_override is not None and str(model_override).strip()
            else self.current_model_override
        )
        sess = Session(
            id=sid,
            cwd=str(cwd or self.workspace_dir),
            model_override=selected_model,
            log_path=_desktop_log_path(self.sessions_dir, sid),
        )
        with self.lock:
            self.sessions[sid] = sess
            self.active_session_id = sid
            if selected_model:
                self.current_model_override = selected_model
        emit_session_state(sess, "created")
        return sess

    def get_session(self, sid: str) -> Session:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            return sess

    def _owned_response_log_path(self, sess: Session) -> str | None:
        if not _DESKTOP_SESSION_ID_RE.fullmatch(sess.id):
            return None
        owned_path = _desktop_log_path(self.sessions_dir, sess.id)
        if not sess.log_path or os.path.abspath(sess.log_path) != owned_path:
            return None
        return owned_path

    def _detach_response_log(self, sess: Session) -> str | None:
        """Detach an owned response log after its session mutation commits."""
        owned_path = self._owned_response_log_path(sess)
        if owned_path is None:
            return None
        runner = sess.agent
        agent = getattr(runner, "_agent", None)
        if agent is not None:
            close_agent_log = getattr(agent, "close_response_log", None)
            if callable(close_agent_log):
                with contextlib.suppress(Exception):
                    close_agent_log()
        client = getattr(agent, "client", None)
        if client is not None:
            close = getattr(client, "close_response_log", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
            else:
                with contextlib.suppress(Exception):
                    client.log_path = None
        return owned_path

    def list_groups(self) -> List[dict]:
        with self.lock:
            result = []
            for g in sorted(self.groups.values(), key=lambda g: g.position):
                session_ids = [
                    sid for sid, sess in self.sessions.items()
                    if sess.group_id == g.id
                ]
                result.append({
                    "id": g.id,
                    "name": g.name,
                    "createdAt": g.created_at,
                    "position": g.position,
                    "sessionIds": session_ids,
                })
            return result

    def create_group(self, name: str) -> dict:
        trimmed = str(name or "").strip()
        if not trimmed:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "group name is required"}, ensure_ascii=False),
                content_type="application/json",
            )
        with self.lock:
            for existing in self.groups.values():
                if existing.name == trimmed:
                    raise web.HTTPBadRequest(
                        text=json.dumps({"error": f"group '{trimmed}' already exists"}, ensure_ascii=False),
                        content_type="application/json",
                    )
            gid = "group-" + uuid.uuid4().hex[:12]
            max_pos = max((g.position for g in self.groups.values()), default=-1)
            group = SessionGroup(id=gid, name=trimmed, position=max_pos + 1)
            self.groups[gid] = group
            try:
                self._persist_groups(raise_on_error=True)
            except Exception:
                self.groups.pop(gid, None)
                raise
        return {
            "id": gid,
            "name": trimmed,
            "createdAt": group.created_at,
            "position": group.position,
            "sessionIds": [],
        }

    def delete_group(self, group_id: str) -> dict:
        gid = str(group_id or "").strip()
        with self.lock:
            group = self.groups.get(gid)
            if not group:
                raise web.HTTPNotFound(
                    text=json.dumps({"error": f"group not found: {gid}"}, ensure_ascii=False),
                    content_type="application/json",
                )
            affected: List[str] = []
            previous: dict[str, tuple[Optional[str], float]] = {}
            updated_at_by_session: dict[str, float] = {}
            for sid, sess in list(self.sessions.items()):
                if sess.group_id == gid:
                    previous[sid] = (sess.group_id, sess.updated_at)
                    sess.group_id = None
                    sess.updated_at = time.time()
                    affected.append(sid)
                    updated_at_by_session[sid] = sess.updated_at
            try:
                self.session_store.delete_group_and_unassign_sessions(
                    gid,
                    updated_at_by_session,
                )
                self.groups.pop(gid)
                self._last_persisted_state = self._capture_session_state()
            except Exception:
                for sid, (group_id, updated_at) in previous.items():
                    sess = self.sessions.get(sid)
                    if sess:
                        sess.group_id = group_id
                        sess.updated_at = updated_at
                raise
        for sid in affected:
            sess = self.sessions.get(sid)
            if sess:
                emit_session_state(sess, "group-changed")
        return {"ok": True, "groupId": gid, "sessionIds": affected}

    def set_session_group(self, sid: str, group_id: str | None) -> dict:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(
                    text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False),
                    content_type="application/json",
                )
            normalized = str(group_id).strip() if group_id else None
            if normalized and normalized not in self.groups:
                raise web.HTTPNotFound(
                    text=json.dumps({"error": f"group not found: {normalized}"}, ensure_ascii=False),
                    content_type="application/json",
                )
            previous_group_id = sess.group_id
            previous_updated_at = sess.updated_at
            sess.group_id = normalized or None
            sess.updated_at = time.time()
            try:
                self._persist_sessions(raise_on_error=True)
            except Exception:
                sess.group_id = previous_group_id
                sess.updated_at = previous_updated_at
                raise
            result = {"ok": True, "sessionId": sid, "groupId": sess.group_id}
        emit_session_state(sess, "group-changed")
        return result

    def _profile_memory_manager(self):
        from zero_agent.memory.manager import MemoryManager

        mm = MemoryManager(
            memory_dir=self.memory_dir,
            workspace_dir=self.workspace_dir,
            language=str(self.config.get("language") or "auto"),
        )
        mm.init_memory()
        return mm

    def memory_profile(self) -> dict:
        """Return the injected user profile and its path."""
        mm = self._profile_memory_manager()
        return {"profile": mm.read_user_profile(), "path": mm.profile_path}

    def save_memory_profile(self, profile: str) -> dict:
        """Replace the user profile file and return the stored text."""
        mm = self._profile_memory_manager()
        mm.write_user_profile(profile)
        return {"profile": mm.read_user_profile(), "path": mm.profile_path}

    def delete_session(self, sid: str) -> dict:
        with self.lock:
            key = ("delete", sid)
            prior_result = self.session_idempotency_results.get(key)
            if prior_result is not None:
                self.session_idempotency_results.move_to_end(key)
                return prior_result
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            owned_path = self._owned_response_log_path(sess)
            if sess.agent and hasattr(sess.agent, "abort"):
                with contextlib.suppress(Exception):
                    sess.agent.abort()
            # 会话被删除/重置后不再复用该 agent：释放 MCP 子进程与扩展注册
            if sess.agent and hasattr(sess.agent, "close"):
                with contextlib.suppress(Exception):
                    sess.agent.close()
            checkpoint_store.clear(self.sessions_dir, sid)
            previous_active = self.active_session_id
            was_active = previous_active == sid
            self.sessions.pop(sid)
            if was_active:
                self.active_session_id = self._newest_session_id()
            persisted_active = self.active_session_id
            if (
                persisted_active is not None
                and persisted_active in self.sessions
                and not session_has_user_message(self.sessions[persisted_active])
            ):
                persisted_active = None
            try:
                self.session_store.delete_session(
                    sid,
                    active_session_id=persisted_active,
                )
                self.session_store.delete_app_state(f"{_QUEUE_KEY_PREFIX}{sid}")
                self.session_store.delete_app_state(f"{_ACTIVE_TURN_KEY_PREFIX}{sid}")
            except Exception:
                self.sessions[sid] = sess
                self.active_session_id = previous_active
                raise
            if owned_path:
                self._detach_response_log(sess)
                with contextlib.suppress(OSError):
                    if os.path.isfile(owned_path):
                        os.remove(owned_path)
            result = {"ok": True, "sessionId": sid}
            remember_session_result(self.session_idempotency_results, "delete", sid, result)
        emit_session_state(sess, "closed")
        return result

    def replace_session(self, sid: str) -> dict:
        with self.lock:
            key = ("replace", sid)
            prior_result = self.session_idempotency_results.get(key)
            if prior_result is not None:
                self.session_idempotency_results.move_to_end(key)
                return prior_result

            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(
                    text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False),
                    content_type="application/json",
                )

            owned_path = self._owned_response_log_path(sess)
            if sess.agent and hasattr(sess.agent, "abort"):
                with contextlib.suppress(Exception):
                    sess.agent.abort()
            # 会话被删除/重置后不再复用该 agent：释放 MCP 子进程与扩展注册
            if sess.agent and hasattr(sess.agent, "close"):
                with contextlib.suppress(Exception):
                    sess.agent.close()
            checkpoint_store.clear(self.sessions_dir, sid)
            previous_active = self.active_session_id
            replacement_id = "sess-" + uuid.uuid4().hex[:12]
            replacement = Session(
                id=replacement_id,
                cwd=sess.cwd,
                model_override=sess.model_override or self.current_model_override,
                log_path=_desktop_log_path(self.sessions_dir, replacement_id),
            )
            was_active = previous_active == sid
            self.sessions.pop(sid)
            if was_active:
                self.active_session_id = self._newest_session_id()
            persisted_active = self.active_session_id
            if (
                persisted_active is not None
                and persisted_active in self.sessions
                and not session_has_user_message(self.sessions[persisted_active])
            ):
                persisted_active = None
            try:
                self.session_store.delete_session(
                    sid,
                    active_session_id=persisted_active,
                )
                self.sessions[replacement.id] = replacement
                if was_active:
                    self.active_session_id = replacement.id
            except Exception:
                self.sessions[sid] = sess
                self.active_session_id = previous_active
                raise
            if owned_path:
                self._detach_response_log(sess)
                with contextlib.suppress(OSError):
                    if os.path.isfile(owned_path):
                        os.remove(owned_path)
            result = {
                "ok": True,
                "replacedSessionId": sid,
                "sessionId": replacement.id,
                "session": self.snapshot(replacement),
            }
            remember_session_result(self.session_idempotency_results, "replace", sid, result)

        emit_session_state(sess, "closed")
        emit_session_state(replacement, "created")
        return result

    def list_resume_sessions(self, limit: int = 10) -> list[dict]:
        self.ensure_project_import_path()
        continue_cmd = importlib.import_module("zero_agent.bots.shared.continue_cmd")
        continue_cmd.set_sessions_dir(self.sessions_dir)
        owned = self._owned_desktop_logs()
        sessions = [
            row for row in continue_cmd.list_sessions(exclude_pid=os.getpid())
            if os.path.abspath(row[0]) not in owned
        ]
        out: list[dict] = []
        for idx, (path, mtime, preview, rounds) in enumerate(sessions[:limit], 1):
            out.append({
                "index": idx,
                "path": path,
                "mtime": mtime,
                "preview": preview,
                "rounds": rounds,
                "name": os.path.basename(path),
            })
        return out

    def resume_history(self, sid: str, index: int) -> dict:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            if sess.status == "running":
                raise web.HTTPConflict(text=json.dumps({"error": "session is already running"}, ensure_ascii=False), content_type="application/json")
        self.ensure_project_import_path()
        continue_cmd = importlib.import_module("zero_agent.bots.shared.continue_cmd")
        continue_cmd.set_sessions_dir(self.sessions_dir)
        sessions = [
            row for row in continue_cmd.list_sessions(exclude_pid=os.getpid())
            if os.path.abspath(row[0]) not in self._owned_desktop_logs()
        ]
        target_idx = index - 1
        if not (0 <= target_idx < len(sessions)):
            return {"ok": False, "error": f"索引越界（有效范围 1-{len(sessions)}）"}
        path = sessions[target_idx][0]

        with self.lock:
            if self.sessions.get(sess.id) is not sess:
                raise web.HTTPNotFound(
                    text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False),
                    content_type="application/json",
                )
            if sess.agent is None:
                sess.agent = self.make_agent(sess)
            runner = sess.agent
            history_snapshot = None
            snapshot_history = getattr(runner, "history_snapshot", None)
            if callable(snapshot_history):
                history_snapshot = snapshot_history()
        summary, full = continue_cmd.restore(runner, path)
        runner_agent = getattr(runner, "_agent", runner)
        if full and hasattr(runner_agent, "_skip_new_task_compaction_once"):
            # continue_cmd restored a real history: keep it on the next task.
            runner_agent._skip_new_task_compaction_once = True
        ui_messages = continue_cmd.extract_ui_messages(path)

        with self.lock:
            old_state = {
                "messages": sess.messages,
                "msg_seq": sess.msg_seq,
                "partial": sess.partial,
                "status": sess.status,
                "last_error": sess.last_error,
                "terminal_status": sess.terminal_status,
                "terminal_reason": sess.terminal_reason,
                "title": sess.title,
                "updated_at": sess.updated_at,
            }
            try:
                restored_messages = []
                for msg in ui_messages:
                    restored_messages.append({
                        "id": len(restored_messages) + 1,
                        "role": str(msg.get("role") or "assistant"),
                        "content": str(msg.get("content") or ""),
                        "ts": time.time(),
                    })
                if summary:
                    restored_messages.append({
                        "id": len(restored_messages) + 1,
                        "role": "system",
                        "content": str(summary),
                        "ts": time.time(),
                    })
                sess.messages = restored_messages
                sess.msg_seq = len(restored_messages)
                sess.partial = None
                sess.status = "idle"
                sess.last_error = ""
                sess.terminal_status = ""
                sess.terminal_reason = ""
                if ui_messages:
                    first_user = next((m for m in ui_messages if m.get("role") == "user"), None)
                    if first_user:
                        sess.title = str(first_user.get("content") or "Restored").replace("\n", " ")[:40]
                sess.updated_at = time.time()
                if session_has_user_message(sess):
                    self.session_store.persist_session(
                        _session_to_store_dict(sess),
                        [_message_to_store_dict(message) for message in sess.messages],
                        active_session_id=self.active_session_id,
                    )
                elif self.active_session_id == sess.id:
                    remaining = [
                        candidate for candidate in self.sessions.values()
                        if candidate is not sess and session_has_user_message(candidate)
                    ]
                    fallback = max(
                        remaining,
                        key=lambda candidate: (candidate.updated_at, candidate.id),
                        default=None,
                    )
                    self.session_store.delete_session(
                        sess.id,
                        active_session_id=fallback.id if fallback else None,
                    )
                else:
                    self.session_store.delete_session(sess.id)
                self._last_persisted_state = self._capture_session_state()
            except Exception:
                sess.messages = old_state["messages"]
                sess.msg_seq = old_state["msg_seq"]
                sess.partial = old_state["partial"]
                sess.status = old_state["status"]
                sess.last_error = old_state["last_error"]
                sess.terminal_status = old_state["terminal_status"]
                sess.terminal_reason = old_state["terminal_reason"]
                sess.title = old_state["title"]
                sess.updated_at = old_state["updated_at"]
                restore_history = getattr(runner, "replace_history", None)
                if history_snapshot is not None and callable(restore_history):
                    restore_history(history_snapshot)
                raise

        emit_session_state(sess, "resumed")
        return {
            "ok": True,
            "sessionId": sess.id,
            "path": path,
            "message": summary,
            "full": full,
            "messages": list(sess.messages),
            "session": self.snapshot(sess),
        }

    def submit_prompt(
        self,
        sid: str,
        prompt: Any,
        images: Optional[list] = None,
        display_text: Optional[str] = None,
        *,
        task_mode: TaskMode = TaskMode.OPEN,
        plan_path: Optional[str] = None,
    ) -> dict:
        prompt, image_ids = normalize_prompt(prompt, images)
        visible_text = str(display_text if display_text is not None else prompt).strip()
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            if sess.status == "running":
                queued = {
                    "queue_id": uuid.uuid4().hex,
                    "prompt": prompt,
                    "display_content": visible_text,
                    "image_ids": image_ids,
                }
                sess.pending_turns.append(queued)
                try:
                    self._persist_pending_turns(sess, raise_on_error=True)
                except Exception:
                    sess.pending_turns.pop()
                    raise web.HTTPInternalServerError(
                        text=json.dumps(
                            {"error": "failed to persist the queued prompt"},
                            ensure_ascii=False,
                        ),
                        content_type="application/json",
                    )
                sess.updated_at = time.time()
                return {
                    "ok": True,
                    "sessionId": sid,
                    "accepted": True,
                    "queued": True,
                    "queueId": queued["queue_id"],
                    "queuePosition": len(sess.pending_turns),
                }
            extra = {}
            if image_ids:
                extra["image_ids"] = image_ids
            user_msg = self.add_message(sess, "user", prompt, **extra)
            sess.status = "running"
            sess.last_error = ""
            sess.terminal_status = ""
            sess.terminal_reason = ""
            sess.partial = {"id": sess.msg_seq + 1, "role": "assistant", "content": "", "ts": time.time(), "partial": True}
            t = threading.Thread(
                target=self.run_agent_turn,
                args=(sess, prompt, None),
                kwargs={"task_mode": task_mode, "plan_path": plan_path},
                daemon=True,
                name=f"Turn-{sid}",
            )
            sess.thread = t
            self._mark_turn_active(sess, True)
            t.start()
            seq = sess.msg_seq
        return {"ok": True, "sessionId": sid, "accepted": True, "queued": False, "userMessageId": user_msg["id"], "seq": seq}

    def start_plan(self, sid: str, task: str) -> dict:
        """Create a plan workspace and submit the first PLAN task atomically."""
        task_text = str(task or "").strip()
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            if not task_text:
                raise web.HTTPBadRequest(text=json.dumps({"error": "task is required"}, ensure_ascii=False), content_type="application/json")
            if sess.status == "running":
                raise web.HTTPConflict(text=json.dumps({"error": "session is already running"}, ensure_ascii=False), content_type="application/json")
            if sess.plan_status in {"planning", "ready", "executing"}:
                raise web.HTTPConflict(
                    text=json.dumps({
                        "error": "session already has an active plan",
                        "planPath": sess.plan_path,
                        "planStatus": sess.plan_status,
                        "planTask": sess.plan_task,
                    }, ensure_ascii=False),
                    content_type="application/json",
                )

            workspace = create_plan_workspace(sess.cwd or self.workspace_dir, task_text)
            prior = (sess.plan_path, sess.plan_status, sess.plan_task)
            sess.plan_task = task_text
            sess.plan_path = workspace.path
            sess.plan_status = "planning"

            try:
                submitted = self._submit_turn(sess, task_text, None, TaskMode.PLAN, workspace.path)
            except Exception:
                sess.plan_path, sess.plan_status, sess.plan_task = prior
                with contextlib.suppress(OSError):
                    shutil.rmtree(workspace.directory, ignore_errors=True)
                self._persist_sessions()
                raise
            if submitted is None:
                sess.plan_path, sess.plan_status, sess.plan_task = prior
                with contextlib.suppress(OSError):
                    shutil.rmtree(workspace.directory, ignore_errors=True)
                raise web.HTTPConflict(text=json.dumps({"error": "session is no longer available"}, ensure_ascii=False), content_type="application/json")
            agent, display_q, token = submitted

            user_msg = self.add_message(sess, "user", task_text)
            sess.status = "running"
            sess.last_error = ""
            sess.terminal_status = ""
            sess.terminal_reason = ""
            sess.partial = {"id": sess.msg_seq + 1, "role": "assistant", "content": "", "ts": time.time(), "partial": True}
            sess.thread = threading.Thread(target=self._run_submitted_turn, args=(sess, agent, display_q, token), daemon=True, name=f"Plan-{sid}")
            self._mark_turn_active(sess, True)
            sess.thread.start()
            seq = sess.msg_seq
            self._persist_sessions()
        emit_session_state(sess, "planning")
        return {
            "ok": True,
            "sessionId": sid,
            "planPath": workspace.path,
            "planTask": task_text,
            "accepted": True,
            "userMessageId": user_msg["id"],
            "seq": seq,
        }

    def execute_plan(self, sid: str) -> dict:
        """Submit the preserved plan task with TaskMode.EXECUTING."""
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            if sess.plan_status != "ready":
                raise web.HTTPConflict(text=json.dumps({"error": "plan is not ready"}, ensure_ascii=False), content_type="application/json")
            if not sess.plan_path or not os.path.isfile(sess.plan_path):
                raise web.HTTPConflict(text=json.dumps({"error": "plan file is missing"}, ensure_ascii=False), content_type="application/json")
            if sess.status == "running":
                raise web.HTTPConflict(text=json.dumps({"error": "session is already running"}, ensure_ascii=False), content_type="application/json")

            task = sess.plan_task or ""
            plan_path = sess.plan_path
            sess.plan_status = "executing"
            try:
                submitted = self._submit_turn(sess, task, None, TaskMode.EXECUTING, plan_path)
            except Exception:
                sess.plan_status = "ready"
                raise
            if submitted is None:
                sess.plan_status = "ready"
                raise web.HTTPConflict(text=json.dumps({"error": "session is no longer available"}, ensure_ascii=False), content_type="application/json")
            agent, display_q, token = submitted

            sess.status = "running"
            sess.last_error = ""
            sess.terminal_status = ""
            sess.terminal_reason = ""
            sess.partial = {"id": sess.msg_seq + 1, "role": "assistant", "content": "", "ts": time.time(), "partial": True}
            sess.thread = threading.Thread(target=self._run_submitted_turn, args=(sess, agent, display_q, token), daemon=True, name=f"Execute-{sid}")
            self._mark_turn_active(sess, True)
            sess.thread.start()
            seq = sess.msg_seq
            self._persist_sessions()
        emit_session_state(sess, "executing")
        return {"ok": True, "sessionId": sid, "planPath": plan_path, "planTask": task, "accepted": True, "seq": seq}

    def _sync_token_usage(self, sess: Session) -> None:
        """Refresh session token/context usage from the live runner if available."""
        runner = sess.agent
        za = getattr(runner, "_agent", None)
        client = getattr(za, "client", None)
        if client is None:
            return
        active = getattr(client, "_active", client)
        stats = getattr(client, "usage_stats", None) or getattr(active, "usage_stats", {}) or {}
        try:
            last_prompt = int(stats.get("last_prompt_tokens", 0) or 0)
        except (TypeError, ValueError):
            last_prompt = 0
        if last_prompt > 0:
            current_context = last_prompt
        else:
            # Fallback for sessions that have not reported usage yet: estimate
            # from the surviving history without serializing it on every chunk.
            history = getattr(active, "history", getattr(client, "history", [])) or []
            system = getattr(active, "system", getattr(client, "system", "")) or ""
            try:
                history_chars = len(json.dumps(history, ensure_ascii=False, default=str)) + len(str(system))
                current_context = max(history_chars // 3, 0)
            except Exception:
                current_context = 0
        limit = int(
            getattr(active, "_context_window", 0)
            or getattr(getattr(active, "config", None), "context_window", 0)
            or getattr(getattr(client, "config", None), "context_window", 200000)
            or 200000
        )
        sess.token_usage = _normalize_token_usage({
            "input": stats.get("total_input_tokens", 0),
            "output": stats.get("total_output_tokens", 0),
            "total": current_context,
            "limit": limit,
            "cacheRead": stats.get("total_cache_read_tokens", 0),
            "cacheCreation": stats.get("total_cache_creation_tokens", 0),
            "cacheMiss": stats.get("total_cache_miss_tokens", 0),
            "cacheHitRate": stats.get("cache_hit_rate", 0.0),
            "cacheMetricsAvailable": stats.get("cache_metrics_available", False),
        })

    @staticmethod
    def _live_task_mode(runner: Any) -> Optional[TaskMode]:
        """Return the wrapped agent's live handler contract mode, if any."""
        za = getattr(runner, "_agent", None)
        handler = getattr(za, "handler", None)
        contract = getattr(handler, "task_contract", None)
        return getattr(contract, "mode", None)

    def _register_agent_locked(self, sess: Session) -> tuple[str, dict]:
        token = uuid.uuid4().hex
        now = time.time()
        record = {
            "id": f"agent-{uuid.uuid4().hex[:12]}",
            "name": "ZeroAgent",
            "type": "session-agent",
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "reason": "",
        }
        sess.agent_turn_token = token
        child_agents = [
            item for item in sess.sub_agents
            if item.get("type") == "subagent"
        ]
        running_children = [
            item for item in child_agents
            if item.get("status") in {"running", "waiting"}
        ]
        completed_children = [
            item for item in child_agents
            if item.get("status") not in {"running", "waiting"}
        ]
        sess.sub_agents[:] = [record, *running_children, *completed_children[:50]]
        return token, record

    def _update_agent_locked(self, sess: Session, token: str, status: str, reason: str = "") -> Optional[dict]:
        if token != sess.agent_turn_token or not sess.sub_agents:
            return None
        record = sess.sub_agents[0]
        record["status"] = status
        record["updated_at"] = time.time()
        record["reason"] = reason
        return record

    def _turn_is_current_locked(self, sess: Session, token: str) -> bool:
        return token == sess.agent_turn_token and bool(sess.sub_agents)

    def _submit_turn(
        self,
        sess: Session,
        prompt: str,
        images: Optional[list],
        task_mode: TaskMode,
        plan_path: Optional[str],
    ):
        """Synchronously ensure a runner and submit one task."""
        with self.lock:
            if self.sessions.get(sess.id) is not sess:
                return None
            token: Optional[str] = None
            try:
                if sess.agent is None:
                    sess.agent = self.make_agent(sess)
                agent = sess.agent
                if self._configure_subagent_tracking(agent, sess):
                    self._ensure_subagent_monitor()
                # A runner may have been created by an earlier turn. Apply a
                # newly selected model before reusing that runner.
                desired_backend = sess.model_override or self.config.get(
                    "default_backend",
                )
                za = getattr(agent, "_agent", None)
                switch_backend = getattr(za, "switch_backend", None)
                if za is not None and desired_backend and callable(switch_backend):
                    active_backend = getattr(
                        za,
                        "_get_active_backend_name",
                        lambda: None,
                    )()
                    if active_backend != desired_backend:
                        switch_backend(str(desired_backend))
                if self.sessions.get(sess.id) is not sess:
                    return None
                if za is not None and getattr(za, "pending_task_state", None) is None:
                    # 普通进程重启后，用户的下一条消息应答复检查点里的待答问题
                    cp = checkpoint_store.load(
                        checkpoint_store.checkpoint_path(self.sessions_dir, sess.id),
                    )
                    if (
                        cp
                        and cp.get("source") == "desktop"
                        and cp.get("status") == "waiting"
                    ):
                        za.restore_checkpoint(cp)
                if not hasattr(agent, "put_task"):
                    raise RuntimeError("AgentRunner object has no put_task method")
                token, record = self._register_agent_locked(sess)
                runtime_config = getattr(za, "config", None)
                if runtime_config is not None:
                    runtime_config._desktop_agent_id = record["id"]
                display_q = agent.put_task(
                    prompt,
                    images=images or [],
                    task_mode=task_mode,
                    plan_path=plan_path,
                    prompt_capabilities=(PROMPT_CAPABILITY_FILE_DELIVERY,),
                )
            except Exception as exc:
                if token:
                    self._update_agent_locked(sess, token, "failed", str(exc))
                sess.agent_turn_token = ""
                self._persist_sessions()
                raise
            self._persist_sessions()
            return agent, display_q, token

    def _run_submitted_turn(self, sess: Session, agent: Any, display_q, token: str) -> None:
        try:
            self._drain_turn(sess, agent, display_q, token)
        except Exception as exc:
            self._fail_turn(sess, exc, token)

    def _prepare_next_turn_locked(self, sess: Session) -> Optional[threading.Thread]:
        """Promote one queued prompt after the current turn has fully ended."""
        if not sess.pending_turns or self.sessions.get(sess.id) is not sess:
            return None

        queued = sess.pending_turns[0]
        extra = {
            "display_content": queued.get("display_content", queued.get("prompt", "")),
            "queue_id": queued.get("queue_id", ""),
        }
        if queued.get("image_ids"):
            extra["image_ids"] = list(queued["image_ids"])
        # Persist the user turn first: a crash between the two writes must leave
        # the durable queue entry behind, never a silently dropped prompt. The
        # recovery pass skips entries whose queue_id already reached messages.
        self.add_message(sess, "user", str(queued.get("prompt") or ""), **extra)
        sess.pending_turns.pop(0)
        self._persist_pending_turns(sess)
        sess.status = "running"
        sess.last_error = ""
        sess.terminal_status = ""
        sess.terminal_reason = ""
        sess.partial = {
            "id": sess.msg_seq + 1,
            "role": "assistant",
            "content": "",
            "ts": time.time(),
            "partial": True,
        }
        thread = threading.Thread(
            target=self.run_agent_turn,
            args=(sess, str(queued.get("prompt") or ""), None),
            daemon=True,
            name=f"Queued-Turn-{sess.id}",
        )
        sess.thread = thread
        self._mark_turn_active(sess, True)
        return thread

    def _drain_turn(self, sess: Session, agent: Any, display_q, token: str) -> None:
        pieces: list[str] = []
        terminal: Optional[dict] = None
        import queue as _queue
        while terminal is None:
            try:
                item = display_q.get(timeout=1.0)
            except _queue.Empty:
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "chunk":
                text = str(item.get("text") or "")
                if not text:
                    continue
                pieces.append(text)
                with self.lock:
                    if self._turn_is_current_locked(sess, token) and sess.partial is not None:
                        sess.partial["content"] = "".join(pieces) if getattr(agent, "inc_out", False) else text
                        sess.partial["ts"] = time.time()
                        sess.updated_at = time.time()
                        self._sync_token_usage(sess)
                continue
            if item_type == "terminal":
                terminal = item
                if "status" not in terminal:
                    raise RuntimeError("invalid terminal event")
                continue

        terminal_status = str(terminal.get("status") or "failed")
        reason = str(terminal.get("reason") or "")
        text = str(terminal.get("text") or "")
        next_thread: Optional[threading.Thread] = None
        with self.lock:
            if not self._turn_is_current_locked(sess, token):
                return
            partial_text = str((sess.partial or {}).get("content") or "")
            sess.partial = None
            sess.terminal_status = terminal_status
            sess.terminal_reason = reason
            self._sync_token_usage(sess)
            plan_contract = self._live_task_mode(agent) == TaskMode.PLAN

            if terminal_status == "completed":
                import re as _re
                text = _re.sub(r'\n*`{5}\n*\[Info\] Final response to user\.\n*`{5}\s*$', '', text)
                self.add_message(sess, "assistant", text)
                sess.status = "idle"
                sess.last_error = ""
                if plan_contract:
                    certificate = terminal.get("certificate")
                    if (
                        isinstance(certificate, dict)
                        and certificate.get("plan_remaining") == 0
                        and certificate.get("verify_status") in {"pass", "partial_accepted"}
                    ):
                        sess.plan_status = "ready"
            elif terminal_status == "waiting":
                data = terminal.get("data")
                payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
                payload = payload if isinstance(payload, dict) else {}
                question = str(payload.get("question") or text or reason)
                candidates = payload.get("candidates")
                if not isinstance(candidates, list):
                    candidates = []
                self.add_message(
                    sess,
                    "system",
                    question,
                    kind="input_required",
                    candidates=[str(candidate) for candidate in candidates],
                )
                sess.status = "waiting"
                sess.last_error = ""
            elif terminal_status == "cancelled":
                sess.status = "cancelled"
                sess.last_error = ""
                # A hot restart (or user cancel) aborts an in-flight turn; the
                # user already saw the streamed text, so keep it instead of
                # making the transcript look like messages vanished.
                if partial_text.strip():
                    self.add_message(sess, "assistant", partial_text)
                if plan_contract:
                    sess.plan_status = "cancelled"
            else:
                data = terminal.get("data")
                data_error = data.get("error") if isinstance(data, dict) else ""
                error_detail = str(data_error or reason or terminal_status)
                sess.status = "error"
                sess.last_error = error_detail
                assistant_text = partial_text or (text if data_error else "")
                # Keep any streamed assistant output as a normal message. The
                # terminal error is a separate current-turn message; storing
                # the accumulated transcript as role=error would mark every
                # internal turn in that transcript as failed in the UI.
                if assistant_text.strip():
                    self.add_message(sess, "assistant", assistant_text)
                elif text:
                    error_detail = str(data_error or text or reason or terminal_status)
                    sess.last_error = error_detail
                self.add_message(sess, "error", error_detail)
                if plan_contract:
                    sess.plan_status = "failed"
            agent_status = {
                "completed": "completed",
                "waiting": "waiting",
                "cancelled": "cancelled",
            }.get(terminal_status, "failed")
            self._update_agent_locked(sess, token, agent_status, reason)
            sess.updated_at = time.time()
            self._mark_turn_active(sess, False)
            next_thread = self._prepare_next_turn_locked(sess)
            self._persist_sessions()
            next_state = sess.status
        emit_session_state(sess, next_state)
        if next_thread:
            next_thread.start()

    def _fail_turn(self, sess: Session, exc: Exception, token: Optional[str] = None) -> None:
        tb = traceback.format_exc()
        next_thread: Optional[threading.Thread] = None
        with self.lock:
            if token is not None and not self._turn_is_current_locked(sess, token):
                return
            partial_text = str((sess.partial or {}).get("content") or "")
            sess.partial = None
            sess.status = "error"
            sess.last_error = str(exc)
            sess.terminal_status = "failed"
            sess.terminal_reason = type(exc).__name__
            if token is not None:
                self._update_agent_locked(sess, token, "failed", str(exc))
            if partial_text.strip():
                self.add_message(sess, "assistant", partial_text)
            self.add_message(sess, "error", str(exc))
            self._mark_turn_active(sess, False)
            next_thread = self._prepare_next_turn_locked(sess)
            self._persist_sessions()
            next_state = sess.status
        print(tb, file=sys.stderr)
        emit_session_state(sess, next_state)
        if next_thread:
            next_thread.start()

    def run_agent_turn(
        self,
        sess: Session,
        prompt: str,
        images: Optional[list] = None,
        *,
        task_mode: TaskMode = TaskMode.OPEN,
        plan_path: Optional[str] = None,
    ):
        token = None
        try:
            submitted = self._submit_turn(sess, prompt, images, task_mode, plan_path)
            if submitted is None:
                return
            agent, display_q, token = submitted
            emit_session_state(sess, "running")
            self._drain_turn(sess, agent, display_q, token)
        except Exception as exc:
            self._fail_turn(sess, exc, token)

    def messages(self, sid: str, after: int = 0, limit: int = 200) -> dict:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            msgs = [m for m in sess.messages if int(m.get("id", 0)) > after]
            if limit > 0:
                msgs = msgs[-limit:]
            return {
                "sessionId": sid,
                "status": sess.status,
                "messages": msgs,
                "partial": dict(sess.partial) if sess.partial else None,
                "msgSeq": sess.msg_seq,
                "updatedAt": sess.updated_at,
                "lastError": sess.last_error,
                "terminalStatus": sess.terminal_status,
                "reason": sess.terminal_reason,
                "pendingTurns": self._pending_turns_payload(sess),
            }

    def cancel(self, sid: str) -> dict:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            # Persist what the user already saw before dropping the draft;
            # otherwise a stop makes streamed output disappear from the UI.
            self._flush_partial_assistant(sess)
            if sess.agent and hasattr(sess.agent, "abort"):
                with contextlib.suppress(Exception):
                    sess.agent.abort()
            token = sess.agent_turn_token
            if token:
                self._update_agent_locked(sess, token, "cancelled", "user_cancelled")
            self._cancel_child_agents_locked(sess)
            cancelled_queue_ids = [
                str(item.get("queue_id") or "")
                for item in sess.pending_turns
                if item.get("queue_id")
            ]
            # Prompts queued behind the cancelled turn were never executed:
            # keep them in the transcript as cancelled user input instead of
            # silently deleting what the user typed.
            cancelled_items = list(sess.pending_turns)
            sess.pending_turns.clear()
            for item in cancelled_items:
                text = str(
                    item.get("display_content") or item.get("prompt") or ""
                ).strip()
                if text:
                    self.add_message(
                        sess,
                        "user",
                        text,
                        kind="queued_cancelled",
                        queue_id=str(item.get("queue_id") or ""),
                    )
            self._persist_pending_turns(sess)
            self._mark_turn_active(sess, False)
            sess.agent_turn_token = ""
            sess.status = "cancelled"
            sess.partial = None
            sess.terminal_status = "cancelled"
            sess.terminal_reason = "user_cancelled"
            sess.updated_at = time.time()
            self._persist_sessions()
        emit_session_state(sess, "cancelled")
        return {"ok": True, "sessionId": sid, "cancelledQueueIds": cancelled_queue_ids}

    def get_agents(self, sid: str) -> list[dict]:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            return _clone_agent_records(sess.sub_agents)

    def cancel_agent(self, sid: str, agent_id: str) -> dict:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            record = next(
                (item for item in sess.sub_agents if item.get("id") == agent_id),
                None,
            )
            if record is None:
                raise web.HTTPNotFound(text=json.dumps({"error": "agent not found"}, ensure_ascii=False), content_type="application/json")
            record_type = record.get("type") or "session-agent"
            if record_type == "subagent":
                if record.get("status") in {"running", "waiting"}:
                    try:
                        pid = int(record.get("pid") or 0)
                    except (TypeError, ValueError):
                        pid = 0
                    try:
                        if pid > 0:
                            if record.get("detached") and os.name != "nt":
                                os.killpg(pid, signal.SIGTERM)
                            else:
                                os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        record["reason"] = "cancel_signal_failed"
                    record["status"] = "cancelled"
                    record["updated_at"] = time.time()
                    record["reason"] = record.get("reason") or "user_cancelled"
                    sess.updated_at = time.time()
                    self._persist_sessions()
                agents = _clone_agent_records(sess.sub_agents)
                state = sess.status
            elif record_type == "session-agent" and record is sess.sub_agents[0]:
                if sess.agent and hasattr(sess.agent, "abort"):
                    with contextlib.suppress(Exception):
                        sess.agent.abort()
                token = sess.agent_turn_token
                if token:
                    self._update_agent_locked(sess, token, "cancelled", "user_cancelled")
                self._cancel_child_agents_locked(sess)
                sess.agent_turn_token = ""
                sess.status = "cancelled"
                sess.partial = None
                sess.terminal_status = "cancelled"
                sess.terminal_reason = "user_cancelled"
                sess.updated_at = time.time()
                self._persist_sessions()
                agents = _clone_agent_records(sess.sub_agents)
                state = "cancelled"
            else:
                raise web.HTTPNotFound(text=json.dumps({"error": "agent not found"}, ensure_ascii=False), content_type="application/json")
        emit_session_state(sess, state)
        return {"ok": True, "sessionId": sid, "agentId": agent_id, "agents": agents}

    def _cancel_child_agents_locked(self, sess: Session) -> None:
        for record in sess.sub_agents:
            if record.get("type") != "subagent" or record.get("status") not in {"running", "waiting"}:
                continue
            try:
                pid = int(record.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            try:
                if pid > 0:
                    if record.get("detached") and os.name != "nt":
                        os.killpg(pid, signal.SIGTERM)
                    else:
                        os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            record["status"] = "cancelled"
            record["updated_at"] = time.time()
            record["reason"] = "parent_session_cancelled"


import base64
import tempfile

# Shared temp dir for image uploads (persists for process lifetime)
_UPLOAD_DIR = Path(tempfile.gettempdir()) / "zero_agent_web2_uploads"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _save_image_data(data_url: str, img_id: str) -> str:
    """Save a data URL to disk, return absolute path."""
    # data:image/png;base64,xxxxx
    if "," in data_url:
        header, b64 = data_url.split(",", 1)
    else:
        b64 = data_url
        header = ""
    ext = "png"
    if "jpeg" in header or "jpg" in header:
        ext = "jpg"
    elif "webp" in header:
        ext = "webp"
    elif "gif" in header:
        ext = "gif"
    fpath = _UPLOAD_DIR / f"{img_id}.{ext}"
    fpath.write_bytes(base64.b64decode(b64))
    return str(fpath)


def normalize_prompt(prompt: Any, images: Optional[list] = None):
    """Normalize prompt and images.
    
    images: list of dicts {"id": "img-xxx", "dataUrl": "data:..."} or plain data URLs.
    Returns: (prompt_text_with_image_tags, image_ids_list)
    """
    images = list(images or [])
    if isinstance(prompt, list):
        text_parts = []
        for part in prompt:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") in ("text", "input_text"):
                    text_parts.append(str(part.get("text") or part.get("content") or ""))
                elif part.get("type") in ("image", "input_image"):
                    url = part.get("image_url") or part.get("url") or part.get("data")
                    if isinstance(url, dict):
                        url = url.get("url")
                    if url:
                        images.append(url)
        prompt = "\n".join([p for p in text_parts if p])

    # Process images: save to disk, build [image:path] tags
    image_ids = []
    image_tags = []
    for img in images:
        if isinstance(img, dict):
            img_id = img.get("id") or f"img-{uuid.uuid4().hex[:8]}"
            data_url = img.get("dataUrl") or img.get("data_url") or ""
        else:
            # Plain data URL string
            img_id = f"img-{uuid.uuid4().hex[:8]}"
            data_url = str(img)
        if data_url:
            path = _save_image_data(data_url, img_id)
            image_tags.append(f"[image:{path}]")
            image_ids.append(img_id)

    # Append image tags to prompt
    final_prompt = str(prompt or "")
    if image_tags:
        final_prompt = final_prompt + "\n" + "\n".join(image_tags)

    return final_prompt, image_ids


class _TeeStream:
    """Duplicate a text stream to the console and the bridge log file."""

    def __init__(self, stream: Any, handle: Any) -> None:
        self.__dict__["_stream"] = stream
        self.__dict__["_handle"] = handle

    def write(self, data: str) -> int:
        try:
            written = self.__dict__["_stream"].write(data)
        except Exception:  # noqa: BLE001 - 控制台不可用不得影响日志
            written = len(data)
        try:
            self.__dict__["_handle"].write(data)
            self.__dict__["_handle"].flush()
        except Exception:  # noqa: BLE001 - 日志不可写不得影响运行
            pass
        return written

    def flush(self) -> None:
        for key in ("_stream", "_handle"):
            try:
                self.__dict__[key].flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        try:
            return bool(self.__dict__["_stream"].isatty())
        except Exception:  # noqa: BLE001
            return False

    def fileno(self) -> int:
        return self.__dict__["_stream"].fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_stream"], name)


_BRIDGE_LOG_HANDLE: Any = None


def configure_bridge_logging(sessions_dir: str) -> str:
    """Mirror stdout / stderr / bridge-log events into ``desktop_bridge.log``.

    桌面端此前只有 UI 内的 ``bridge-log`` 事件，进程的 print / traceback 全部随
    Tauri 的管道消失，出问题无法事后排查。这里把三者落到
    ``<sessions_dir>/desktop_bridge.log``（超过 5 MiB 轮转一次）。

    Args:
        sessions_dir: 会话目录（与 sessions.sqlite3 同级）.

    Returns:
        日志文件路径；不可写时返回空串.
    """
    global _BRIDGE_LOG_HANDLE
    if _BRIDGE_LOG_HANDLE is not None:
        return str(Path(sessions_dir) / "desktop_bridge.log")
    path = Path(sessions_dir).expanduser() / "desktop_bridge.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(path.with_name("desktop_bridge.log.1"))
        handle = open(path, "a", encoding="utf-8", buffering=1)
    except OSError as exc:
        print(f"bridge log unavailable: {exc}", file=sys.stderr)
        return ""
    _BRIDGE_LOG_HANDLE = handle
    sys.stdout = _TeeStream(sys.stdout, handle)
    sys.stderr = _TeeStream(sys.stderr, handle)
    handle.write(
        f"\n=== bridge start {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"pid={os.getpid()} ===\n"
    )
    previous_hook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        try:
            handle.write("".join(traceback.format_exception(exc_type, exc, tb)))
            handle.flush()
        except Exception:  # noqa: BLE001
            pass
        previous_hook(exc_type, exc, tb)

    sys.excepthook = _excepthook
    return str(path)


def log_bridge_event(text: str) -> None:
    """Append one ``bridge-log`` payload (what the UI shows) to the log file."""
    handle = _BRIDGE_LOG_HANDLE
    if handle is None:
        return
    try:
        handle.write(f"[bridge-log] {text}\n")
    except Exception:  # noqa: BLE001
        pass


class _LazyAgentManager:
    """Create the AgentManager singleton on first use.

    Importing this module must stay free of side effects: constructing the
    manager eagerly opened (and, on first run, created) the real session store
    under the project's ``workspace/sessions`` — every test that merely
    imported the bridge wrote the developer's runtime directory.
    """

    _instance: Optional[AgentManager] = None

    def _manager(self) -> AgentManager:
        if type(self)._instance is None:
            type(self)._instance = AgentManager()
        return type(self)._instance  # type: ignore[return-value]

    @property
    def __dict__(self):  # type: ignore[override]
        # Tests (and monkeypatch) mutate manager attributes; expose the real
        # instance dict so attribute lookups/undo behave like AgentManager.
        return self._manager().__dict__

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_instance":
            type(self)._instance = value
            return
        setattr(self._manager(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._manager(), name)


manager: AgentManager = _LazyAgentManager()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Transport layer: WS notification only
# ---------------------------------------------------------------------------

class WsHub:
    def __init__(self):
        self.websockets: Set[web.WebSocketResponse] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def emit(self, obj: dict):
        if isinstance(obj, dict) and obj.get("type") == "bridge-log":
            log_bridge_event(str(obj.get("payload", "")))
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(obj), self.loop)

    async def _broadcast(self, obj: dict):
        data = json.dumps(obj, ensure_ascii=False, default=str)
        dead = set()
        for ws in list(self.websockets):
            try:
                await ws.send_str(data)
            except Exception:
                dead.add(ws)
        self.websockets.difference_update(dead)


hub = WsHub()


def emit_session_state(sess: Session, state_name: str):
    with manager.lock:
        payload = {
            "type": "session-state",
            "sessionId": sess.id,
            "state": state_name,
            "status": sess.status,
            "terminalStatus": sess.terminal_status,
            "reason": sess.terminal_reason,
            "seq": sess.msg_seq,
            "updatedAt": sess.updated_at,
            "title": sess.title,
            "tokenUsage": copy.deepcopy(sess.token_usage),
            "modelOverride": sess.model_override,
            "groupId": sess.group_id,
            "subAgents": _clone_agent_records(sess.sub_agents),
            "planPath": sess.plan_path,
            "planStatus": sess.plan_status,
            "planTask": sess.plan_task,
            "pendingTurns": manager._pending_turns_payload(sess),
        }
    hub.emit(payload)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    hub.websockets.add(ws)
    await ws.send_str(json.dumps({
        "type": "bridge-ready",
        "workspaceDir": manager.workspace_dir,
        "configPath": manager.config_path,
        "http": True,
        "wsEventsOnly": True,
    }, ensure_ascii=False))
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            # WS is intentionally not a data/command channel anymore.
            with contextlib.suppress(Exception):
                data = json.loads(msg.data)
                if data.get("action") == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "ts": time.time()}, ensure_ascii=False))
    hub.websockets.discard(ws)
    return ws


# ---------------------------------------------------------------------------
# Transport layer: HTTP command/data API
# ---------------------------------------------------------------------------

_CORS_METHODS = "GET,POST,DELETE,OPTIONS"
_CORS_HEADERS = "Content-Type, Authorization, X-ZA-Desktop-Token"


def _is_public_request(request: web.Request) -> bool:
    if request.path in {"/", "/status"}:
        return True
    route = request.match_info.route
    return isinstance(getattr(route, "resource", None), web.StaticResource)


def cors_headers(request: Optional[web.Request] = None) -> dict[str, str]:
    if request is None:
        return {}
    origin = request.headers.get("Origin")
    security = request.app.get("bridge_security")
    if not origin or not isinstance(security, BridgeSecurity) or origin not in security.allowed_origins:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": _CORS_METHODS,
        "Access-Control-Allow-Headers": _CORS_HEADERS,
        "Vary": "Origin",
    }


def _request_auth_tokens(request: web.Request) -> tuple[str, ...]:
    if request.path == "/ws":
        return (request.query.get("token", "").strip(),)

    candidates: list[str] = []
    # <img> requests cannot attach Authorization headers. Allow the bridge
    # token in the query only for the read-only, session-scoped image route.
    if request.method == "GET" and re.fullmatch(r"/session/[^/]+/(?:image|file)", request.path):
        candidates.append(request.query.get("token", "").strip())
    auth = request.headers.get("Authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value:
        candidates.append(value.strip())
    candidates.append(request.headers.get("X-ZA-Desktop-Token", "").strip())
    return tuple(candidates)


@web.middleware
async def security_middleware(request, handler):
    security = request.app["bridge_security"]
    origin = request.headers.get("Origin")
    if origin and origin not in security.allowed_origins:
        return web.json_response({"error": "origin not allowed"}, status=403)

    headers = cors_headers(request)
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=headers)
    if not _is_public_request(request):
        if not any(
            token and hmac.compare_digest(token, security.token)
            for token in _request_auth_tokens(request)
        ):
            return web.json_response({"error": "unauthorized"}, status=401, headers=headers)

    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        for k, v in headers.items():
            exc.headers[k] = v
        raise
    if not getattr(resp, "prepared", False):
        for k, v in headers.items():
            resp.headers[k] = v
    return resp


def json_ok(data: dict, status: int = 200):
    return web.json_response(data, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False, default=str))


async def read_json(request) -> dict:
    if request.can_read_body:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


async def status_handler(request):
    return json_ok({
        "ok": True,
        "running": True,
        "ready": True,
        "authRequired": True,
    })


async def system_restart_handler(request):
    """Manually hot-restart the bridge process and resume active tasks."""

    manager.request_hot_restart("manual", reset_budget=True)
    return json_ok({"ok": True, "restarting": True})


async def get_config_handler(request):
    return json_ok({
        "workspaceDir": manager.workspace_dir,
        "configPath": manager.config_path,
        "config": manager.config,
    })


async def save_config_handler(request):
    data = await read_json(request)
    cfg = data.get("config", data)
    if isinstance(cfg, dict):
        manager.config.update(cfg)
    return json_ok({
        "ok": True,
        "workspaceDir": manager.workspace_dir,
        "configPath": manager.config_path,
        "config": manager.config,
    })


async def model_profiles_handler(request):
    return json_ok({"profiles": manager.list_model_profiles()})


async def slash_commands_handler(request):
    try:
        from zero_agent.frontends.desktop_commands import PALETTE_ENTRIES, prompt_for
        locally_handled = {"/resume", "/scheduler"}
        commands = [
            {"cmd": cmd, "argHint": arg_hint, "description": desc}
            for cmd, arg_hint, desc in PALETTE_ENTRIES
            if cmd in locally_handled or prompt_for(cmd, "")
        ]
        commands.append({
            "cmd": "/continue",
            "argHint": "",
            "description": "继续最近一个历史会话",
        })
    except Exception as e:
        print(f"get slash commands failed: {e}", file=sys.stderr)
        commands = []
    return json_ok({"commands": commands})


async def slash_resolve_handler(request):
    data = await read_json(request)
    command = str(data.get("command") or data.get("cmd") or "").strip()
    args_text = str(data.get("args") or data.get("argsText") or "").strip()
    if not command.startswith("/"):
        command = "/" + command
    try:
        from zero_agent.frontends.desktop_commands import prompt_for
        prompt = prompt_for(command, args_text)
    except Exception as e:
        return json_ok({"ok": False, "error": f"Failed to resolve {command}: {type(e).__name__}: {e}"}, status=500)
    if not prompt:
        return json_ok({"ok": False, "error": f"Unsupported slash command: {command}"}, status=404)
    return json_ok({"ok": True, "command": command, "prompt": prompt})


async def scheduler_status_handler(request):
    try:
        from zero_agent.frontends import desktop_commands
        tasks = desktop_commands.list_scheduler_tasks()
        running = desktop_commands.running_services()
        return json_ok({
            "ok": True,
            "tasks": tasks,
            "running": "reflect/scheduler.py" in running,
            "pid": running.get("reflect/scheduler.py"),
        })
    except Exception as e:
        return json_ok({"ok": False, "error": f"Failed to read scheduler: {type(e).__name__}: {e}"}, status=500)


async def scheduler_start_handler(request):
    try:
        from zero_agent.frontends import desktop_commands
        ok, message = desktop_commands.start_reflect_task("scheduler")
        running = desktop_commands.running_services(use_cache=False)
        return json_ok({
            "ok": ok,
            "message": message,
            "running": "reflect/scheduler.py" in running,
            "pid": running.get("reflect/scheduler.py"),
        }, status=200 if ok else 409)
    except Exception as e:
        return json_ok({"ok": False, "error": f"Failed to start scheduler: {type(e).__name__}: {e}"}, status=500)


async def memory_profile_get_handler(request):
    return json_ok({"ok": True, **manager.memory_profile()})


async def memory_profile_save_handler(request):
    data = await read_json(request)
    profile = data.get("profile")
    if not isinstance(profile, str):
        return json_ok({"ok": False, "error": "profile must be a string"}, status=400)
    try:
        return json_ok({"ok": True, **manager.save_memory_profile(profile)})
    except ValueError as exc:
        return json_ok({"ok": False, "error": str(exc)}, status=400)


async def channels_handler(request):
    try:
        from zero_agent.frontends import desktop_commands
        return json_ok({
            "ok": True,
            "channels": desktop_commands.channel_statuses(),
        })
    except Exception as exc:
        return json_ok(
            {"ok": False, "error": f"Failed to read channels: {type(exc).__name__}: {exc}"},
            status=500,
        )


def _channel_definition_from_request(request):
    try:
        return channel_control.channel_definition(request.match_info.get("channel_id", ""))
    except KeyError:
        raise web.HTTPNotFound(text="unknown channel")


async def channel_start_handler(request):
    definition = _channel_definition_from_request(request)
    try:
        from zero_agent.frontends import desktop_commands
        ok, message = desktop_commands.start_channel(definition.id)
        return json_ok({
            "ok": ok,
            "message": message,
            "channels": desktop_commands.channel_statuses(),
        }, status=200 if ok else 409)
    except Exception as exc:
        return json_ok(
            {"ok": False, "error": f"Failed to start channel: {type(exc).__name__}: {exc}"},
            status=500,
        )


async def channel_stop_handler(request):
    definition = _channel_definition_from_request(request)
    try:
        from zero_agent.frontends import desktop_commands
        ok, message = desktop_commands.stop_channel(definition.id)
        return json_ok({
            "ok": ok,
            "message": message,
            "channels": desktop_commands.channel_statuses(),
        }, status=200 if ok else 409)
    except Exception as exc:
        return json_ok(
            {"ok": False, "error": f"Failed to stop channel: {type(exc).__name__}: {exc}"},
            status=500,
        )


async def channel_link_handler(request):
    definition = _channel_definition_from_request(request)
    data = await read_json(request)
    linked = data.get("linked")
    if not isinstance(linked, bool):
        return json_ok({"ok": False, "error": "linked must be a boolean"}, status=400)
    try:
        channel_control.set_channel_linked(definition.id, linked)
        from zero_agent.frontends import desktop_commands
        return json_ok({
            "ok": True,
            "channels": desktop_commands.channel_statuses(),
        })
    except Exception as exc:
        return json_ok(
            {"ok": False, "error": f"Failed to link channel: {type(exc).__name__}: {exc}"},
            status=500,
        )


def _channel_config_error_response(exc: ChannelConfigError):
    message = str(exc).lower()
    if "environment-managed" in message:
        return json_ok(
            {"ok": False, "error": "channel configuration is environment-managed"},
            status=409,
        )
    if "parse" in message:
        error = "channel configuration source parse error"
    elif "path" in message:
        error = "channel configuration source path error"
    elif "bots section" in message:
        error = "channel configuration source structure error"
    else:
        error = "invalid channel configuration"
    return json_ok({"ok": False, "error": error}, status=400)


async def channel_config_get_handler(request):
    definition = _channel_definition_from_request(request)
    try:
        snapshot = channel_config_snapshot(definition.id)
    except ChannelConfigError as exc:
        return _channel_config_error_response(exc)
    return json_ok({"ok": True, "config": snapshot})


async def channel_config_save_handler(request):
    definition = _channel_definition_from_request(request)
    try:
        data = await read_json(request)
        values = data.get("values")
        if not isinstance(values, dict):
            return json_ok(
                {"ok": False, "error": "values must be an object"},
                status=400,
            )
        snapshot = update_channel_config(definition.id, values)
        from zero_agent.frontends import desktop_commands

        channels = desktop_commands.channel_statuses()
        row = next((item for item in channels if item["id"] == definition.id), None)
        return json_ok({
            "ok": True,
            "config": snapshot,
            "channels": channels,
            "requiresRestart": bool(row and row.get("running")),
        })
    except ChannelConfigError as exc:
        return _channel_config_error_response(exc)
    except Exception as exc:
        return json_ok(
            {
                "ok": False,
                "error": f"Failed to save channel configuration: {type(exc).__name__}",
            },
            status=500,
        )


async def wechat_login_start_handler(request):
    from zero_agent.frontends import desktop_commands

    if "bots/wechat_app.py" in desktop_commands.running_services(use_cache=False):
        return json_ok(
            {"ok": False, "error": "WeChat channel is running"},
            status=409,
        )
    manager = request.app["wechat_qr_manager"]
    try:
        result = manager.start()
        if not isinstance(result, dict):
            raise TypeError("invalid QR manager response")
    except Exception as exc:
        return json_ok(
            {
                "ok": False,
                "status": "failed",
                "error": f"WeChat QR initialization failed: {type(exc).__name__}",
            },
            status=503,
        )
    if result.get("status") == "failed":
        return json_ok({"ok": False, **result}, status=503)
    return json_ok({"ok": True, **result})


async def wechat_login_status_handler(request):
    session_id = request.match_info.get("session_id", "")
    manager = request.app["wechat_qr_manager"]
    try:
        result = manager.status(session_id)
    except KeyError:
        return json_ok({"ok": False, "error": "login session not found"}, status=404)
    except Exception as exc:
        return json_ok(
            {
                "ok": False,
                "error": f"WeChat QR status failed: {type(exc).__name__}",
            },
            status=503,
        )
    return json_ok({"ok": True, **result})


async def wechat_login_cancel_handler(request):
    session_id = request.match_info.get("session_id", "")
    manager = request.app["wechat_qr_manager"]
    try:
        result = manager.cancel(session_id)
    except KeyError:
        return json_ok({"ok": False, "error": "login session not found"}, status=404)
    except Exception as exc:
        return json_ok(
            {
                "ok": False,
                "error": f"WeChat QR cancellation failed: {type(exc).__name__}",
            },
            status=503,
        )
    return json_ok({"ok": True, **result})


async def list_sessions_handler(request):
    with manager.lock:
        sessions = [manager.snapshot(s, include_messages=False) for s in manager.sessions.values()]
    return json_ok({"sessions": sessions, "activeSessionId": manager.active_session_id})


async def history_sessions_handler(request):
    limit = int(request.query.get("limit") or 10)
    return json_ok({"sessions": manager.list_resume_sessions(limit=limit)})


async def history_resume_handler(request):
    data = await read_json(request)
    sid = str(data.get("sessionId") or data.get("id") or manager.active_session_id or "")
    index = int(data.get("index") or data.get("n") or 0)
    if not sid:
        return json_ok({"ok": False, "error": "missing sessionId"}, status=400)
    if index <= 0:
        return json_ok({"ok": False, "error": "missing resume index"}, status=400)
    return json_ok(manager.resume_history(sid, index))


async def new_session_handler(request):
    data = await read_json(request)
    requested_model = data.get("modelName")
    if requested_model is None:
        requested_model = data.get("modelNo")
    model_override = (
        str(requested_model).strip()
        if requested_model is not None and str(requested_model).strip()
        else None
    )
    sess = manager.create_session(
        cwd=data.get("cwd") or data.get("path"),
        model_override=model_override,
    )
    return json_ok({"ok": True, "sessionId": sess.id, "session": manager.snapshot(sess)}, status=201)


async def get_session_handler(request):
    sid = request.match_info["sid"]
    sess = manager.get_session(sid)
    return json_ok({"sessionId": sid, "session": manager.snapshot(sess), "messages": list(sess.messages), "partial": sess.partial})


async def delete_session_handler(request):
    sid = request.match_info["sid"]
    return json_ok(manager.delete_session(sid))


async def replace_session_handler(request):
    sid = request.match_info["sid"]
    return json_ok(manager.replace_session(sid))


async def prompt_handler(request):
    sid = request.match_info["sid"]
    data = await read_json(request)
    prompt = data.get("prompt", data.get("content", data.get("message", "")))
    images = data.get("images") or []
    display_text = data.get("displayText")
    try:
        result = manager.submit_prompt(sid, prompt, images, display_text=display_text)
    except ValueError as exc:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": str(exc)}, ensure_ascii=False),
            content_type="application/json",
        ) from exc
    except web.HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 必须把真实原因交给前端
        # A bare aiohttp 500 hides the cause and the UI labels every 5xx as a
        # model outage. Keep the traceback in the bridge log and echo a
        # concrete message so the user can act on it.
        print(traceback.format_exc(), file=sys.stderr)
        raise web.HTTPInternalServerError(
            text=json.dumps(
                {
                    "error": f"提交失败：{type(exc).__name__}: {exc}",
                    "code": "prompt_failed",
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
        ) from exc
    return json_ok(result)


async def messages_handler(request):
    sid = request.match_info["sid"]
    after = int(request.query.get("after") or request.query.get("afterId") or 0)
    limit = int(request.query.get("limit") or 200)
    return json_ok(manager.messages(sid, after=after, limit=limit))


async def session_image_handler(request):
    """Serve a session's local raster images for Markdown previews."""
    sid = request.match_info["sid"]
    raw_path = str(request.query.get("path") or "").strip()
    if not raw_path:
        raise web.HTTPNotFound(text="image not found")

    with manager.lock:
        sess = manager.sessions.get(sid)
        if sess is None:
            raise web.HTTPNotFound(text="image not found")
        cwd = sess.cwd or manager.workspace_dir

    image_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".avif": "image/avif",
    }
    try:
        root = Path(cwd).expanduser().resolve(strict=True)
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        image_path = candidate.resolve(strict=True)
        image_path.relative_to(root)
        content_type = image_types[image_path.suffix.lower()]
        if not image_path.is_file():
            raise ValueError("not a regular image file")
    except (OSError, RuntimeError, ValueError, KeyError):
        raise web.HTTPNotFound(text="image not found")

    response = web.FileResponse(image_path)
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def session_file_handler(request):
    """Serve a generated session file as a download, confined to its cwd."""
    sid = request.match_info["sid"]
    raw_path = str(request.query.get("path") or "").strip()
    if not raw_path:
        raise web.HTTPNotFound(text="file not found")

    with manager.lock:
        sess = manager.sessions.get(sid)
        if sess is None:
            raise web.HTTPNotFound(text="file not found")
        cwd = sess.cwd or manager.workspace_dir

    try:
        root = Path(cwd).expanduser().resolve(strict=True)
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        file_path = candidate.resolve(strict=True)
        file_path.relative_to(root)
        if not file_path.is_file():
            raise ValueError("not a regular file")
    except (OSError, RuntimeError, ValueError):
        raise web.HTTPNotFound(text="file not found")

    response = web.FileResponse(file_path)
    response.headers["Content-Disposition"] = (
        "attachment; filename*=UTF-8''" + urllib.parse.quote(file_path.name)
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def cancel_handler(request):
    sid = request.match_info["sid"]
    return json_ok(manager.cancel(sid))


async def plan_handler(request):
    sid = request.match_info["sid"]
    data = await read_json(request)
    task = data.get("task", "")
    return json_ok(manager.start_plan(sid, task), status=202)


async def plan_execute_handler(request):
    sid = request.match_info["sid"]
    return json_ok(manager.execute_plan(sid), status=202)


async def get_agents_handler(request):
    """Get sub-agents for a session."""
    sid = request.match_info["sid"]
    return json_ok({"ok": True, "sessionId": sid, "agents": manager.get_agents(sid)})


async def cancel_agent_handler(request):
    """Cancel a sub-agent."""
    sid = request.match_info["sid"]
    aid = request.match_info["aid"]
    return json_ok(manager.cancel_agent(sid, aid))


async def path_open_handler(request):
    data = await read_json(request)
    kind = data.get("kind", "")
    if kind == "config":
        target = Path(manager.config_path)
    else:
        target = Path(data.get("path") or data.get("target") or manager.workspace_dir)
    target = target.resolve()
    if not target.exists():
        return json_ok({"ok": False, "error": f"File not found: {target}"})
    # Actually open the file with the system default editor
    import subprocess, platform
    if platform.system() == "Windows":
        os.startfile(str(target))
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return json_ok({"ok": True, "path": str(target)})


async def set_model_handler(request):
    """Set model override for a session"""
    sid = request.match_info["sid"]
    data = await read_json(request)
    model_name = data.get("modelName")
    if model_name is None:
        model_name = data.get("modelNo")
    model_name = (
        str(model_name).strip()
        if model_name is not None and str(model_name).strip()
        else None
    )

    with manager.lock:
        sess = manager.get_session(sid)
        sess.model_override = model_name
        manager.current_model_override = model_name
        sess.updated_at = time.time()
        if session_has_user_message(sess):
            manager._persist_sessions()

    emit_session_state(sess, "model-changed")
    return json_ok({"ok": True, "sessionId": sid, "modelOverride": sess.model_override})


async def get_tokens_handler(request):
    """Get token usage for a session"""
    sid = request.match_info["sid"]
    sess = manager.get_session(sid)
    manager._sync_token_usage(sess)
    return json_ok({"ok": True, "sessionId": sid, "tokenUsage": sess.token_usage})


async def set_session_group_handler(request):
    """Assign a session to a persisted frontend group."""
    sid = request.match_info["sid"]
    data = await read_json(request)
    return json_ok(manager.set_session_group(sid, data.get("groupId")))
async def list_groups_handler(request):
    return json_ok({"ok": True, "groups": manager.list_groups()})

async def create_group_handler(request):
    data = await read_json(request)
    return json_ok(manager.create_group(str(data.get("name") or "")))

async def delete_group_handler(request):
    gid = request.match_info["gid"]
    return json_ok(manager.delete_group(gid))




async def worldline_handler(request):
    """GET /worldline/{sid} — return checkpoint tree when worldline is enabled."""
    sid = request.match_info["sid"]
    sess = manager.get_session(sid)
    if sess is None:
        return json_ok({"enabled": False, "items": []})

    config = getattr(sess, "config", None) or getattr(manager, "config", None)
    enabled = getattr(config, "enable_worldline", False) if config else False
    if not enabled:
        return json_ok({"enabled": False, "items": []})

    try:
        from zero_agent.frontends.worldline import tree_from_store
        import time

        handler = getattr(sess, "handler", None)
        store = getattr(handler, "_worldline_store", None) if handler else None
        if store is None:
            return json_ok({"enabled": True, "items": []})

        tree = tree_from_store(store, time.time())
        nodes = [
            {"id": n.id, "title": n.title, "kind": n.kind, "children": n.children}
            for n in [tree.nodes[nid] for nid in tree.root_id] if nid in tree.nodes
        ] if tree.root_id else []
        return json_ok({"enabled": True, "items": nodes})
    except ImportError:
        return json_ok({"enabled": True, "items": []})


async def worldline_restore_handler(request):
    """POST /worldline/{sid}/restore — restore to a checkpoint node."""
    sid = request.match_info["sid"]
    sess = manager.get_session(sid)
    if sess is None:
        return json_ok({"ok": False, "error": "session not found"}, status=404)

    # Reject restore while session is running
    if getattr(sess, "running", False):
        return json_ok({"ok": False, "error": "session is running"}, status=409)

    data = await read_json(request)
    node_id = data.get("nodeId")
    if not node_id:
        return json_ok({"ok": False, "error": "nodeId required"}, status=400)

    try:
        from zero_agent.frontends.worldline import restore_plan
        handler = getattr(sess, "handler", None)
        store = getattr(handler, "_worldline_store", None) if handler else None
        if store is None:
            return json_ok({"ok": False, "error": "worldline not initialized"}, status=400)

        result = restore_plan(store, node_id)
        return json_ok({"ok": True, "result": result or {}})
    except ImportError:
        return json_ok({"ok": False, "error": "worldline module unavailable"}, status=500)



def create_app(
    *,
    security: Optional[BridgeSecurity] = None,
    host: str = "127.0.0.1",
    port: int = 14168,
    wechat_qr_manager=None,
):
    security = _validate_bridge_security(security) if security is not None else load_bridge_security(host, port)
    app = web.Application(middlewares=[security_middleware])
    if wechat_qr_manager is None:
        from zero_agent.frontends.channel_onboarding import WechatQrManager

        wechat_qr_manager = WechatQrManager()
    app.router.add_get("/memory/profile", memory_profile_get_handler)
    app.router.add_post("/memory/profile", memory_profile_save_handler)
    app.router.add_get("/channels", channels_handler)
    app.router.add_post("/channels/{channel_id}/start", channel_start_handler)
    app.router.add_post("/channels/{channel_id}/stop", channel_stop_handler)
    app.router.add_post("/channels/{channel_id}/link", channel_link_handler)
    app.router.add_get("/channels/{channel_id}/config", channel_config_get_handler)
    app.router.add_post("/channels/{channel_id}/config", channel_config_save_handler)
    app.router.add_post("/channels/wechat/login", wechat_login_start_handler)
    app.router.add_get("/channels/wechat/login/{session_id}", wechat_login_status_handler)
    app.router.add_post(
        "/channels/wechat/login/{session_id}/cancel",
        wechat_login_cancel_handler,
    )
    app["bridge_security"] = security
    app["wechat_qr_manager"] = wechat_qr_manager
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_post("/system/restart", system_restart_handler)
    app.router.add_get("/config", get_config_handler)
    app.router.add_post("/config", save_config_handler)
    app.router.add_get("/model-profiles", model_profiles_handler)
    app.router.add_get("/slash/commands", slash_commands_handler)
    app.router.add_post("/slash/resolve", slash_resolve_handler)
    app.router.add_get("/scheduler", scheduler_status_handler)
    app.router.add_post("/scheduler/start", scheduler_start_handler)
    app.router.add_get("/history/sessions", history_sessions_handler)
    app.router.add_post("/history/resume", history_resume_handler)
    app.router.add_get("/sessions", list_sessions_handler)
    app.router.add_post("/session/new", new_session_handler)
    app.router.add_get("/session/{sid}", get_session_handler)
    app.router.add_post("/session/{sid}/replace", replace_session_handler)
    app.router.add_delete("/session/{sid}", delete_session_handler)
    app.router.add_post("/session/{sid}/prompt", prompt_handler)
    app.router.add_get("/session/{sid}/messages", messages_handler)
    app.router.add_get("/session/{sid}/image", session_image_handler)
    app.router.add_get("/session/{sid}/file", session_file_handler)
    app.router.add_post("/session/{sid}/cancel", cancel_handler)
    app.router.add_post("/session/{sid}/plan", plan_handler)
    app.router.add_post("/session/{sid}/plan/execute", plan_execute_handler)
    app.router.add_post("/session/{sid}/model", set_model_handler)
    app.router.add_get("/session/{sid}/tokens", get_tokens_handler)
    app.router.add_post("/session/{sid}/group", set_session_group_handler)
    app.router.add_get("/session/{sid}/agents", get_agents_handler)
    app.router.add_post("/session/{sid}/agents/{aid}/cancel", cancel_agent_handler)
    app.router.add_get("/worldline/{sid}", worldline_handler)
    app.router.add_post("/worldline/{sid}/restore", worldline_restore_handler)
    app.router.add_get("/groups", list_groups_handler)
    app.router.add_post("/groups", create_group_handler)
    app.router.add_delete("/groups/{gid}", delete_group_handler)
    app.router.add_post("/path/open", path_open_handler)

    # Serve static frontend (desktop/static/). The Tauri WebView keeps an HTTP
    # cache across app reinstalls, so each app navigation gets a fresh asset
    # key and the document itself is explicitly non-cacheable.
    static_dir = APP_DIR / "desktop" / "static"

    def frontend_cache_key(request: web.Request) -> str:
        requested = (request.query.get("za_ui") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", requested):
            return requested
        files = (
            static_dir / "index.html",
            static_dir / "styles.css",
            static_dir / "za-web.js",
            static_dir / "app.js",
        )
        mtimes = [path.stat().st_mtime_ns for path in files if path.exists()]
        return str(max(mtimes, default=0))

    def version_frontend_assets(body: str, cache_key: str) -> str:
        for attribute, asset in (
            ("href", "styles.css"),
            ("href", "vendor/katex/katex.min.css"),
            ("src", "vendor/marked.min.js"),
            ("src", "vendor/katex/katex.min.js"),
            ("src", "za-web.js"),
            ("src", "app.js"),
        ):
            pattern = rf'({attribute}="{re.escape(asset)})(?:\?[^" ]*)?(")'
            body = re.sub(pattern, rf"\1?v={cache_key}\2", body)
        return body

    async def index_handler(request):
        body = (static_dir / "index.html").read_text(encoding="utf-8")
        body = version_frontend_assets(body, frontend_cache_key(request))
        response = web.Response(text=body, content_type="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    app.router.add_get("/", index_handler)
    app.router.add_static("/", static_dir, show_index=False)

    async def on_startup(app):
        hub.loop = asyncio.get_running_loop()
        resume = getattr(manager, "resume_restarted_sessions", None)
        if callable(resume):
            app["checkpoint_resume_task"] = asyncio.create_task(
                asyncio.to_thread(resume)
            )
        parent_pid = desktop_parent_pid()
        if parent_pid is not None:
            app["desktop_parent_monitor"] = asyncio.create_task(monitor_desktop_parent(parent_pid))

    async def on_cleanup(app):
        task = app.get("desktop_parent_monitor")
        if task is not None:
            task.cancel()
        resume_task = app.get("checkpoint_resume_task")
        if resume_task is not None:
            resume_task.cancel()
        qr_manager = app.get("wechat_qr_manager")
        if qr_manager is not None:
            qr_manager.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "14168"))
    try:
        bridge_log = configure_bridge_logging(manager.sessions_dir)
    except Exception as exc:  # noqa: BLE001 - 日志失败不得阻断桌面端
        bridge_log = ""
        print(f"configure bridge logging failed: {exc}", file=sys.stderr)
    if bridge_log:
        print(f"bridge log: {bridge_log}", file=sys.stderr)
    security = load_bridge_security(host, port)
    adopt_bridge_token(security)
    url = f"http://{host}:{port}/"
    browser_url = f"{url}#token={urllib.parse.quote(security.token, safe='')}"
    print(f"ZeroAgent Web2 bridge: {url}  ws://{host}:{port}/ws", file=sys.stderr)
    if os.environ.get("ZA_DESKTOP_BRIDGE_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(browser_url)).start()
    web.run_app(create_app(security=security, host=host, port=port), host=host, port=port, print=None)
