"""会话历史查询工具：直接读取 sessions.sqlite3.

此前 agent 想回顾“上一个会话”时只能满仓库找日志、再把历史导出成 txt 再
读取，既慢又容易找错。这里提供一个只读工具：
``session_history(action=list|read|search)``，直接查桌面会话库
``<sessions_dir>/sessions.sqlite3`` 的 ``sessions`` / ``messages`` 表。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, Generator, List, Optional
from urllib.parse import quote

from zero_agent.core.config import AgentConfig
from zero_agent.tools.registry import ToolRegistry

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 200
_MAX_CONTENT_CHARS = 4000
_SNIPPET_RADIUS = 120
_KIND_KEYS = ("kind", "queue_id", "image_ids", "file_ids", "file_names")


def _t(zh: str, en: str, lang: str) -> str:
    return zh if lang == "zh" else en


def _sessions_db(config: AgentConfig) -> str:
    sessions_dir = str(getattr(config, "sessions_dir", "") or "").strip()
    if not sessions_dir:
        return ""
    return os.path.join(os.path.expanduser(sessions_dir), "sessions.sqlite3")


def _connect_readonly(path: str) -> sqlite3.Connection:
    uri = "file:" + quote(os.path.abspath(path), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _metadata(raw: Any) -> Dict[str, Any]:
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _message_payload(row: sqlite3.Row, *, full: bool) -> Dict[str, Any]:
    content = str(row["content"] or "")
    truncated = 0
    if not full and len(content) > _MAX_CONTENT_CHARS:
        truncated = len(content) - _MAX_CONTENT_CHARS
        content = content[:_MAX_CONTENT_CHARS]
    payload: Dict[str, Any] = {
        "id": int(row["message_id"]),
        "role": str(row["role"] or ""),
        "timestamp": float(row["timestamp"] or 0.0),
        "content": content,
    }
    meta = _metadata(row["metadata_json"] if "metadata_json" in row.keys() else "")
    for key in _KIND_KEYS:
        if meta.get(key):
            payload[key] = meta[key]
    if truncated:
        payload["truncated_chars"] = truncated
    return payload


def _make_handler(config: AgentConfig):
    lang = config.resolved_tool_language

    def _handler(
        args: Dict[str, Any],
        _response: Any = None,
        _handler: Any = None,
    ) -> Generator[str, None, dict]:
        action = str(args.get("action") or "list").strip().lower()
        if action not in {"list", "read", "search"}:
            return {
                "status": "error",
                "msg": _t(
                    "action 只能是 list / read / search",
                    "action must be list / read / search",
                    lang,
                ),
            }
        db_path = _sessions_db(config)
        if not db_path:
            return {
                "status": "error",
                "msg": _t(
                    "未配置 sessions_dir，无法定位会话库",
                    "sessions_dir is not configured; cannot locate the session store",
                    lang,
                ),
            }
        if not os.path.isfile(db_path):
            return {
                "status": "error",
                "db": db_path,
                "msg": _t(
                    "会话库不存在（尚未产生桌面会话，或 sessions_dir 指向别处）",
                    "session store not found (no desktop session yet, or sessions_dir differs)",
                    lang,
                ),
            }

        limit = args.get("limit", _DEFAULT_LIMIT)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(limit, _MAX_LIMIT))

        try:
            conn = _connect_readonly(db_path)
        except sqlite3.Error as exc:
            return {"status": "error", "db": db_path, "msg": f"open failed: {exc}"}

        try:
            tables = _tables(conn)
            if "sessions" not in tables or "messages" not in tables:
                return {
                    "status": "error",
                    "db": db_path,
                    "msg": _t(
                        "会话库存在但缺少 sessions/messages 表",
                        "session store exists but lacks sessions/messages tables",
                        lang,
                    ),
                }
            session_columns = _columns(conn, "sessions")
            select_extra = [
                column
                for column in ("msg_seq", "terminal_status", "terminal_reason", "updated_at")
                if column in session_columns
            ]

            if action == "list":
                rows = conn.execute(
                    """
                    SELECT s.*,
                           (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count
                      FROM sessions s
                     ORDER BY s.updated_at DESC
                     LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                sessions: List[Dict[str, Any]] = []
                for row in rows:
                    item: Dict[str, Any] = {
                        "sessionId": str(row["id"]),
                        "title": str(row["title"] or ""),
                        "messageCount": int(row["message_count"] or 0),
                        "updatedAt": float(row["updated_at"] or 0.0),
                    }
                    for column in select_extra:
                        if column != "updated_at":
                            item[column] = row[column]
                    sessions.append(item)
                yield f"[Info] {len(sessions)} session(s) from {db_path}\n"
                return {"status": "success", "db": db_path, "sessions": sessions}

            if action == "read":
                session_id = str(args.get("session_id") or "").strip()
                if not session_id:
                    return {
                        "status": "error",
                        "msg": _t(
                            "read 需要 session_id（先用 action=list 查）",
                            "read requires session_id (call action=list first)",
                            lang,
                        ),
                    }
                after_id = args.get("after_id", 0)
                try:
                    after_id = int(after_id)
                except (TypeError, ValueError):
                    after_id = 0
                full = bool(args.get("full", False))
                rows = conn.execute(
                    """
                    SELECT message_id, role, content, timestamp, metadata_json
                      FROM messages
                     WHERE session_id = ? AND message_id > ?
                     ORDER BY message_id ASC
                     LIMIT ?
                    """,
                    (session_id, after_id, limit),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                messages = [_message_payload(row, full=full) for row in rows]
                yield f"[Info] {len(messages)}/{int(total)} message(s) for {session_id}\n"
                return {
                    "status": "success",
                    "db": db_path,
                    "sessionId": session_id,
                    "totalMessages": int(total),
                    "messages": messages,
                }

            query = str(args.get("query") or "").strip()
            if not query:
                return {
                    "status": "error",
                    "msg": _t(
                        "search 需要 query",
                        "search requires query",
                        lang,
                    ),
                }
            session_id = str(args.get("session_id") or "").strip()
            like = f"%{query}%"
            if session_id:
                rows = conn.execute(
                    """
                    SELECT m.session_id, m.message_id, m.role, m.content, m.timestamp,
                           m.metadata_json, s.title
                      FROM messages m JOIN sessions s ON s.id = m.session_id
                     WHERE m.session_id = ? AND m.content LIKE ?
                     ORDER BY m.message_id DESC
                     LIMIT ?
                    """,
                    (session_id, like, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT m.session_id, m.message_id, m.role, m.content, m.timestamp,
                           m.metadata_json, s.title
                      FROM messages m JOIN sessions s ON s.id = m.session_id
                     WHERE m.content LIKE ?
                     ORDER BY m.timestamp DESC
                     LIMIT ?
                    """,
                    (like, limit),
                ).fetchall()
            hits: List[Dict[str, Any]] = []
            for row in rows:
                content = str(row["content"] or "")
                position = content.lower().find(query.lower())
                start = max(0, position - _SNIPPET_RADIUS)
                snippet = content[start:start + _SNIPPET_RADIUS * 2]
                hits.append({
                    "sessionId": str(row["session_id"]),
                    "title": str(row["title"] or ""),
                    "messageId": int(row["message_id"]),
                    "role": str(row["role"] or ""),
                    "timestamp": float(row["timestamp"] or 0.0),
                    "snippet": snippet,
                })
            yield f"[Info] {len(hits)} match(es) for {query!r}\n"
            return {"status": "success", "db": db_path, "query": query, "matches": hits}
        except sqlite3.Error as exc:
            return {"status": "error", "db": db_path, "msg": f"query failed: {exc}"}
        finally:
            conn.close()

    return _handler


def register_history_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """注册会话历史查询工具.

    Args:
        registry: 工具注册中心.
        config: Agent 配置.
    """
    from zero_agent.tools.registry import ToolDefinition

    lang = config.resolved_tool_language
    db_path = _sessions_db(config) or "<sessions_dir>/sessions.sqlite3"

    registry.register(ToolDefinition(
        name="session_history",
        description=_t(
            "直接查询桌面会话 SQLite 历史（不要导出 txt 再读，也不要猜路径）。"
            f"库文件：{db_path}，表：sessions / messages。\n"
            "action=list：列出最近会话（id/title/消息数/更新时间）；"
            "action=read：按 session_id 读取消息（after_id 增量、limit 条数、full=true 不截断）；"
            "action=search：按 query 关键字跨会话搜索（可加 session_id 限定）。",
            "Query the desktop session SQLite store directly (do not export to txt or "
            f"guess paths). DB: {db_path}; tables: sessions / messages.\n"
            "action=list: recent sessions (id/title/message count/updated); "
            "action=read: messages for a session_id (after_id, limit, full=true); "
            "action=search: keyword search across sessions (optional session_id).",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "read", "search"],
                    "description": _t(
                        "list=列出会话，read=读消息，search=搜关键字",
                        "list=recent sessions, read=messages, search=keyword",
                        lang,
                    ),
                    "default": "list",
                },
                "session_id": {
                    "type": "string",
                    "description": _t(
                        "会话 id（read 必填；search 可选）",
                        "session id (required for read, optional for search)",
                        lang,
                    ),
                },
                "query": {
                    "type": "string",
                    "description": _t(
                        "search 关键字",
                        "keyword for search",
                        lang,
                    ),
                },
                "after_id": {
                    "type": "integer",
                    "description": _t(
                        "read：只返回 message_id 大于该值的消息",
                        "read: return messages with id greater than this",
                        lang,
                    ),
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": _t(
                        f"返回条数（默认 {_DEFAULT_LIMIT}，最大 {_MAX_LIMIT}）",
                        f"max rows (default {_DEFAULT_LIMIT}, cap {_MAX_LIMIT})",
                        lang,
                    ),
                    "default": _DEFAULT_LIMIT,
                },
                "full": {
                    "type": "boolean",
                    "description": _t(
                        "read：true 时返回完整内容（默认截断到 4000 字符）",
                        "read: return full content instead of truncating at 4000 chars",
                        lang,
                    ),
                    "default": False,
                },
            },
            "required": ["action"],
        },
        handler=_make_handler(config),
        evidence_kind="read",
        category="memory",
    ))
