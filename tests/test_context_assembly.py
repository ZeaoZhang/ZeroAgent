from __future__ import annotations

import pytest
from zero_agent.core.exceptions import ConfigError

from zero_agent.context.assembly import ContextAssembler
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.service import ContextService
from zero_agent.context.sources import EvidenceReceiptService
from zero_agent.context.store import InMemoryContextStore
from zero_agent.context.types import ContextObject, RuntimeContext
from zero_agent.skills.manifest import builtin_context_objects, load_builtin_catalog
def _put(store: InMemoryContextStore, obj: ContextObject) -> None:
    store.commit(obj, expected_revision=None)


def _object(
    uri: str,
    *,
    kind: str,
    content: str,
    owner: str = "alice",
    project: str | None = None,
    title: str = "",
    status: str = "active",
    key: str | None = None,
    scope: str | None = None,
    value: object | None = None,
    source_refs: tuple[str, ...] = (),
    schema_version: int = 1,
    tags: tuple[str, ...] = (),
) -> ContextObject:
    return ContextObject(
        object_id=uri.rsplit("/", 1)[-1],
        uri=uri,
        kind=kind,
        owner_id=owner,
        project_id=project,
        title=title,
        content=content,
        status=status,
        preference_key=key,
        scope_key=scope,
        value=value,
        source_refs=source_refs,
        schema_version=schema_version,
        tags=tags,
    )


def test_assembler_orders_scoped_preferences_project_and_skill_navigation(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    runtime = RuntimeContext("account", "alice", "session", project_id="project-1")
    receipts = EvidenceReceiptService(store)
    objects = (
        ("za://users/alice/memories/preferences/detail", "user_preference", "concise", "Answer detail", None, "response.detail_level", "user", "concise", "active"),
        ("za://users/alice/memories/preferences/candidate", "user_preference", "detailed", "Unverified detail", None, "response.detail_level", "user", "detailed", "candidate_unverified"),
        ("za://users/alice/memories/preferences/tone", "user_preference", "warm", "User tone", None, "response.tone", "user", "warm", "active"),
        ("za://projects/project-1/memories/preferences/tone", "user_preference", "formal", "Project tone", "project-1", "response.tone", "project:project-1", "formal", "active"),
    )
    for uri, kind, content, title, project, key, scope, value, status in objects:
        _put(store, _object(uri, kind=kind, content=content, title=title, project=project, key=key, scope=scope, value=value, status=status))
    decision = "决定采用 SQLite for the local manifest."
    source = receipts.record_user_message(runtime, task_id="assembly", event_sequence=1, message_id="decision-1", text=decision)
    _put(store, _object(
        "za://projects/project-1/memories/decisions/storage",
        kind="project_decision", content=decision, project="project-1", title="Storage decision",
        source_refs=(source.receipt_id,), schema_version=2,
    ))
    skill = next(obj for obj in builtin_context_objects() if obj.uri == "za://skills/builtin/code_review_principles")
    service = ContextService(store, runtime, sources=receipts, receipt_store=store, builtin_objects=(skill,))
    blocks = ContextAssembler(service).build(working={"key_info": "Continue with the migration tests."})
    assert "context-rules-version: 1" in blocks["context_rules"]
    assert '"value":"concise"' in blocks["context_user"]
    assert "Unverified detail" not in blocks["context_user"]
    assert "warm" not in blocks["context_user"]
    assert "formal" in blocks["context_project"]
    assert "SQLite" in blocks["context_project"]
    route = skill.uri.rsplit("/", 1)[-1]
    assert f"{route}|" in blocks["context_candidates"]
    assert "full skill body" not in blocks["context_candidates"]
    assert "Continue with the migration tests." in blocks["context_working"]


def test_assembler_enforces_utf8_byte_budget_and_reports_dropped_uris(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    runtime = RuntimeContext("account", "alice", "session", project_id="project-1")
    receipts = EvidenceReceiptService(store, max_excerpt_chars=3000)
    uri = "za://projects/project-1/memories/decisions/large"
    decision = "决定采用" + "界" * 2000
    source = receipts.record_user_message(
        runtime, task_id="budget", event_sequence=1, message_id="large-decision", text=decision,
    )
    _put(store, _object(
        uri, kind="project_decision", content=decision, project="project-1",
        title="Large decision", source_refs=(source.receipt_id,), schema_version=2,
    ))
    service = ContextService(store, runtime, sources=receipts, receipt_store=store)
    assembler = ContextAssembler(service, max_bytes=8000)
    blocks = assembler.build()
    counts = {name: len(text.encode("utf-8")) for name, text in blocks.items()}
    assert sum(counts.values()) <= 8000
    assert counts == assembler.last_stats["sections"]
    assert assembler.last_stats["total_bytes"] == sum(counts.values())
    assert assembler.last_stats["estimated_tokens"] == (sum(counts.values()) + 3) // 4
    assert uri in blocks["context_project"]
    assert "truncated" in blocks["context_project"]


def test_assembler_fails_instead_of_truncating_required_rules() -> None:
    store = InMemoryContextStore()
    service = ContextService(store, RuntimeContext("account", "alice", "session"))

    with pytest.raises(ConfigError, match="required context rules exceed"):
        ContextAssembler(service, max_bytes=1000).build()


def test_all_skill_triggers_fit_and_remain_visible_for_routing() -> None:
    store = InMemoryContextStore()
    service = ContextService(
        store,
        RuntimeContext("account", "alice", "session"),
        builtin_objects=builtin_context_objects(),
    )
    assembler = ContextAssembler(service, max_bytes=8000)
    blocks = assembler.build()
    route_lines = blocks["context_candidates"].splitlines()[1:]
    assert all(line.count("|") == 2 for line in route_lines)
    assert len(route_lines) > 0
    assert len(route_lines) == len(assembler.last_stats["included_objects"]["skills"])
    assert not assembler.last_stats["dropped_uris"]
    assert sum(len(block.encode("utf-8")) for block in blocks.values()) <= 8000
