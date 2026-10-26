"""Durable source receipt validation and typed claim policies."""

import hashlib

import pytest

from zero_agent.context.errors import ContextError
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.sources import (
    EvidenceReceiptService,
    ToolClaimRule,
    ToolSourcePolicy,
    ToolSourcePolicyRegistry,
    canonical_claim_content,
)
from zero_agent.context.types import RuntimeContext


def _runtime(user_id: str = "alice", project_id: str | None = None) -> RuntimeContext:
    return RuntimeContext("local", user_id, "session-1", project_id=project_id)


def test_user_receipt_persists_only_a_bounded_excerpt_and_source_hash(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    service = EvidenceReceiptService(store, max_excerpt_chars=12)
    message = "Please remember this preference for later."

    receipt = service.record_user_message(
        _runtime(),
        task_id="task-1",
        event_sequence=3,
        message_id="message-9",
        text=message,
    )

    loaded = store.get_evidence_receipt(receipt.receipt_id)
    assert loaded is not None
    assert loaded.excerpt == message[:12]
    assert loaded.source_revision == "message-9"
    assert loaded.source_content_sha256 == hashlib.sha256(message.encode()).hexdigest()
    assert loaded.user_id == "alice"
    assert loaded.project_id is None


def test_tool_receipts_project_only_allowlisted_fields_and_support_exact_claims(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    policies = ToolSourcePolicyRegistry(
        (
            ToolSourcePolicy(
                tool_name="environment_probe",
                contract_version="v1",
                allowed_fields=frozenset({"os_version"}),
                claim_rules=(ToolClaimRule("environment_fact", "environment.os_version", "os_version"),),
            ),
        )
    )
    service = EvidenceReceiptService(store, tool_policies=policies)
    receipt = service.record_tool_result(
        _runtime(),
        task_id="task-1",
        event_sequence=4,
        tool_name="environment_probe",
        contract_version="v1",
        status="success",
        result_fields={"os_version": "14.5", "api_key": "must-not-persist"},
        source_id="tool-call-3",
    )

    assert receipt.result_fields == {"os_version": "14.5"}
    assert service.validate_claim(
        receipt.receipt_id,
        _runtime(),
        kind="environment_fact",
        predicate="environment.os_version",
        value="14.5",
        subject="tool-call-3",
        scope_key="user:alice",
    ).receipt_id == receipt.receipt_id
    with pytest.raises(ContextError, match="cannot support this memory type"):
        service.validate_claim(
            receipt.receipt_id,
            _runtime(),
            kind="environment_fact",
            predicate="environment.os_version",
            value="14.6",
            subject="tool-call-3",
            scope_key="user:alice",
        )


def test_failed_tool_receipt_cannot_support_a_success_claim(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    policy = ToolSourcePolicy(
        "environment_probe",
        "v1",
        frozenset({"os_version"}),
        (ToolClaimRule("environment_fact", "environment.os_version", "os_version"),),
    )
    service = EvidenceReceiptService(
        store,
        tool_policies=ToolSourcePolicyRegistry((policy,)),
    )
    receipt = service.record_tool_result(
        _runtime(),
        task_id="task-1",
        event_sequence=1,
        tool_name="environment_probe",
        contract_version="v1",
        status="error",
        result_fields={"os_version": "14.5"},
        source_id="tool-call-failed",
    )

    with pytest.raises(ContextError, match="cannot support this memory type"):
        service.validate_claim(
            receipt.receipt_id,
            _runtime(),
            kind="environment_fact",
            predicate="environment.os_version",
            value="14.5",
            subject="tool-call-failed",
            scope_key="user:alice",
        )


def test_source_receipt_cannot_cross_user_boundary_and_revocation_is_append_only(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    service = EvidenceReceiptService(store)
    receipt = service.record_user_message(
        _runtime(),
        task_id="task-1",
        event_sequence=1,
        message_id="message-1",
        text="Remember that I prefer concise answers.",
    )
    assert service.validate_claim(
        receipt.receipt_id,
        _runtime(),
        kind="user_preference",
        predicate="response.detail_level",
        value="concise",
        subject="message-1",
        scope_key="user:alice",
    ).receipt_id == receipt.receipt_id
    with pytest.raises(ContextError) as denied:
        service.validate_claim(
            receipt.receipt_id,
            _runtime("bob"),
            kind="user_preference",
            predicate="response.detail_level",
            value="concise",
            subject="message-1",
            scope_key="user:bob",
        )
    assert denied.value.code == "PERMISSION_DENIED"

    service.revoke(receipt.receipt_id, _runtime(), reason_code="user_retracted")
    loaded = store.get_evidence_receipt(receipt.receipt_id)
    assert loaded is not None and loaded.revoked
    with pytest.raises(ContextError, match="missing or revoked"):
        service.validate_claim(
            receipt.receipt_id,
            _runtime(),
            kind="user_preference",
            predicate="response.detail_level",
            value="concise",
            subject="message-1",
            scope_key="user:alice",
        )


def test_project_receipt_persists_project_and_claims_remain_scoped(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    policy = ToolSourcePolicy(
        "environment_probe",
        "v1",
        frozenset({"os_version"}),
        (ToolClaimRule("environment_fact", "environment.os_version", "os_version"),),
    )
    service = EvidenceReceiptService(
        store,
        tool_policies=ToolSourcePolicyRegistry((policy,)),
    )
    project_runtime = _runtime(project_id="project-1")
    receipt = service.record_tool_result(
        project_runtime,
        task_id="task-1",
        event_sequence=1,
        tool_name="environment_probe",
        contract_version="v1",
        status="success",
        result_fields={"os_version": "14.5"},
        source_id="project-call-1",
    )

    loaded = store.get_evidence_receipt(receipt.receipt_id)
    assert loaded is not None
    assert loaded.project_id == "project-1"
    with pytest.raises(ContextError, match="another project scope"):
        service.validate_claim(
            receipt.receipt_id,
            _runtime(),
            kind="environment_fact",
            predicate="environment.os_version",
            value="14.5",
            subject="project-call-1",
            scope_key="user:alice",
        )

    scope_key = "project:project-1"
    assert canonical_claim_content(
        subject="project-call-1",
        predicate="environment.os_version",
        value="14.5",
        scope_key=scope_key,
    ) == '{"predicate":"environment.os_version","scope":"project:project-1","subject":"project-call-1","value":"14.5"}'
    assert service.validate_claim(
        receipt.receipt_id,
        project_runtime,
        kind="environment_fact",
        predicate="environment.os_version",
        value="14.5",
        subject="project-call-1",
        scope_key=scope_key,
    ).receipt_id == receipt.receipt_id



def test_missing_tool_policy_does_not_create_a_receipt(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    service = EvidenceReceiptService(store)

    with pytest.raises(ContextError) as error:
        service.record_tool_result(
            _runtime(),
            task_id="task-1",
            event_sequence=1,
            tool_name="unregistered",
            contract_version="v1",
            status="success",
            result_fields={"success": True},
            source_id="call-1",
        )
    assert error.value.code == "INVALID_SOURCE"
