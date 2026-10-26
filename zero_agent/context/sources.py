"""Capture and validate durable evidence references for memory updates."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.types import ContextObject, EvidenceReceipt, RuntimeContext

_SOURCE_TYPES = {"user_message", "tool_result", "resource_import"}
_OUTCOME_STATUSES = {"success", "error", "interrupt", "unknown"}
_CLAIM_KINDS = {"environment_fact", "experience"}
_SAFE_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class ToolClaimRule:
    kind: str
    predicate: str
    source_field: str


@dataclass(frozen=True)
class ToolSourcePolicy:
    tool_name: str
    contract_version: str
    allowed_fields: frozenset[str]
    claim_rules: tuple[ToolClaimRule, ...] = ()


class EvidenceReceiptStore(Protocol):
    def create_evidence_receipt(self, receipt: EvidenceReceipt) -> EvidenceReceipt: ...

    def get_evidence_receipt(self, receipt_id: str) -> EvidenceReceipt | None: ...

    def list_evidence_receipts(
        self, *, account_id: str, user_id: str, session_id: str, task_id: str,
        source_type: str | None = None,
    ) -> Sequence[EvidenceReceipt]: ...

    def revoke_evidence_receipt(
        self, receipt_id: str, *, principal_id: str, reason_code: str
    ) -> None: ...


class ToolSourcePolicyRegistry:
    """Explicitly allowlisted tool fields and claims; unregistered tools prove nothing."""

    def __init__(self, policies: Sequence[ToolSourcePolicy] = ()) -> None:
        self._policies: dict[tuple[str, str], ToolSourcePolicy] = {}
        for policy in policies:
            self.register(policy)

    def register(self, policy: ToolSourcePolicy) -> None:
        if not policy.tool_name or not policy.contract_version:
            raise ValueError("tool source policies require a name and contract version")
        if any(not _SAFE_FIELD.fullmatch(name) for name in policy.allowed_fields):
            raise ValueError("tool source policy fields must be normalized identifiers")
        seen: set[tuple[str, str]] = set()
        for rule in policy.claim_rules:
            if rule.kind not in _CLAIM_KINDS or not rule.predicate or rule.source_field not in policy.allowed_fields:
                raise ValueError("claim rules must use supported kinds and allowlisted result fields")
            key = (rule.kind, rule.predicate)
            if key in seen:
                raise ValueError("duplicate claim rule")
            seen.add(key)
        key = (policy.tool_name, policy.contract_version)
        if key in self._policies:
            raise ValueError(f"duplicate source policy for {key[0]}@{key[1]}")
        self._policies[key] = policy

    def get(self, tool_name: str, contract_version: str) -> ToolSourcePolicy | None:
        return self._policies.get((tool_name, contract_version))

    def supports_claim(self, receipt: EvidenceReceipt, *, kind: str, predicate: str, value: object) -> bool:
        policy = self.get(receipt.tool_name, receipt.tool_contract_version)
        if policy is None or receipt.outcome_status != "success" or kind not in _CLAIM_KINDS:
            return False
        for rule in policy.claim_rules:
            if rule.kind == kind and rule.predicate == predicate:
                if rule.source_field not in receipt.result_fields:
                    return False
                actual = receipt.result_fields[rule.source_field]
                return _finite_json(actual) and _finite_json(value) and _same_typed_value(actual, value)
        return False

class EvidenceReceiptService:
    """Create source receipts from trusted entry and tool adapters."""

    def __init__(
        self,
        store: EvidenceReceiptStore,
        *,
        tool_policies: ToolSourcePolicyRegistry | None = None,
        max_excerpt_chars: int = 800,
    ) -> None:
        if max_excerpt_chars < 1:
            raise ValueError("max_excerpt_chars must be positive")
        self.store = store
        self.tool_policies = tool_policies or ToolSourcePolicyRegistry()
        self.max_excerpt_chars = max_excerpt_chars

    def record_user_message(
        self,
        runtime: RuntimeContext,
        *,
        task_id: str,
        event_sequence: int,
        message_id: str,
        text: str,
    ) -> EvidenceReceipt:
        if not message_id or not task_id or event_sequence < 0:
            raise ValueError("user message receipts require stable task, event, and message IDs")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("user message receipt text must be non-empty")
        excerpt = text.strip()[: self.max_excerpt_chars]
        receipt = EvidenceReceipt(
            receipt_id=str(uuid.uuid4()),
            source_type="user_message",
            account_id=runtime.account_id,
            user_id=runtime.user_id,
            session_id=runtime.session_id,
            project_id=runtime.project_id,
            task_id=task_id,
            event_sequence=event_sequence,
            source_id=message_id,
            outcome_status="success",
            excerpt=excerpt,
            source_revision=message_id,
            source_content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        return self.store.create_evidence_receipt(receipt)

    def record_tool_result(
        self,
        runtime: RuntimeContext,
        *,
        task_id: str,
        event_sequence: int,
        tool_name: str,
        contract_version: str,
        status: str,
        result_fields: dict[str, object],
        source_id: str,
        source_revision: str = "",
    ) -> EvidenceReceipt:
        if status not in _OUTCOME_STATUSES:
            raise ValueError("unsupported tool outcome status")
        policy = self.tool_policies.get(tool_name, contract_version)
        if policy is None:
            raise ContextError("INVALID_SOURCE", "No source policy is registered for this tool version.")
        projected = {
            key: value
            for key, value in result_fields.items()
            if key in policy.allowed_fields
        }
        if not projected and policy.allowed_fields and status == "success":
            raise ContextError("INVALID_SOURCE", "The tool result has no allowlisted evidence fields.")
        try:
            serialized = json.dumps(projected, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ContextError("INVALID_SOURCE", "Tool evidence fields must be JSON values.") from exc
        receipt = EvidenceReceipt(
            receipt_id=str(uuid.uuid4()),
            source_type="tool_result",
            account_id=runtime.account_id,
            user_id=runtime.user_id,
            session_id=runtime.session_id,
            project_id=runtime.project_id,
            task_id=task_id,
            event_sequence=event_sequence,
            source_id=source_id,
            tool_name=tool_name,
            tool_contract_version=contract_version,
            outcome_status=status,
            result_fields=projected,
            source_revision=source_revision or source_id,
            source_content_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        )
        return self.store.create_evidence_receipt(receipt)

    def record_resource_import(
        self,
        runtime: RuntimeContext,
        *,
        task_id: str,
        event_sequence: int,
        content_sha256: str,
        size_bytes: int,
    ) -> EvidenceReceipt:
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha256) or size_bytes < 0:
            raise ContextError("INVALID_SOURCE", "Resource imports require a valid content hash and size.")
        receipt = EvidenceReceipt(
            receipt_id=str(uuid.uuid4()),
            source_type="resource_import",
            account_id=runtime.account_id,
            user_id=runtime.user_id,
            session_id=runtime.session_id,
            project_id=runtime.project_id,
            task_id=task_id,
            event_sequence=event_sequence,
            source_id=f"sha256:{content_sha256}",
            outcome_status="success",
            result_fields={"size_bytes": size_bytes},
            source_revision=content_sha256,
            source_content_sha256=content_sha256,
        )
        return self.store.create_evidence_receipt(receipt)

    def validate_claim(
        self,
        receipt_id: str,
        runtime: RuntimeContext,
        *,
        kind: str,
        predicate: str,
        value: object,
        subject: str,
        scope_key: str,
    ) -> EvidenceReceipt:
        if (
            kind not in {"user_preference", "project_decision", "environment_fact", "experience"}
            or not isinstance(subject, str) or not subject
            or not isinstance(predicate, str) or not predicate
            or not _finite_json(value)
        ):
            raise ContextError("INVALID_SOURCE", "Invalid claim fields.")
        receipt = self.store.get_evidence_receipt(receipt_id)
        if receipt is None or receipt.revoked:
            raise ContextError("INVALID_SOURCE", "The source receipt is missing or revoked.")
        if receipt.account_id != runtime.account_id or receipt.user_id != runtime.user_id:
            raise ContextError("PERMISSION_DENIED", "The source receipt belongs to another principal.")
        if subject != receipt.source_id:
            raise ContextError("INVALID_SOURCE", "The claim subject does not match its source.")
        if scope_key == f"user:{runtime.user_id}":
            expected_project = None
        elif runtime.project_id and scope_key == f"project:{runtime.project_id}":
            expected_project = runtime.project_id
        else:
            raise ContextError("INVALID_SOURCE", "The claim scope does not match the active runtime.")
        if receipt.project_id != expected_project:
            raise ContextError("INVALID_SOURCE", "The source observation belongs to another project scope.")
        if receipt.source_type == "user_message" and kind in {"user_preference", "project_decision"} and receipt.outcome_status == "success":
            if kind == "project_decision" and not _is_project_decision(receipt.excerpt):
                raise ContextError("INVALID_SOURCE", "The source does not state a project decision.")
            return receipt
        if receipt.source_type == "tool_result" and kind in _CLAIM_KINDS and self.tool_policies.supports_claim(
            receipt, kind=kind, predicate=predicate, value=value
        ):
            return receipt
        raise ContextError("INVALID_SOURCE", "The source type cannot support this memory type.")
    def validate_stored_claim(self, obj: ContextObject, runtime: RuntimeContext) -> bool:
        """Revalidate provenance as its original owner while runtime supplies current read authority."""

        if obj.schema_version < 2 or obj.kind not in {"environment_fact", "experience", "project_decision"}:
            return False
        if obj.kind == "project_decision":
            if len(obj.source_refs) != 1:
                return False
            receipt = self.store.get_evidence_receipt(obj.source_refs[0])
            return (
                receipt is not None
                and not receipt.revoked
                and receipt.outcome_status == "success"
                and receipt.source_type == "user_message"
                and receipt.user_id == obj.owner_id
                and receipt.project_id == obj.project_id
                and obj.project_id is not None
                and receipt.excerpt == obj.content
                and _is_project_decision(receipt.excerpt)
            )
        try:
            payload = json.loads(obj.content)
            if not isinstance(payload, dict) or set(payload) != {"subject", "predicate", "value", "scope"}:
                return False
            if payload["scope"] != obj.scope_key:
                return False
            if canonical_claim_content(
                subject=payload["subject"], predicate=payload["predicate"],
                value=payload["value"], scope_key=obj.scope_key or "",
            ) != obj.content or len(obj.source_refs) != 1:
                return False
            receipt = self.store.get_evidence_receipt(obj.source_refs[0])
        except (ContextError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if (
            receipt is None or receipt.revoked or receipt.outcome_status != "success"
            or not receipt.account_id or receipt.user_id != obj.owner_id
            or receipt.project_id != obj.project_id
        ):
            return False
        expected_scope = f"project:{obj.project_id}" if obj.project_id else f"user:{obj.owner_id}"
        return (
            obj.kind in _CLAIM_KINDS
            and receipt.source_type == "tool_result"
            and payload["scope"] == expected_scope
            and payload["subject"] == receipt.source_id
            and self.tool_policies.supports_claim(
                receipt, kind=obj.kind, predicate=payload["predicate"], value=payload["value"]
            )
        )

    def list_current(
        self,
        runtime: RuntimeContext,
        *,
        task_id: str,
        source_type: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict:
        if (not task_id or source_type not in (None, *_SOURCE_TYPES)
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50):
            raise ContextError("INVALID_URI", "Invalid receipt listing parameters.")
        rows = sorted(
            (receipt for receipt in self.store.list_evidence_receipts(
                account_id=runtime.account_id, user_id=runtime.user_id,
                session_id=runtime.session_id, task_id=task_id, source_type=source_type,
            ) if not receipt.revoked and _receipt_read_allowed(receipt, runtime)),
            key=lambda receipt: (receipt.event_sequence, receipt.receipt_id),
        )
        snapshot = tuple((receipt.receipt_id, receipt.source_content_sha256, receipt.revoked) for receipt in rows)
        offset = _receipt_cursor(cursor, runtime, task_id, source_type, snapshot)
        page = rows[offset:offset + limit]
        next_offset = offset + len(page)
        return {
            "items": [_receipt_public(receipt) for receipt in page],
            "next_cursor": _make_receipt_cursor(runtime, task_id, source_type, snapshot, next_offset)
            if next_offset < len(rows) else None,
        }

    def read(self, receipt_id: str, runtime: RuntimeContext) -> EvidenceReceipt:
        receipt = self.store.get_evidence_receipt(receipt_id)
        if receipt is None or receipt.revoked:
            raise ContextError("INVALID_SOURCE", "The source receipt is missing or revoked.")
        if receipt.account_id != runtime.account_id or receipt.user_id != runtime.user_id:
            raise ContextError("PERMISSION_DENIED", "The source receipt is not authorized for this runtime.")
        if not _receipt_read_allowed(receipt, runtime):
            raise ContextError("PERMISSION_DENIED", "The project source receipt is not authorized for this runtime.")
        return receipt

    def revoke(self, receipt_id: str, runtime: RuntimeContext, *, reason_code: str) -> None:
        self.store.revoke_evidence_receipt(receipt_id, principal_id=runtime.user_id, reason_code=reason_code)


def _receipt_read_allowed(receipt: EvidenceReceipt, runtime: RuntimeContext) -> bool:
    if receipt.project_id is None:
        return True
    return (
        receipt.project_id == runtime.project_id
        or f"project:{receipt.project_id}:read" in runtime.capabilities
        or "context:admin" in runtime.capabilities
    )

def _finite_json(value: object) -> bool:
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def canonical_claim_content(*, subject: str, predicate: str, value: object, scope_key: str) -> str:
    if not isinstance(subject, str) or not subject or not isinstance(predicate, str) or not predicate or not isinstance(scope_key, str) or not scope_key or not _finite_json(value):
        raise ContextError("INVALID_SOURCE", "Invalid canonical claim fields.")
    return json.dumps({"subject": subject, "predicate": predicate, "value": value, "scope": scope_key}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _make_receipt_cursor(runtime, task_id, source_type, snapshot, offset):
    import base64
    binding = (runtime.account_id, runtime.user_id, runtime.session_id, task_id, source_type, snapshot)
    payload = (binding, offset)
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def _receipt_cursor(cursor, runtime, task_id, source_type, snapshot):
    if cursor is None: return 0
    try:
        import base64
        binding, offset = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        identity = [runtime.account_id, runtime.user_id, runtime.session_id, task_id, source_type]
        if not isinstance(binding, list) or binding[:5] != identity or not isinstance(offset, int) or offset < 0:
            raise ValueError
        if binding[5] != [list(x) for x in snapshot]:
            raise ContextError("REVISION_CONFLICT", "The receipt view changed during pagination.")
        return offset
    except ContextError:
        raise
    except Exception as exc:
        raise ContextError("INVALID_URI", "The receipt cursor is invalid.") from exc


def _receipt_public(r):
    return {"receipt_id": r.receipt_id, "source_type": r.source_type, "event_sequence": r.event_sequence, "tool_name": r.tool_name, "tool_contract_version": r.tool_contract_version, "outcome_status": r.outcome_status, "source_id": r.source_id, "source_revision": r.source_revision, "source_content_sha256": r.source_content_sha256, "created_at": r.created_at, "excerpt": r.excerpt, "result_fields": r.result_fields, "project_id": r.project_id}


def _is_project_decision(text: str) -> bool:
    return bool(re.search(
        r"决定|采用|改用|不再使用|选用|确认|项目应|应该|decide|we will|adopt|choose|approved|should use",
        text,
        re.IGNORECASE,
    ))

def _same_typed_value(actual: object, expected: object) -> bool:
    return type(actual) is type(expected) and actual == expected
