"""Logical context reads and source-checked memory updates."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Dict, Generator

from zero_agent.core.config import AgentConfig
from zero_agent.context.errors import ContextError
from zero_agent.memory.service import MemoryOperation
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def register_context_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """Register model-facing context tools for the explicitly enabled local mode."""

    lang = config.resolved_tool_language
    descriptions = {
        "context_read": (
            "Read one authorized context object by logical URI and revision.",
            "按逻辑 URI 和修订号读取当前身份有权访问的上下文对象。",
        ),
        "context_list": (
            "List authorized objects under a logical context collection URI.",
            "列出逻辑上下文目录中当前身份获准访问的对象。",
        ),
        "context_search": (
            "Search active objects in the current user and project by default; provide an authorized scope to explicitly widen the search.",
            "默认只搜索当前用户和项目中的有效对象；传入有权访问的 scope 可显式扩大搜索范围。",
        ),
        "context_sources": (
            "Read authorized source receipts for citing evidence before memory updates.",
            "读取有权访问的来源回执，引用证据前使用。",
        ),
        "memory_update": (
            "Query context_sources before citing a receipt, then submit source-backed memory operations.",
            "引用回执前先查询 context_sources，再提交带来源回执的记忆更新。",
        ),
    }

    registry.register(ToolDefinition(
        name="context_read",
        description=descriptions["context_read"][0 if lang != "zh" else 1],
        parameters={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "revision": {"type": ["integer", "null"]},
                "view": {"type": "string", "enum": ["abstract", "overview", "detail"]},
                "cursor": {"type": ["string", "null"]},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 12000},
            },
            "required": ["uri"],
        },
        handler=_context_read,
        category="context",
        evidence_kind="read",
        promotes_task_state=False,
    ))
    registry.register(ToolDefinition(
        name="context_list",
        description=descriptions["context_list"][0 if lang != "zh" else 1],
        parameters={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "cursor": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["uri"],
        },
        handler=_context_list,
        category="context",
        evidence_kind="read",
        promotes_task_state=False,
    ))
    registry.register(ToolDefinition(
        name="context_search",
        description=descriptions["context_search"][0 if lang != "zh" else 1],
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "scope": {"type": ["string", "null"]},
                "kinds": {"type": "array", "items": {"type": "string"}},
                "cursor": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        },
        handler=_context_search,
        category="context",
        evidence_kind="read",
        promotes_task_state=False,
    ))
    registry.register(ToolDefinition(
        name="context_sources",
        description=descriptions["context_sources"][0 if lang != "zh" else 1],
        parameters={"type": "object", "properties": {
            "receipt_id": {"type": ["string", "null"]},
            "source_type": {"type": ["string", "null"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "cursor": {"type": ["string", "null"]},
        }},
        handler=_context_sources, category="context", evidence_kind="read", promotes_task_state=False,
    ))
    registry.register(ToolDefinition(
        name="memory_update",
        description=descriptions["memory_update"][0 if lang != "zh" else 1],
        parameters={
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["put", "retract", "purge"]},
                            "kind": {"type": "string", "enum": ["user_preference", "environment_fact", "project_decision", "experience"]},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "expected_revision": {"type": ["integer", "null"]},
                            "uri": {"type": ["string", "null"]},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                            "predicate": {"type": "string"},
                            "subject": {"type": ["string", "null"]},
                            "value": {},
                            "preference_key": {"type": ["string", "null"]},
                            "scope_key": {"type": ["string", "null"]},
                        },
                        "required": ["action", "kind", "source_refs", "expected_revision"],
                    },
                },
            },
            "required": ["idempotency_key", "operations"],
        },
        handler=_memory_update,
        category="memory",
        evidence_kind="memory",
        promotes_task_state=False,
        skill_action="memory.write",
    ))


def _context_sources(args: Dict[str, Any], _response: Any, handler: Any) -> Generator[str, None, dict]:
    try:
        if not isinstance(args, dict):
            raise ContextError("INVALID_URI", "Source receipt arguments must be an object.")
        if {key for key in args if not str(key).startswith("_")} - {"receipt_id", "source_type", "limit", "cursor"}:
            raise ContextError("INVALID_URI", "Unsupported source receipt arguments.")
        service = _service(handler, "context_service")
        receipt_id = args.get("receipt_id")
        source_type = args.get("source_type")
        if source_type is not None and source_type not in {"user_message", "tool_result", "resource_import"}:
            raise ContextError("INVALID_SOURCE", "Unsupported source type.")
        if receipt_id is not None:
            if not isinstance(receipt_id, str) or not receipt_id or source_type is not None or args.get("cursor") is not None or "limit" in args:
                raise ContextError("INVALID_URI", "Exact receipt reads cannot be combined with list filters.")
            return {"items": [_receipt_payload(service.read_receipt(receipt_id))], "next_cursor": None}
        task_id = getattr(getattr(handler, "task_contract", None), "task_id", None)
        if not isinstance(task_id, str) or not task_id:
            raise ContextError("INVALID_SOURCE", "The current task has no stable task identity.")
        return service.list_task_receipts(task_id=task_id, source_type=source_type, limit=_bounded_int(args.get("limit", 20), minimum=1, maximum=50), cursor=args.get("cursor"))
    except Exception as exc:
        return _error(exc)
    yield ""


def _receipt_payload(receipt: Any) -> dict:
    return {name: getattr(receipt, name) for name in ("receipt_id", "source_type", "event_sequence", "tool_name", "tool_contract_version", "outcome_status", "source_id", "source_revision", "source_content_sha256", "created_at", "excerpt", "result_fields", "project_id")}


def _context_read(args: Dict[str, Any], _response: Any, handler: Any) -> Generator[str, None, dict]:
    try:
        service = _service(handler, "context_service")
        uri = args.get("uri")
        view = args.get("view", "detail")
        if view not in {"abstract", "overview", "detail"}:
            raise ContextError("INVALID_URI", "Unsupported context view.")
        result = service.read_view(uri, view=view, revision=args.get("revision"))
        max_chars = _bounded_int(args.get("max_chars", 6000), minimum=1, maximum=12000)
        cursor_binding = (
            result["uri"],
            result["view"],
            result["revision"],
            result["content_sha256"],
        )
        offset = _parse_cursor(args.get("cursor"), cursor_binding)
        content = result["content"]
        page = content[offset : offset + max_chars]
        next_offset = offset + len(page)
        return {
            **result,
            "content": page,
            "next_cursor": (
                _make_cursor(cursor_binding, next_offset)
                if next_offset < len(content)
                else None
            ),
        }
    except Exception as exc:
        return _error(exc)
    yield ""


def _context_list(args: Dict[str, Any], _response: Any, handler: Any) -> Generator[str, None, dict]:
    try:
        result = _service(handler, "context_service").list(
            args.get("uri"),
            cursor=args.get("cursor"),
            limit=_bounded_int(args.get("limit", 50), minimum=1, maximum=100),
        )
        return {
            "items": [
                {
                    "uri": obj.uri,
                    "title": obj.title,
                    "kind": obj.kind,
                    "revision": obj.revision,
                    "status": obj.status,
                    "content_sha256": obj.content_sha256,
                }
                for obj in result["items"]
            ],
            "next_cursor": result["next_cursor"],
        }
    except Exception as exc:
        return _error(exc)
    yield ""


def _context_search(args: Dict[str, Any], _response: Any, handler: Any) -> Generator[str, None, dict]:
    try:
        result = _service(handler, "context_service").search(
            args.get("query"),
            scope=args.get("scope"),
            kinds=tuple(args.get("kinds", ())),
            cursor=args.get("cursor"),
            limit=_bounded_int(args.get("limit", 10), minimum=1, maximum=100),
        )
        return {
            "results": [item.__dict__ for item in result["results"]],
            "next_cursor": result["next_cursor"],
        }
    except Exception as exc:
        return _error(exc)
    yield ""


def _memory_update(args: Dict[str, Any], _response: Any, handler: Any) -> Generator[str, None, dict]:
    try:
        service = _service(handler, "memory_service")
        operations = tuple(
            MemoryOperation(
                action=item["action"],
                kind=item["kind"],
                source_refs=tuple(item["source_refs"]),
                expected_revision=item["expected_revision"],
                uri=item.get("uri"),
                title=item.get("title", ""),
                content=item.get("content", ""),
                predicate=item.get("predicate", ""),
                subject=item.get("subject"),
                value=item.get("value"),
                preference_key=item.get("preference_key"),
                scope_key=item.get("scope_key"),
            )
            for item in args["operations"]
        )
        result = service.update(operations, idempotency_key=args["idempotency_key"])
        return {
            "change_id": result.change_id,
            "status": result.status,
            "objects": [
                {
                    "uri": obj.uri,
                    "revision": obj.revision,
                    "status": obj.status,
                    "content_sha256": obj.content_sha256,
                }
                for obj in result.objects
            ],
            "retained_sessions": list(result.retained_sessions),
            "retained_backups": list(result.retained_backups),
            "retained_exports": list(result.retained_exports),
            "source_cleanup_complete": result.source_cleanup_complete,
            "source_cleanup_reason": result.source_cleanup_reason,
        }
    except Exception as exc:
        return _error(exc)
    yield ""


def _service(handler: Any, name: str) -> Any:
    service = getattr(handler, name, None)
    if service is None:
        raise ContextError("BACKEND_UNAVAILABLE", "Context runtime is not initialized for this task.")
    return service


def _make_cursor(binding: tuple[Any, ...], offset: int) -> str:
    raw = json.dumps((binding, offset), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_cursor(cursor: Any, binding: tuple[Any, ...]) -> int:
    if cursor is None:
        return 0
    try:
        if not isinstance(cursor, str):
            raise ValueError
        padding = "=" * (-len(cursor) % 4)
        bound, value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (TypeError, ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ContextError("INVALID_URI", "The context cursor is invalid.") from exc
    if not isinstance(bound, list) or tuple(bound) != binding:
        raise ContextError("REVISION_CONFLICT", "The context view changed during pagination.")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContextError("INVALID_URI", "The context cursor is invalid.")
    return value


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContextError("INVALID_URI", "A numeric argument is outside its allowed range.")
    return value


def _error(error: Exception) -> dict:
    if isinstance(error, ContextError):
        return {"status": "error", "code": error.code, "message": str(error)}
    return {"status": "error", "code": "INVALID_SOURCE", "message": "The context operation failed."}
