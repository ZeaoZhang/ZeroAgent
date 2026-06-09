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

import asyncio, contextlib, copy, importlib, json, os, sys, webbrowser
import threading, time, traceback, uuid
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

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Agent management layer
# ---------------------------------------------------------------------------

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
    status: str = "idle"  # idle|running|error|cancelled
    agent: Any = None
    thread: Optional[threading.Thread] = None
    cancel_requested: bool = False
    last_error: str = ""


def _resolve_runtime_path(path: str | os.PathLike[str] | None) -> str:
    if not path:
        return ""
    return str(Path(path).expanduser().resolve())


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
            agent = ZeroAgent(config=config)
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
            "createdAt": sess.created_at,
            "updatedAt": sess.updated_at,
            "lastError": sess.last_error,
            "msgSeq": sess.msg_seq,
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
        return msg

    def create_session(self, cwd: Optional[str] = None) -> Session:
        sid = "sess-" + uuid.uuid4().hex[:12]
        sess = Session(id=sid, cwd=str(cwd or self.workspace_dir))
        with self.lock:
            self.sessions[sid] = sess
            self.active_session_id = sid
        emit_session_state(sess, "created")
        return sess

    def get_session(self, sid: str) -> Session:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            return sess

    def delete_session(self, sid: str) -> dict:
        with self.lock:
            sess = self.sessions.pop(sid, None)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            if self.active_session_id == sid:
                self.active_session_id = next(iter(self.sessions), None)
            if sess.agent and hasattr(sess.agent, "abort"):
                with contextlib.suppress(Exception):
                    sess.agent.abort()
        emit_session_state(sess, "closed")
        return {"ok": True, "sessionId": sid}

    def list_resume_sessions(self, limit: int = 10) -> list[dict]:
        self.ensure_project_import_path()
        continue_cmd = importlib.import_module("zero_agent.bots.shared.continue_cmd")

        continue_cmd.set_sessions_dir(self.sessions_dir)
        sessions = continue_cmd.list_sessions(exclude_pid=os.getpid())
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
            if sess.agent is None:
                sess.agent = self.make_agent(sess)

        self.ensure_project_import_path()
        continue_cmd = importlib.import_module("zero_agent.bots.shared.continue_cmd")

        continue_cmd.set_sessions_dir(self.sessions_dir)
        sessions = continue_cmd.list_sessions(exclude_pid=os.getpid())
        target_idx = index - 1
        if not (0 <= target_idx < len(sessions)):
            return {"ok": False, "error": f"索引越界（有效范围 1-{len(sessions)}）"}

        path = sessions[target_idx][0]
        summary, full = continue_cmd.restore(sess.agent, path)
        ui_messages = continue_cmd.extract_ui_messages(path)

        with self.lock:
            sess.messages.clear()
            sess.msg_seq = 0
            sess.partial = None
            sess.status = "idle"
            sess.last_error = ""
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
            sess.cancel_requested = False
            sess.last_error = ""
            sess.partial = {"id": sess.msg_seq + 1, "role": "assistant", "content": "", "ts": time.time(), "partial": True}
            t = threading.Thread(target=self.run_agent_turn, args=(sess, prompt, None), daemon=True, name=f"Turn-{sid}")
            sess.thread = t
            t.start()
            seq = sess.msg_seq
        emit_session_state(sess, "running")
        return {"ok": True, "sessionId": sid, "accepted": True, "userMessageId": user_msg["id"], "seq": seq}

    def run_agent_turn(self, sess: Session, prompt: str, images: Optional[list] = None):
        try:
            if sess.agent is None:
                sess.agent = self.make_agent(sess)
            agent = sess.agent
            full = ""
            if hasattr(agent, "put_task"):
                display_q = agent.put_task(prompt, images=images or [])
                pieces = []
                import queue as _queue
                while True:
                    if sess.cancel_requested:
                        break
                    try:
                        item = display_q.get(timeout=1.0)
                    except _queue.Empty:
                        continue
                    if isinstance(item, dict):
                        if item.get("next"):
                            text = str(item["next"])
                            pieces.append(text)
                            with self.lock:
                                if sess.partial is not None:
                                    sess.partial["content"] = "".join(pieces) if getattr(agent, "inc_out", False) else text
                                    sess.partial["ts"] = time.time()
                                    sess.updated_at = time.time()
                        if "done" in item:
                            full = str(item.get("done") or "")
                            break
                    else:
                        pieces.append(str(item))
                if not full and pieces:
                    full = pieces[-1] if not getattr(agent, "inc_out", False) else "".join(pieces)
            else:
                full = "AgentRunner object has no put_task method"
            if not full:
                full = "(completed)"
            if sess.cancel_requested:
                with self.lock:
                    sess.partial = None
                    # Ensure status stays cancelled (don't overwrite)
                    if sess.status != "cancelled":
                        sess.status = "cancelled"
                    sess.updated_at = time.time()
                emit_session_state(sess, "cancelled")
                return
            with self.lock:
                sess.partial = None
                # Strip trailing [Info] Final response to user. marker
                import re as _re
                full = _re.sub(r'\n*`{5}\n*\[Info\] Final response to user\.\n*`{5}\s*$', '', full)
                self.add_message(sess, "assistant", full)
                sess.status = "idle"
                sess.last_error = ""
            emit_session_state(sess, "idle")
        except Exception as e:
            tb = traceback.format_exc()
            with self.lock:
                sess.partial = None
                sess.status = "error"
                sess.last_error = str(e)
                self.add_message(sess, "error", str(e))
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
            }

    def cancel(self, sid: str) -> dict:
        with self.lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise web.HTTPNotFound(text=json.dumps({"error": f"session not found: {sid}"}, ensure_ascii=False), content_type="application/json")
            sess.cancel_requested = True
            if sess.agent and hasattr(sess.agent, "abort"):
                with contextlib.suppress(Exception):
                    sess.agent.abort()
            sess.status = "cancelled"
            sess.partial = None
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
        "seq": sess.msg_seq,
        "updatedAt": sess.updated_at,
        "title": sess.title,
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

def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())
    resp = await handler(request)
    for k, v in cors_headers().items():
        resp.headers[k] = v
    return resp


def json_ok(data: dict, status: int = 200):
    return web.json_response(data, status=status, headers=cors_headers(), dumps=lambda x: json.dumps(x, ensure_ascii=False, default=str))


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
        "workspaceDir": manager.workspace_dir,
        "configPath": manager.config_path,
        "sessionCount": len(manager.sessions),
        "activeSessionId": manager.active_session_id,
        "ws": "/ws",
        "transport": {"http": True, "wsEventsOnly": True},
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


def create_app():
    app = web.Application(middlewares=[cors_middleware])
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
    app.router.add_delete("/session/{sid}", delete_session_handler)
    app.router.add_post("/session/{sid}/prompt", prompt_handler)
    app.router.add_get("/session/{sid}/messages", messages_handler)
    app.router.add_post("/session/{sid}/cancel", cancel_handler)
    app.router.add_post("/path/open", path_open_handler)

    # Serve static frontend (desktop/static/)
    static_dir = APP_DIR / "desktop" / "static"

    async def index_handler(request):
        return web.FileResponse(static_dir / "index.html")

    app.router.add_get("/", index_handler)
    app.router.add_static("/", static_dir, show_index=False)

    async def on_startup(app):
        hub.loop = asyncio.get_running_loop()

    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "14168"))
    url = f"http://{host}:{port}/"
    print(f"ZeroAgent Web2 bridge: {url}  ws://{host}:{port}/ws", file=sys.stderr)
    if os.environ.get("ZA_DESKTOP_BRIDGE_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    web.run_app(create_app(), host=host, port=port, print=None)
