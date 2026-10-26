from __future__ import annotations

from zero_agent.context.service import ContextService
from zero_agent.context.store import InMemoryContextStore
from zero_agent.context.types import ContextObject, RuntimeContext
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.sources import EvidenceReceiptService
from zero_agent.core.handler import BaseHandler
from zero_agent.core.types import TaskContract, TaskMode
from zero_agent.memory.service import MemoryService
from zero_agent.tools.registry import ToolRegistry
from zero_agent.tools.builtin.context import register_context_tools
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.tools.builtin.context import _context_read

class _Handler:
    def __init__(self, service: ContextService) -> None:
        self.context_service = service


def _read(handler: _Handler, **args):
    generator = _context_read(args, None, handler)
    try:
        next(generator)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("context_read unexpectedly yielded progress")

def _dispatch(handler: BaseHandler, name: str, args: dict):
    generator = handler.dispatch(name, args, None)
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        return stop.value


def test_read_cursor_is_bound_to_uri_view_revision_and_content_hash() -> None:
    store = InMemoryContextStore()
    first_uri = "za://users/alice/memories/resources/first"
    second_uri = "za://users/alice/memories/resources/second"
    original = ContextObject(
        object_id="first",
        uri=first_uri,
        kind="resource",
        owner_id="alice",
        title="First",
        content="0123456789",
    )
    store.commit(original, expected_revision=None)
    store.commit(
        ContextObject(
            object_id="second",
            uri=second_uri,
            kind="resource",
            owner_id="alice",
            title="Second",
            content="0123456789",
        ),
        expected_revision=None,
    )
    handler = _Handler(ContextService(store, RuntimeContext("account", "alice", "session")))

    first_page = _read(handler, uri=first_uri, view="detail", max_chars=4)
    second_page = _read(
        handler,
        uri=first_uri,
        view="detail",
        max_chars=4,
        cursor=first_page["next_cursor"],
    )

    assert first_page["content"] == "0123"
    assert second_page["content"] == "4567"
    mismatch = _read(
        handler,
        uri=second_uri,
        view="detail",
        max_chars=4,
        cursor=first_page["next_cursor"],
    )
    assert mismatch["status"] == "error"
    assert mismatch["code"] == "REVISION_CONFLICT"

    store.commit(
        ContextObject(
            object_id="first",
            uri=first_uri,
            kind="resource",
            owner_id="alice",
            title="First",
            content="abcdefghij",
        ),
        expected_revision=1,
    )
    changed = _read(
        handler,
        uri=first_uri,
        view="detail",
        max_chars=4,
        cursor=first_page["next_cursor"],
    )
    assert changed["code"] == "REVISION_CONFLICT"


def test_read_cursor_rejects_malformed_payload() -> None:
    store = InMemoryContextStore()
    uri = "za://users/alice/memories/resources/one"
    store.commit(
        ContextObject(
            object_id="one",
            uri=uri,
            kind="resource",
            owner_id="alice",
            content="0123456789",
        ),
        expected_revision=None,
    )
    handler = _Handler(ContextService(store, RuntimeContext("account", "alice", "session")))

    result = _read(handler, uri=uri, view="detail", cursor="malformed", max_chars=4)

    assert result["code"] == "INVALID_URI"

def test_context_sources_dispatch_to_memory_update_with_real_receipt(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    runtime = RuntimeContext("local", "alice", "session-1")
    receipts = EvidenceReceiptService(store)
    context_service = ContextService(store, runtime, sources=receipts, receipt_store=store)
    config = AgentConfig(
        llm_backends={"default": LLMBackendConfig(name="default", provider="openai", api_key="test", api_base="https://api.invalid", model="test")},
        default_backend="default",
        workspace_dir=str(tmp_path),
        memory_dir=str(tmp_path / "legacy"),
    )
    registry = ToolRegistry()
    register_context_tools(registry, config)
    handler = BaseHandler(registry=registry, cwd=str(tmp_path))
    handler.context_service = context_service
    handler.memory_service = MemoryService(store, receipts, runtime)
    handler.runtime_context = runtime
    handler.task_contract = TaskContract("task-current", "记住以后回答简洁", TaskMode.OPEN)
    source = receipts.record_user_message(
        runtime, task_id="task-current", event_sequence=1, message_id="message-current",
        text="记住：以后回答请简洁。",
    )

    listed = _dispatch(handler, "context_sources", {})
    assert [item["receipt_id"] for item in listed.data["items"]] == [source.receipt_id]
    update = _dispatch(handler, "memory_update", {
        "idempotency_key": "dispatch-memory",
        "operations": [{
            "action": "put", "kind": "user_preference", "source_refs": [source.receipt_id],
            "expected_revision": None, "preference_key": "response.detail_level",
            "scope_key": "user", "value": "concise",
        }],
    })
    assert update.data["status"] == "active"
    assert update.data["objects"][0]["status"] == "active"
    assert store.list_objects()[0].value == "concise"

def test_context_sources_pages_current_task_and_reads_only_authorized_receipts(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    runtime = RuntimeContext("local", "alice", "session-1")
    receipts = EvidenceReceiptService(store)
    service = ContextService(store, runtime, sources=receipts, receipt_store=store)
    config = AgentConfig(
        llm_backends={"default": LLMBackendConfig(name="default", provider="openai", api_key="test", api_base="https://api.invalid", model="test")},
        default_backend="default",
        workspace_dir=str(tmp_path),
        memory_dir=str(tmp_path / "legacy"),
    )
    registry = ToolRegistry()
    register_context_tools(registry, config)
    handler = BaseHandler(registry=registry, cwd=str(tmp_path))
    handler.context_service = service
    handler.runtime_context = runtime
    handler.task_contract = TaskContract("task-current", "inspect sources", TaskMode.OPEN)

    first = receipts.record_user_message(
        runtime, task_id="task-current", event_sequence=1,
        message_id="message-1", text="first source excerpt",
    )
    second = receipts.record_user_message(
        runtime, task_id="task-current", event_sequence=2,
        message_id="message-2", text="second source excerpt",
    )
    receipts.record_user_message(
        runtime, task_id="task-other", event_sequence=1,
        message_id="message-other-task", text="not in this task listing",
    )
    other_user = RuntimeContext("local", "bob", "session-1")
    unauthorized = receipts.record_user_message(
        other_user, task_id="task-current", event_sequence=1,
        message_id="message-other-user", text="another principal's source",
    )

    project_runtime = RuntimeContext("local", "alice", "session-1", project_id="project-secret")
    project_receipt = receipts.record_user_message(
        project_runtime, task_id="task-current", event_sequence=3,
        message_id="message-project", text="project-only source",
    )
    first_page = _dispatch(handler, "context_sources", {"limit": 1})
    assert [item["receipt_id"] for item in first_page.data["items"]] == [first.receipt_id]
    assert first_page.data["next_cursor"]
    second_page = _dispatch(handler, "context_sources", {
        "limit": 1, "cursor": first_page.data["next_cursor"],
    })
    assert [item["receipt_id"] for item in second_page.data["items"]] == [second.receipt_id]
    assert second_page.data["next_cursor"] is None

    exact = _dispatch(handler, "context_sources", {"receipt_id": second.receipt_id})
    assert exact.data["items"][0]["excerpt"] == "second source excerpt"
    denied = _dispatch(handler, "context_sources", {"receipt_id": unauthorized.receipt_id})
    assert denied.data["status"] == "error"
    assert denied.data["code"] == "PERMISSION_DENIED"

    ungranted_listing = _dispatch(handler, "context_sources", {})
    assert [item["receipt_id"] for item in ungranted_listing.data["items"]] == [
        first.receipt_id, second.receipt_id,
    ]

    project_denied = _dispatch(handler, "context_sources", {"receipt_id": project_receipt.receipt_id})
    assert project_denied.data["status"] == "error"
    assert project_denied.data["code"] == "PERMISSION_DENIED"

    project_reader = RuntimeContext(
        "local", "alice", "session-1", capabilities=frozenset({"project:project-secret:read"}),
    )
    handler.context_service = ContextService(
        store, project_reader, sources=receipts, receipt_store=store,
    )
    project_allowed = _dispatch(handler, "context_sources", {"receipt_id": project_receipt.receipt_id})
    assert project_allowed.data["items"][0]["receipt_id"] == project_receipt.receipt_id

    receipts.record_user_message(
        runtime, task_id="task-current", event_sequence=3,
        message_id="message-3", text="changes the cursor snapshot",
    )
    stale = _dispatch(handler, "context_sources", {
        "limit": 1, "cursor": first_page.data["next_cursor"],
    })
    assert stale.data["status"] == "error"
    assert stale.data["code"] == "REVISION_CONFLICT"

    receipts.revoke(second.receipt_id, runtime, reason_code="user_retracted")
    revoked = _dispatch(handler, "context_sources", {"receipt_id": second.receipt_id})
    assert revoked.data["status"] == "error"
    assert revoked.data["code"] == "INVALID_SOURCE"
