#!/usr/bin/env python3
"""
ZeroAgent Web2 Bridge.

Clear split:
1) AgentManager: owns AgentRunner instances, sessions and histories.
2) Transport: HTTP is the command/data channel; WebSocket only pushes small
   session-state notifications.

HTTP API:
  GET    /status
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

import asyncio, contextlib, copy, hmac, importlib, ipaddress, json, os, re, secrets, signal, sys, urllib.parse, webbrowser
import threading, time, traceback, uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from aiohttp import web, WSMsgType

from zero_agent.runners.agent_runner import AgentRunner
from zero_agent.core.agent import ZeroAgent
from zero_agent.core.config import AgentConfig, default_config_path, load_default_config

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


def desktop_parent_is_alive(expected_parent_pid: int, *, current_parent_pid: Optional[int] = None) -> bool:
    return expected_parent_pid > 1 and (current_parent_pid if current_parent_pid is not None else os.getppid()) == expected_parent_pid


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
    return parent_pid if parent_pid > 1 else None


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
    last_error: str = ""
    terminal_status: str = ""
    terminal_reason: str = ""
    # New fields for frontend redesign
    model_override: Optional[str] = None  # Override model for this session
    token_usage: Dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_TOKEN_USAGE))
    group_id: Optional[str] = None  # Session group ID
    log_path: Optional[str] = None
    sub_agents: List[Dict[str, Any]] = field(default_factory=list)
    restore_history: bool = False

_DESKTOP_SESSION_ID_RE = re.compile(r"^sess-[0-9a-f]{12}$")


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
# Session persistence (sessions.json) so conversations survive app restarts.
# ---------------------------------------------------------------------------

def _session_to_persistable(sess: Session) -> dict:
    return {
        "id": sess.id,
        "title": sess.title,
        "cwd": sess.cwd,
        "created_at": sess.created_at,
        "updated_at": sess.updated_at,
        "messages": list(sess.messages),
        "msg_seq": sess.msg_seq,
        "log_path": sess.log_path,
        "status": "idle",
        "last_error": sess.last_error,
        "terminal_status": sess.terminal_status,
        "terminal_reason": sess.terminal_reason,
        "model_override": sess.model_override,
        "token_usage": _normalize_token_usage(sess.token_usage),
        "group_id": sess.group_id,
        "sub_agents": list(sess.sub_agents),
    }


def _session_from_persisted(
    data: dict,
    sessions_dir: str | None = None,
) -> Session:
    sid = str(data.get("id") or ("sess-" + uuid.uuid4().hex[:12]))
    owned_path = _desktop_log_path(sessions_dir, sid) if sessions_dir and _DESKTOP_SESSION_ID_RE.fullmatch(sid) else None
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
        sub_agents=list(data.get("sub_agents") or []),
        token_usage=_normalize_token_usage(data.get("token_usage")),
    )
    sess.messages = list(data.get("messages") or [])
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
        base_config = self._load_base_config()
        self.workspace_dir = _resolve_runtime_path(base_config.workspace_dir)
        self.sessions_dir = _resolve_runtime_path(base_config.sessions_dir)
        self.config_path = str(getattr(base_config, "_source_path", default_config_path()))
        self.config: Dict[str, Any] = _public_config_snapshot(base_config)
        self.sessions: Dict[str, Session] = {}
        self.active_session_id: Optional[str] = None
        self.session_idempotency_results: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._load_persisted_sessions()

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


    def _persist_sessions(self, *, raise_on_error: bool = False) -> None:
        """Atomically write all in-memory sessions to sessions.json."""
        store = os.path.join(self.sessions_dir, "sessions.json")
        tmp = f"{store}.{uuid.uuid4().hex}.tmp"
        try:
            with self.lock, SESSION_PERSISTENCE_LOCK:
                os.makedirs(self.sessions_dir, exist_ok=True)
                payload = {
                    "version": 1,
                    "activeSessionId": self.active_session_id,
                    "sessions": [_session_to_persistable(s) for s in self.sessions.values()],
                }
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, store)
        except Exception as exc:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            print(f"persist sessions failed: {exc}", file=sys.stderr)
            if raise_on_error:
                raise

    def _load_persisted_sessions(self) -> None:
        """Restore sessions from sessions.json on startup."""
        store = os.path.join(self.sessions_dir, "sessions.json")
        if not os.path.isfile(store):
            return
        try:
            with open(store, encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            print(f"load persisted sessions failed: {exc}", file=sys.stderr)
            return
        sessions = payload.get("sessions") or []
        for data in sessions:
            try:
                sess = _session_from_persisted(data, self.sessions_dir)
                self.sessions[sess.id] = sess
            except Exception as exc:
                print(f"skip broken persisted session: {exc}", file=sys.stderr)
        active = payload.get("activeSessionId")
        if active and active in self.sessions:
            self.active_session_id = active
        elif self.sessions:
            self.active_session_id = next(iter(self.sessions))

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
            if not sess.log_path and _DESKTOP_SESSION_ID_RE.fullmatch(sess.id):
                sess.log_path = _desktop_log_path(self.sessions_dir, sess.id)
            agent = ZeroAgent(config=config, session_log_path=sess.log_path)
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
                sess.restore_history = False
            effective_path = getattr(getattr(agent, "client", None), "log_path", None)
            if effective_path:
                sess.log_path = effective_path
            self._persist_sessions()
            return AgentRunner(agent)
        finally:
            with contextlib.suppress(Exception):
                os.chdir(old_cwd)
    def list_model_profiles(self):
        self.ensure_project_import_path()
        try:
            runner = AgentRunner(ZeroAgent())
            return runner.list_llm_profiles()
        except Exception as e:
            print(f"get model profiles failed: {e}", file=sys.stderr)
        return []

    def snapshot(self, sess: Session, include_messages: bool = True) -> dict:
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
            "tokenUsage": sess.token_usage,
            "groupId": sess.group_id,
            "subAgents": sess.sub_agents,
        }
        if include_messages:
            out["messages"] = list(sess.messages)
            out["partial"] = dict(sess.partial) if sess.partial else None
        return out

    def add_message(self, sess: Session, role: str, content: str, **extra) -> dict:
        sess.msg_seq += 1
        msg = {"id": sess.msg_seq, "role": role, "content": content, "ts": time.time()}
        msg.update(extra)
        sess.messages.append(msg)
        sess.updated_at = time.time()
        if role == "user" and content.strip() and sess.title == "New chat":
            sess.title = content.strip().replace("\n", " ")[:40]
        self._persist_sessions()
        return msg

    def create_session(self, cwd: Optional[str] = None) -> Session:
        sid = "sess-" + uuid.uuid4().hex[:12]
        sess = Session(
            id=sid,
            cwd=str(cwd or self.workspace_dir),
            log_path=_desktop_log_path(self.sessions_dir, sid),
        )
        with self.lock:
            self.sessions[sid] = sess
            self.active_session_id = sid
            self._persist_sessions()
        emit_session_state(sess, "created")
        return sess

    def get_session(self, sid: str) -> Session:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            return sess

    def _detach_response_log(self, sess: Session) -> str | None:
        """Detach an owned response log before aborting its worker."""
        if not _DESKTOP_SESSION_ID_RE.fullmatch(sess.id):
            return None
        owned_path = _desktop_log_path(self.sessions_dir, sess.id)
        if not sess.log_path or os.path.abspath(sess.log_path) != owned_path:
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

    def set_session_group(self, sid: str, group_id: str | None) -> dict:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(
                    text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False),
                    content_type="application/json",
                )
            previous_group_id = sess.group_id
            previous_updated_at = sess.updated_at
            normalized = str(group_id).strip() if group_id else None
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
            owned_path = self._detach_response_log(sess)
            if sess.agent and hasattr(sess.agent, "abort"):
                with contextlib.suppress(Exception):
                    sess.agent.abort()
            self.sessions.pop(sid)
            if self.active_session_id == sid:
                self.active_session_id = self._newest_session_id()
            if owned_path:
                with contextlib.suppress(OSError):
                    if os.path.isfile(owned_path):
                        os.remove(owned_path)
            result = {"ok": True, "sessionId": sid}
            remember_session_result(self.session_idempotency_results, "delete", sid, result)
            self._persist_sessions()
        emit_session_state(sess, "closed")
        return result

    def replace_session(self, sid: str) -> dict:
        with self.lock:
            key = ("replace", sid)
            prior_result = self.session_idempotency_results.get(key)
            if prior_result is not None:
                self.session_idempotency_results.move_to_end(key)
                return prior_result

            sess = self.sessions.pop(sid, None)
            if not sess:
                raise web.HTTPNotFound(
                    text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False),
                    content_type="application/json",
                )

            owned_path = self._detach_response_log(sess)
            if sess.agent and hasattr(sess.agent, "abort"):
                with contextlib.suppress(Exception):
                    sess.agent.abort()
            replacement_id = "sess-" + uuid.uuid4().hex[:12]
            replacement = Session(
                id=replacement_id,
                cwd=sess.cwd,
                log_path=_desktop_log_path(self.sessions_dir, replacement_id),
            )
            self.sessions[replacement.id] = replacement
            if self.active_session_id == sid:
                self.active_session_id = replacement.id
            if owned_path:
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
            self._persist_sessions()


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
        summary, full = continue_cmd.restore(runner, path)
        ui_messages = continue_cmd.extract_ui_messages(path)

        with self.lock:
            sess.messages.clear()
            sess.msg_seq = 0
            sess.partial = None
            sess.status = "idle"
            sess.last_error = ""
            sess.terminal_status = ""
            sess.terminal_reason = ""
            for msg in ui_messages:
                self.add_message(
                    sess,
                    str(msg.get("role") or "assistant"),
                    str(msg.get("content") or ""),
                )
            self.add_message(sess, "system", summary)
            if ui_messages:
                first_user = next((m for m in ui_messages if m.get("role") == "user"), None)
                if first_user:
                    sess.title = str(first_user.get("content") or "Restored").replace("\n", " ")[:40]
            sess.updated_at = time.time()

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

    def submit_prompt(self, sid: str, prompt: Any, images: Optional[list] = None) -> dict:
        prompt, image_ids = normalize_prompt(prompt, images)
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            if sess.status == "running":
                raise web.HTTPConflict(text=json.dumps({"error": "session is already running"}, ensure_ascii=False), content_type="application/json")
            extra = {}
            if image_ids:
                extra["image_ids"] = image_ids
            user_msg = self.add_message(sess, "user", prompt, **extra)
            sess.status = "running"
            sess.last_error = ""
            sess.terminal_status = ""
            sess.terminal_reason = ""
            sess.partial = {"id": sess.msg_seq + 1, "role": "assistant", "content": "", "ts": time.time(), "partial": True}
            t = threading.Thread(target=self.run_agent_turn, args=(sess, prompt, None), daemon=True, name=f"Turn-{sid}")
            sess.thread = t
            t.start()
            seq = sess.msg_seq
        emit_session_state(sess, "running")
        return {"ok": True, "sessionId": sid, "accepted": True, "userMessageId": user_msg["id"], "seq": seq}

    def _sync_token_usage(self, sess: Session) -> None:
        """Refresh session token/context usage from the live runner if available."""
        runner = sess.agent
        za = getattr(runner, "_agent", None)
        client = getattr(za, "client", None)
        if client is None:
            return
        active = getattr(client, "_active", client)
        stats = getattr(client, "usage_stats", None) or getattr(active, "usage_stats", {}) or {}
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

    def run_agent_turn(self, sess: Session, prompt: str, images: Optional[list] = None):
        try:
            with self.lock:
                if self.sessions.get(sess.id) is not sess:
                    return
                if sess.agent is None:
                    sess.agent = self.make_agent(sess)
                agent = sess.agent
                if self.sessions.get(sess.id) is not sess:
                    return
                if not hasattr(agent, "put_task"):
                    raise RuntimeError("AgentRunner object has no put_task method")
                display_q = agent.put_task(prompt, images=images or [])
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
                        if sess.partial is not None:
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
            with self.lock:
                sess.partial = None
                sess.terminal_status = terminal_status
                sess.terminal_reason = reason
                self._sync_token_usage(sess)

                if terminal_status == "completed":
                    import re as _re
                    text = _re.sub(r'\n*`{5}\n*\[Info\] Final response to user\.\n*`{5}\s*$', '', text)
                    self.add_message(sess, "assistant", text)
                    sess.status = "idle"
                    sess.last_error = ""
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
                else:
                    error_detail = reason or text or terminal_status
                    sess.status = "error"
                    sess.last_error = error_detail
                    self.add_message(sess, "error", text or error_detail)
                sess.updated_at = time.time()
                self._persist_sessions()
            emit_session_state(sess, sess.status)
        except Exception as e:
            tb = traceback.format_exc()
            with self.lock:
                sess.partial = None
                sess.status = "error"
                sess.last_error = str(e)
                sess.terminal_status = "failed"
                sess.terminal_reason = type(e).__name__
                self.add_message(sess, "error", str(e))
                self._persist_sessions()
            print(tb, file=sys.stderr)
            emit_session_state(sess, "error")

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
            }

    def cancel(self, sid: str) -> dict:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            if sess.agent and hasattr(sess.agent, "abort"):
                with contextlib.suppress(Exception):
                    sess.agent.abort()
            sess.status = "cancelled"
            sess.partial = None
            sess.terminal_status = "cancelled"
            sess.terminal_reason = "user_cancelled"
            sess.updated_at = time.time()
        emit_session_state(sess, "cancelled")
        return {"ok": True, "sessionId": sid}


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


manager = AgentManager()


# ---------------------------------------------------------------------------
# Transport layer: WS notification only
# ---------------------------------------------------------------------------

class WsHub:
    def __init__(self):
        self.websockets: Set[web.WebSocketResponse] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def emit(self, obj: dict):
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
    hub.emit({
        "type": "session-state",
        "sessionId": sess.id,
        "state": state_name,
        "status": sess.status,
        "terminalStatus": sess.terminal_status,
        "reason": sess.terminal_reason,
        "seq": sess.msg_seq,
        "updatedAt": sess.updated_at,
        "title": sess.title,
        "tokenUsage": sess.token_usage,
        "modelOverride": sess.model_override,
        "groupId": sess.group_id,
    })


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
    sess = manager.create_session(cwd=data.get("cwd") or data.get("path"))
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
    return json_ok(manager.submit_prompt(sid, prompt, images))


async def messages_handler(request):
    sid = request.match_info["sid"]
    after = int(request.query.get("after") or request.query.get("afterId") or 0)
    limit = int(request.query.get("limit") or 200)
    return json_ok(manager.messages(sid, after=after, limit=limit))


async def cancel_handler(request):
    sid = request.match_info["sid"]
    return json_ok(manager.cancel(sid))


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
    model_name = data.get("modelName") or data.get("modelNo")

    sess = manager.get_session(sid)
    sess.model_override = str(model_name) if model_name else None
    sess.updated_at = time.time()

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
    """List all session groups"""
    with manager.lock:
        session_groups = [(sess.id, sess.group_id) for sess in manager.sessions.values()]
    groups = {}
    for session_id, group_id in session_groups:
        if group_id:
            if group_id not in groups:
                groups[group_id] = {"id": group_id, "name": group_id, "sessionIds": []}
            groups[group_id]["sessionIds"].append(session_id)
    return json_ok({"ok": True, "groups": list(groups.values())})


async def get_agents_handler(request):
    """Get sub-agents for a session"""
    sid = request.match_info["sid"]
    sess = manager.get_session(sid)
    return json_ok({"ok": True, "sessionId": sid, "agents": sess.sub_agents})


async def cancel_agent_handler(request):
    """Cancel a sub-agent"""
    sid = request.match_info["sid"]
    aid = request.match_info["aid"]
    sess = manager.get_session(sid)

    # Find and mark agent as cancelled
    for agent in sess.sub_agents:
        if agent.get("id") == aid:
            agent["status"] = "cancelled"
            agent["updated_at"] = time.time()
            return json_ok({"ok": True, "sessionId": sid, "agentId": aid})
    return json_ok({"ok": False, "error": "agent not found"}, status=404)


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



def create_app(*, security: Optional[BridgeSecurity] = None, host: str = "127.0.0.1", port: int = 14168):
    security = _validate_bridge_security(security) if security is not None else load_bridge_security(host, port)
    app = web.Application(middlewares=[security_middleware])
    app["bridge_security"] = security
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/status", status_handler)
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
    app.router.add_post("/session/{sid}/cancel", cancel_handler)
    app.router.add_post("/session/{sid}/model", set_model_handler)
    app.router.add_get("/session/{sid}/tokens", get_tokens_handler)
    app.router.add_post("/session/{sid}/group", set_session_group_handler)
    app.router.add_get("/session/{sid}/agents", get_agents_handler)
    app.router.add_post("/session/{sid}/agents/{aid}/cancel", cancel_agent_handler)
    app.router.add_get("/worldline/{sid}", worldline_handler)
    app.router.add_post("/worldline/{sid}/restore", worldline_restore_handler)
    app.router.add_get("/groups", list_groups_handler)
    app.router.add_post("/path/open", path_open_handler)

    # Serve static frontend (desktop/static/)
    static_dir = APP_DIR / "desktop" / "static"

    async def index_handler(request):
        return web.FileResponse(static_dir / "index.html")

    app.router.add_get("/", index_handler)
    app.router.add_static("/", static_dir, show_index=False)

    async def on_startup(app):
        hub.loop = asyncio.get_running_loop()
        parent_pid = desktop_parent_pid()
        if parent_pid is not None:
            app["desktop_parent_monitor"] = asyncio.create_task(monitor_desktop_parent(parent_pid))

    async def on_cleanup(app):
        task = app.get("desktop_parent_monitor")
        if task is not None:
            task.cancel()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "14168"))
    security = load_bridge_security(host, port)
    url = f"http://{host}:{port}/"
    browser_url = f"{url}#token={urllib.parse.quote(security.token, safe='')}"
    print(f"ZeroAgent Web2 bridge: {url}  ws://{host}:{port}/ws", file=sys.stderr)
    if os.environ.get("ZA_DESKTOP_BRIDGE_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(browser_url)).start()
    web.run_app(create_app(security=security, host=host, port=port), host=host, port=port, print=None)
