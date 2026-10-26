"""Memory service source, preference conflict, and scope contracts."""

import pytest

from zero_agent.context.errors import ContextError
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.sources import (
    EvidenceReceiptService,
    ToolClaimRule,
    ToolSourcePolicy,
    ToolSourcePolicyRegistry,
)
from zero_agent.context.types import RuntimeContext
from zero_agent.memory.service import MemoryOperation, MemoryService


def _runtime(*, project_id=None, capabilities=frozenset()):
    return RuntimeContext(
        "local",
        "alice",
        "session-1",
        project_id=project_id,
        capabilities=frozenset(capabilities),
    )


def _preference(
    receipt_id: str,
    value: str,
    *,
    scope_key: str = "user",
    expected_revision: int | None = None,
) -> MemoryOperation:
    return MemoryOperation(
        action="put",
        kind="user_preference",
        source_refs=(receipt_id,),
        expected_revision=expected_revision,
        preference_key="response.detail_level",
        scope_key=scope_key,
        value=value,
    )


def _user_receipt(sources, runtime, text, event=1):
    return sources.record_user_message(
        runtime,
        task_id=f"task-{event}",
        event_sequence=event,
        message_id=f"message-{event}",
        text=text,
    )


def test_explicit_preference_is_saved_without_tool_verification(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    source = _user_receipt(sources, runtime, "请记住：以后回答请简洁。")
    memory = MemoryService(store, sources, runtime)

    result = memory.update(
        (_preference(source.receipt_id, "concise"),),
        idempotency_key="memory-update-1",
    )

    assert result.status == "active"
    assert result.objects[0].preference_key == "response.detail_level"
    assert result.objects[0].scope_key == "user"
    assert result.objects[0].value == "concise"
    assert result.objects[0].source_refs == (source.receipt_id,)


def test_same_key_opposite_value_without_clear_replacement_pauses_both(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    first = _user_receipt(sources, runtime, "记住，我通常喜欢简洁回答。", 1)
    memory = MemoryService(store, sources, runtime)
    memory.update((_preference(first.receipt_id, "concise"),), idempotency_key="first")

    second = _user_receipt(sources, runtime, "请记住，以后回答详细一些。", 2)
    result = memory.update((_preference(second.receipt_id, "detailed"),), idempotency_key="second")

    assert result.status == "conflicted"
    assert {obj.status for obj in result.objects} == {"conflicted"}
    active = [obj for obj in store.list_objects() if obj.status == "active"]
    assert active == []


def test_explicit_same_key_correction_supersedes_old_preference(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    memory = MemoryService(store, sources, runtime)
    first = _user_receipt(sources, runtime, "记住，我通常喜欢简洁回答。", 1)
    memory.update((_preference(first.receipt_id, "concise"),), idempotency_key="first")

    correction = _user_receipt(
        sources,
        runtime,
        "请记住，以后改为详细回答，不要再简洁。",
        2,
    )
    result = memory.update(
        (_preference(correction.receipt_id, "detailed"),),
        idempotency_key="correction",
    )

    assert result.status == "active"
    assert {obj.status for obj in store.list_objects()} == {"active", "superseded"}


def test_non_persistent_preference_is_rejected(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    source = _user_receipt(sources, runtime, "我喜欢简洁回答。")

    with pytest.raises(ContextError) as error:
        MemoryService(store, sources, runtime).update(
            (_preference(source.receipt_id, "concise"),),
            idempotency_key="candidate",
        )

    assert error.value.code == "INVALID_SOURCE"
    assert "explicit lasting" in str(error.value)

def test_tool_claim_policy_can_activate_only_exact_observed_values(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    policy = ToolSourcePolicy(
        "environment_probe",
        "v1",
        frozenset({"os_version"}),
        (ToolClaimRule("environment_fact", "environment.os_version", "os_version"),),
    )
    sources = EvidenceReceiptService(
        store,
        tool_policies=ToolSourcePolicyRegistry((policy,)),
    )
    source = sources.record_tool_result(
        runtime,
        task_id="task-1",
        event_sequence=1,
        tool_name="environment_probe",
        contract_version="v1",
        status="success",
        result_fields={"os_version": "14.5"},
        source_id="tool-call-1",
    )
    memory = MemoryService(store, sources, runtime)
    operation = MemoryOperation(
        action="put",
        kind="environment_fact",
        source_refs=(source.receipt_id,),
        expected_revision=None,
        uri="za://users/me/memories/facts/os-version",
        subject="tool-call-1",
        predicate="environment.os_version",
        value="14.5",
        content='{"predicate":"environment.os_version","scope":"user:alice","subject":"tool-call-1","value":"14.5"}',
    )

    result = memory.update((operation,), idempotency_key="fact-1")
    assert result.status == "active"
    assert result.objects[0].uri == "za://users/alice/memories/facts/os-version"


def test_project_preference_requires_write_capability(tmp_path) -> None:
    runtime = _runtime(project_id="p-1")
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    source = _user_receipt(sources, runtime, "记住，以后这个项目用详细回答。")
    memory = MemoryService(store, sources, runtime)

    with pytest.raises(ContextError) as error:
        memory.update(
            (_preference(source.receipt_id, "detailed", scope_key="project:p-1"),),
            idempotency_key="project-pref",
        )
    assert error.value.code == "PERMISSION_DENIED"


def test_group_messages_do_not_write_personal_preferences_by_default(tmp_path) -> None:
    runtime = RuntimeContext(
        "local",
        "alice",
        "session-1",
        channel_scope="telegram:group:group-1",
    )
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    source = _user_receipt(sources, runtime, "记住，以后回答简洁。")

    with pytest.raises(ContextError) as error:
        MemoryService(store, sources, runtime).update(
            (_preference(source.receipt_id, "concise"),),
            idempotency_key="group-personal-memory",
        )

    assert error.value.code == "PERMISSION_DENIED"


def test_memory_update_idempotency_replays_after_revision_changes(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    source = _user_receipt(sources, runtime, "记住，以后请简洁回答。")
    memory = MemoryService(store, sources, runtime)
    operation = _preference(source.receipt_id, "concise")

    first = memory.update((operation,), idempotency_key="same-request")
    replay = memory.update((operation,), idempotency_key="same-request")

    assert replay.objects == first.objects
    assert len(store.list_objects()) == 1


def test_invalid_source_batch_does_not_commit_other_objects(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    source = _user_receipt(sources, runtime, "记住，以后请简洁回答。")
    memory = MemoryService(store, sources, runtime)
    valid = _preference(source.receipt_id, "concise")
    invalid = MemoryOperation(
        action="put",
        kind="environment_fact",
        source_refs=("forged-receipt",),
        expected_revision=None,
        uri="za://users/me/memories/facts/os-version",
        predicate="environment.os_version",
        value="14.5",
        content="macOS version 14.5",
    )

    with pytest.raises(ContextError):
        memory.update((valid, invalid), idempotency_key="atomic-fail")
    assert store.list_objects() == ()


def test_arbitrary_tool_claim_never_activates_without_subject(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    policy = ToolSourcePolicy("environment_probe", "v1", frozenset({"os_version"}),
                              (ToolClaimRule("environment_fact", "environment.os_version", "os_version"),))
    sources = EvidenceReceiptService(store, tool_policies=ToolSourcePolicyRegistry((policy,)))
    source = sources.record_tool_result(runtime, task_id="task-1", event_sequence=1,
        tool_name="environment_probe", contract_version="v1", status="success",
        result_fields={"os_version": "14.5"}, source_id="probe-1")
    result = MemoryService(store, sources, runtime).update((MemoryOperation(
        action="put", kind="environment_fact", source_refs=(source.receipt_id,), expected_revision=None,
        uri="za://users/me/memories/facts/os-version", predicate="environment.os_version",
        value="14.5", content="arbitrary model text"),), idempotency_key="candidate-claim")
    assert result.status == "candidate_unverified"


def test_resource_path_is_rejected_by_memory_model(tmp_path) -> None:
    runtime = _runtime(project_id="p-1", capabilities={"project:p-1:write"})
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    source = sources.record_resource_import(runtime, task_id="task-1", event_sequence=1,
        content_sha256="0" * 64, size_bytes=1)
    with pytest.raises(ContextError) as error:
        MemoryService(store, sources, runtime).update((MemoryOperation(
            action="put", kind="resource", source_refs=(source.receipt_id,), expected_revision=None,
            uri="za://projects/p-1/resources/docs/item", content="resource"),), idempotency_key="resource-write")
    assert error.value.code == "INVALID_URI"

def test_project_decision_requires_a_receipt_from_the_same_project(tmp_path) -> None:
    runtime = _runtime(project_id="p-1", capabilities={"project:p-1:write", "project:p-2:read"})
    other_project = _runtime(project_id="p-2", capabilities={"project:p-2:write"})
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    statement = "项目决定采用 SQLite。"
    source = _user_receipt(sources, other_project, statement)

    with pytest.raises(ContextError) as error:
        MemoryService(store, sources, runtime).update((MemoryOperation(
            action="put", kind="project_decision", source_refs=(source.receipt_id,),
            expected_revision=None, uri="za://projects/p-1/memories/decisions/storage",
            content=statement,
        ),), idempotency_key="cross-project-decision")

    assert error.value.code == "INVALID_SOURCE"
    assert store.list_objects() == ()


def test_preference_update_does_not_read_unrelated_damaged_revision(tmp_path) -> None:
    from zero_agent.context.types import ContextObject

    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    foreign_uri = "za://users/bob/memories/preferences/style"
    store.commit(ContextObject(
        object_id="style", uri=foreign_uri, kind="user_preference", owner_id="bob",
        content="unrelated damaged object",
    ), expected_revision=None)
    with store._connection() as connection:
        relative_path = connection.execute(
            "SELECT relative_path FROM revisions WHERE uri=?", (foreign_uri,),
        ).fetchone()["relative_path"]
    (store.root / relative_path).unlink()
    source = _user_receipt(sources, runtime, "记住，以后回答请简洁。")

    result = MemoryService(store, sources, runtime).update(
        (_preference(source.receipt_id, "concise"),), idempotency_key="isolated-preference",
    )

    assert result.status == "active"
    assert result.objects[0].value == "concise"
    visible = store.list_objects(uri_filter=lambda uri: uri.startswith("za://users/alice/"))
    assert visible == result.objects


def test_purge_removes_revisions_and_reports_retained_session_and_backup(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    memory = MemoryService(store, sources, runtime)
    original_source = _user_receipt(sources, runtime, "记住，以后回答请简洁。", 1)
    saved = memory.update(
        (_preference(original_source.receipt_id, "concise"),),
        idempotency_key="save-before-purge",
    )
    obj = saved.objects[0]
    with store._connection() as connection:
        relative_path = connection.execute(
            "SELECT relative_path FROM revisions WHERE uri=?", (obj.uri,)
        ).fetchone()[0]
    backup = store.backup_to(tmp_path / "backup")
    export = store.export_json(tmp_path / "export.json")
    purge_source = _user_receipt(sources, runtime, "请忘掉这个偏好。", 2)
    operation = MemoryOperation(
        action="purge",
        kind="user_preference",
        source_refs=(purge_source.receipt_id,),
        expected_revision=obj.revision,
        uri=obj.uri,
    )

    result = memory.update((operation,), idempotency_key="purge-preference")
    retry = memory.update((operation,), idempotency_key="purge-preference")

    assert result.status == "purged"
    assert result.retained_sessions == ("session-1",)
    assert len(result.retained_backups) == 1
    assert result.retained_backups[0].startswith("backup:")
    assert len(result.retained_exports) == 1
    assert result.retained_exports[0].startswith("export:")
    assert result.source_cleanup_complete
    assert result.source_cleanup_reason == ""
    assert retry == result
    assert store.get(obj.uri) is None
    assert not (store.root / relative_path).exists()
    redacted = store.get_evidence_receipt(original_source.receipt_id)
    assert redacted is not None and redacted.revoked and redacted.excerpt == ""
    with store._connection() as connection:
        purge_row = connection.execute(
            "SELECT uri_sha256 FROM purge_events WHERE idempotency_key=?",
            ("purge-preference",),
        ).fetchone()
    assert purge_row["uri_sha256"] != obj.uri


def test_pending_purge_retries_physical_cleanup_after_failure(tmp_path, monkeypatch) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    memory = MemoryService(store, sources, runtime)
    original_source = _user_receipt(sources, runtime, "记住，以后回答请简洁。", 1)
    obj = memory.update(
        (_preference(original_source.receipt_id, "concise"),),
        idempotency_key="save-before-recoverable-purge",
    ).objects[0]
    with store._connection() as connection:
        relative_path = connection.execute(
            "SELECT relative_path FROM revisions WHERE uri=?", (obj.uri,)
        ).fetchone()[0]
    purge_source = _user_receipt(sources, runtime, "请忘掉这个偏好。", 2)
    operation = MemoryOperation(
        action="purge", kind="user_preference", source_refs=(purge_source.receipt_id,),
        expected_revision=obj.revision, uri=obj.uri,
    )
    finish_cleanup = store._finish_purge_cleanup
    monkeypatch.setattr(store, "_finish_purge_cleanup", lambda *args: False)

    pending = memory.update((operation,), idempotency_key="recover-purge")
    assert pending.status == "purge_pending"
    assert store.get(obj.uri) is None
    assert (store.root / relative_path).is_file()
    assert pending.retained_sessions == ("session-1",)

    monkeypatch.setattr(store, "_finish_purge_cleanup", finish_cleanup)
    recovered = memory.update((operation,), idempotency_key="recover-purge")
    assert recovered.status == "purged"
    assert not (store.root / relative_path).exists()
    assert recovered.retained_sessions == ("session-1",)
    assert sources.store.get_evidence_receipt(purge_source.receipt_id).revoked


def test_explicit_user_request_for_multiple_named_memories_requires_single_object_batches(tmp_path) -> None:
    runtime = _runtime()
    store = LocalContextStore(tmp_path / "context")
    sources = EvidenceReceiptService(store)
    memory = MemoryService(store, sources, runtime)
    original = _user_receipt(sources, runtime, "记住，以后回答简洁。", 1)
    saved = memory.update(
        (_preference(original.receipt_id, "concise"),),
        idempotency_key="save-before-multi-delete",
    ).objects[0]
    correction = _user_receipt(sources, runtime, "记住，以后改为详细回答。", 2)
    corrected = memory.update(
        (_preference(correction.receipt_id, "detailed"),),
        idempotency_key="correct-before-multi-delete",
    )
    deletion = _user_receipt(
        sources, runtime, "请彻底删除简洁和详细这两个回答偏好。", 3,
    )
    operations = tuple(
        MemoryOperation(
            action="purge", kind="user_preference", source_refs=(deletion.receipt_id,),
            expected_revision=obj.revision, uri=obj.uri,
        )
        for obj in corrected.objects
    )

    with pytest.raises(ContextError, match="A purge must be the only operation"):
        memory.update(operations, idempotency_key="purge-two-preferences")

    assert store.get(saved.uri) is not None
    assert len(store.list_objects()) == 2
