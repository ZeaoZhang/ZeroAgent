"""Source-aware memory updates over a revisioned ContextStore."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import quote

from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.sources import EvidenceReceiptService, canonical_claim_content
from zero_agent.context.types import (
    CollectionPrecondition,
    ContextObject,
    ContextStore,
    EvidenceReceipt,
    PurgeResult,
    RuntimeContext,
    collection_fingerprint,
)
from zero_agent.context.uri import parse_context_uri

_PERSISTENT_INTENT = re.compile(
    r"记住|记下|以后|今后|从现在起|默认|通常|长期|remember|from now on|"
    r"going forward|by default|usually|in general|always|改回|恢复为|恢复成|"
    r"switch back to|change back to",
    re.IGNORECASE,
)
_REPLACEMENT_INTENT = re.compile(
    r"改为|改成|更正|不要再|以后用|替换为|换成|change .* to|"
    r"update .* to|no longer|instead|switch to|改回|恢复为|恢复成|"
    r"switch back to|change back to",
    re.IGNORECASE,
)
_TEMPORARY_INTENT = re.compile(
    r"这次|本次|当前回答|仅此一次|临时|暂时|"
    r"\bthis time\b|\bfor this answer\b|\bjust this once\b|"
    r"\bfor now\b|\btemporarily\b",
    re.IGNORECASE,
)
_PROJECT_DECISION_INTENT = re.compile(
    r"决定|采用|改用|不再使用|选用|确认|项目应|应该|decide|we will|adopt|"
    r"choose|approved|should use",
    re.IGNORECASE,
)
_PURGE_INTENT = re.compile(
    r"忘掉|忘记|删除|清除|彻底移除|forget|delete|purge|erase|remove",
    re.IGNORECASE,
)
_RETRACTION_INTENT = re.compile(r"撤回|收回|不再记住|retract|withdraw|undo", re.IGNORECASE)


@dataclass(frozen=True)
class PreferenceKeySpec:
    key: str
    allowed_values: frozenset[str] = frozenset()
    allow_free_text: bool = False
    max_value_chars: int = 160
    allow_project_scope: bool = True


class PreferenceKeyRegistry:
    """Versioned preference keys and value schemas accepted by memory updates."""

    def __init__(self, specs: Sequence[PreferenceKeySpec] | None = None) -> None:
        defaults = specs or (
            PreferenceKeySpec(
                "response.detail_level",
                frozenset({"concise", "standard", "detailed"}),
            ),
            PreferenceKeySpec("response.tone", allow_free_text=True),
            PreferenceKeySpec("response.language", allow_free_text=True),
            PreferenceKeySpec("response.format", allow_free_text=True),
            PreferenceKeySpec(
                "response.proactivity",
                frozenset({"low", "normal", "high"}),
            ),
        )
        self._specs = {spec.key: spec for spec in defaults}
        if len(self._specs) != len(defaults):
            raise ValueError("preference key registry contains duplicate keys")

    def validate(self, key: str, value: object, *, scope_key: str) -> PreferenceKeySpec:
        spec = self._specs.get(key)
        if spec is None:
            raise ContextError("INVALID_SOURCE", "The preference key is not registered.")
        if scope_key.startswith("project:") and not spec.allow_project_scope:
            raise ContextError("INVALID_SOURCE", "This preference key cannot use project scope.")
        if spec.allowed_values:
            if not isinstance(value, str) or value not in spec.allowed_values:
                raise ContextError("INVALID_SOURCE", "The preference value is outside its registered schema.")
        elif spec.allow_free_text:
            if not isinstance(value, str) or not value.strip() or len(value) > spec.max_value_chars:
                raise ContextError("INVALID_SOURCE", "The free-text preference value is invalid.")
        else:
            raise ContextError("INVALID_SOURCE", "The preference key has no usable value schema.")
        return spec


@dataclass(frozen=True)
class MemoryOperation:
    action: str
    kind: str
    source_refs: tuple[str, ...]
    expected_revision: int | None
    uri: str | None = None
    title: str = ""
    content: str = ""
    predicate: str = ""
    value: object | None = None
    preference_key: str | None = None
    scope_key: str | None = None
    subject: str | None = None

@dataclass(frozen=True)
class MemoryUpdateResult:
    change_id: str
    objects: tuple[ContextObject, ...]
    status: str
    retained_sessions: tuple[str, ...] = ()
    retained_backups: tuple[str, ...] = ()
    retained_exports: tuple[str, ...] = ()
    source_cleanup_complete: bool = True
    source_cleanup_reason: str = ""


class MemoryService:
    """Validate memory claims and scopes before committing an atomic update batch."""

    def __init__(
        self,
        store: ContextStore,
        sources: EvidenceReceiptService,
        runtime: RuntimeContext,
        *,
        preference_keys: PreferenceKeyRegistry | None = None,
    ) -> None:
        self.store = store
        self.sources = sources
        self.runtime = runtime
        self.preference_keys = preference_keys or PreferenceKeyRegistry()

    def update(
        self,
        operations: Sequence[MemoryOperation],
        *,
        idempotency_key: str,
    ) -> MemoryUpdateResult:
        if not operations:
            raise ContextError("INVALID_SOURCE", "At least one memory operation is required.")
        if not idempotency_key or len(idempotency_key) > 200:
            raise ContextError("INVALID_SOURCE", "A bounded idempotency key is required.")
        purge_operation = next((op for op in operations if op.action == "purge"), None)
        if purge_operation is not None:
            if len(operations) != 1 or not purge_operation.uri:
                raise ContextError("INVALID_SOURCE", "A purge must be the only operation in its batch and name one URI.")
            parsed = parse_context_uri(purge_operation.uri, self.runtime)
            self._validate_memory_target(purge_operation.kind, parsed)
            self._authorize_object_scope(purge_operation.kind, parsed)
        else:
            self._preflight_operations(operations)
        request_hash = _request_hash(self.runtime, operations)
        prior_purge = None
        if purge_operation is not None:
            purge_uri = parse_context_uri(purge_operation.uri, self.runtime).canonical
            prior_purge = self.store.lookup_purge_status(
                purge_uri,
                idempotency_key=idempotency_key,
                idempotency_payload_hash=request_hash,
                principal_id=self.runtime.user_id,
            )
            if prior_purge is not None and prior_purge.cleanup_complete:
                return _purge_update_result(idempotency_key, prior_purge)
            sources = self._load_sources(
                purge_operation, allow_purged_retry=prior_purge is not None
            )
            if prior_purge is None:
                if not any(receipt.source_type == "user_message" and _PURGE_INTENT.search(receipt.excerpt) for receipt in sources):
                    raise ContextError("INVALID_SOURCE", "Purge requires an explicit user deletion request.")
                self._validate_initial_purge(purge_operation)
        if purge_operation is None:
            prior = self.store.lookup_idempotency(idempotency_key, request_hash)
            if prior is not None:
                committed = tuple(prior)
                return MemoryUpdateResult(idempotency_key, committed, _result_status(committed))
        if purge_operation is not None:
            purge_result = self.store.purge_object(
                parse_context_uri(purge_operation.uri, self.runtime).canonical,
                expected_revision=purge_operation.expected_revision if prior_purge is None else None,
                principal_id=self.runtime.user_id,
                idempotency_key=idempotency_key,
                idempotency_payload_hash=request_hash,
                additional_source_refs=purge_operation.source_refs,
            )
            return _purge_update_result(idempotency_key, purge_result)
        preference_scopes = tuple(dict.fromkeys(
            self._preference_collection_scope(op.scope_key or "user")
            for op in operations if op.action == "put" and op.kind == "user_preference"
        ))
        preference_snapshot = tuple(
            obj for obj in preference_scopes
            for obj in self.store.list_objects(
                uri_filter=lambda uri, scope=obj: uri == scope or uri.startswith(scope + "/")
            )
        )
        self._reject_duplicate_operations(operations)
        prepared: list[tuple[ContextObject, int | None]] = []
        preconditions: dict[str, CollectionPrecondition] = {}
        for operation in operations:
            if operation.action == "retract":
                obj, expected, status = self._prepare_retraction(operation)
                prepared.append((obj, expected))
            elif operation.action == "put" and operation.kind == "user_preference":
                writes, status = self._prepare_preference(operation, preference_snapshot)
                prepared.extend(writes)
                scope = self._preference_collection_scope(operation.scope_key or "user")
                entries = tuple(
                    (obj.uri, obj.revision) for obj in preference_snapshot
                    if obj.uri == scope or obj.uri.startswith(scope + "/")
                )
                preconditions[scope] = CollectionPrecondition(scope, collection_fingerprint(entries))
            elif operation.action == "put":
                obj, expected, status = self._prepare_object(operation)
                prepared.append((obj, expected))
        if len({obj.uri for obj, _ in prepared}) != len(prepared):
            raise ContextConflictError("A memory batch cannot update one URI more than once.")
        committed = tuple(self.store.commit_many(
            prepared, idempotency_key=idempotency_key,
            idempotency_payload_hash=request_hash,
            preconditions=tuple(preconditions.values()),
        ))
        return MemoryUpdateResult(idempotency_key, committed, _result_status(committed))

    def _prepare_preference(
        self,
        operation: MemoryOperation,
        snapshot: Sequence[ContextObject],
    ) -> tuple[list[tuple[ContextObject, int | None]], str]:
        key = operation.preference_key or ""
        scope_key = operation.scope_key or "user"
        self.preference_keys.validate(key, operation.value, scope_key=scope_key)
        project_id = self._authorize_preference_scope(scope_key)
        receipts = self._validate_preference_sources(operation)
        if not isinstance(operation.value, (str, int, float, bool)):
            raise ContextError("INVALID_SOURCE", "Preference values must be simple JSON scalars.")
        excerpts = tuple(receipt.excerpt for receipt in receipts)
        if any(_TEMPORARY_INTENT.search(text) for text in excerpts):
            raise ContextError("INVALID_SOURCE", "Temporary preferences are task-local and cannot update persistent memory.")
        if not any(_PERSISTENT_INTENT.search(text) for text in excerpts):
            raise ContextError("INVALID_SOURCE", "Persistent preferences require an explicit lasting user instruction.")
        value_digest = hashlib.sha256(json.dumps(
            operation.value, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")).hexdigest()[:16]
        object_id = hashlib.sha256(f"{scope_key}:{key}:{value_digest}".encode()).hexdigest()[:32]
        key_segment = quote(f"{key}--{value_digest}", safe="-._~")
        if project_id:
            uri = f"za://projects/{quote(project_id, safe='-._~')}/memories/preferences/{key_segment}"
        else:
            uri = f"za://users/{quote(self.runtime.user_id, safe='-._~')}/memories/preferences/{key_segment}"
        if operation.uri is not None and operation.uri != uri:
            raise ContextError("INVALID_URI", "The preference URI must match its derived identity.")
        current = next((obj for obj in snapshot if obj.uri == uri), None)
        if current is not None:
            if operation.expected_revision != current.revision:
                raise ContextConflictError("The preference object's expected revision is stale.")
            checks = (
                ("kind", "user_preference"),
                ("project_id", project_id),
                ("preference_key", key),
                ("scope_key", scope_key),
            )
            if (
                any(getattr(current, field) != expected for field, expected in checks)
                or (project_id is None and current.owner_id != self.runtime.user_id)
                or not _same_typed_value(current.value, operation.value)
            ):
                raise ContextError("INTEGRITY_ERROR", "The preference object does not match its derived identity.")
        elif operation.expected_revision is not None:
            raise ContextConflictError("The preference object does not have the expected revision.")
        matching = [obj for obj in snapshot if obj.preference_key == key and obj.scope_key == scope_key
                    and (project_id is not None or obj.owner_id == self.runtime.user_id)
                    and obj.project_id == project_id and obj.status in {"active", "conflicted"}
                    and obj.uri != uri]
        replacement = any(_REPLACEMENT_INTENT.search(text) for text in excerpts)
        status = "active"
        operations: list[tuple[ContextObject, int | None]] = []
        conflicting = [obj for obj in matching if not _same_typed_value(obj.value, operation.value)]
        if conflicting and not replacement:
            status = "conflicted"
            operations.extend((_with_status(old, "conflicted", old.source_refs), old.revision) for old in conflicting)
        elif conflicting:
            operations.extend((_with_status(old, "superseded", old.source_refs), old.revision) for old in conflicting)
        content = json.dumps({"key": key, "scope": scope_key, "value": operation.value}, ensure_ascii=False, sort_keys=True)
        obj = ContextObject(
            object_id=object_id, uri=uri, kind="user_preference", owner_id=self.runtime.user_id,
            project_id=project_id, content=content, title=operation.title or key, status=status,
            source_refs=tuple(dict.fromkeys(operation.source_refs)), preference_key=key,
            scope_key=scope_key, value=operation.value, schema_version=2,
        )
        operations.append((obj, current.revision if current is not None else None))
        return operations, status

    def _validate_preference_sources(self, operation: MemoryOperation) -> tuple[EvidenceReceipt, ...]:
        receipts = self._load_sources(operation)
        if not receipts or any(receipt.source_type != "user_message" for receipt in receipts):
            raise ContextError("INVALID_SOURCE", "A preference update must cite the current user's message.")
        if any(
            receipt.project_id is not None
            and receipt.project_id != self.runtime.project_id
            and f"project:{receipt.project_id}:read" not in self.runtime.capabilities
            and "context:admin" not in self.runtime.capabilities
            for receipt in receipts
        ):
            raise ContextError("PERMISSION_DENIED", "A project source receipt is outside the current read grants.")
        if any(receipt.user_id != self.runtime.user_id for receipt in receipts):
            raise ContextError("PERMISSION_DENIED", "A preference source belongs to another user.")
        return receipts

    def _preference_collection_scope(self, scope_key: str) -> str:
        project_id = self._authorize_preference_scope(scope_key)
        if project_id:
            return f"za://projects/{quote(project_id, safe='-._~')}/memories/preferences"
        return f"za://users/{quote(self.runtime.user_id, safe='-._~')}/memories/preferences"

    def _reject_duplicate_operations(self, operations: Sequence[MemoryOperation]) -> None:
        preference_keys: set[tuple[str, str]] = set()
        targets: set[str] = set()
        for operation in operations:
            if operation.action == "put" and operation.kind == "user_preference":
                identity = (operation.scope_key or "user", operation.preference_key or "")
                if identity in preference_keys:
                    raise ContextConflictError("A memory batch cannot update one preference key more than once.")
                preference_keys.add(identity)
            target_uri = operation.uri
            if operation.action == "put" and operation.kind == "user_preference":
                digest = hashlib.sha256(json.dumps(
                    operation.value, sort_keys=True, ensure_ascii=False
                ).encode("utf-8")).hexdigest()[:16]
                key_segment = quote(f"{operation.preference_key or ''}--{digest}", safe="-._~")
                if (operation.scope_key or "user").startswith("project:"):
                    project_id = (operation.scope_key or "").partition(":")[2]
                    target_uri = f"za://projects/{quote(project_id, safe='-._~')}/memories/preferences/{key_segment}"
                else:
                    target_uri = f"za://users/{quote(self.runtime.user_id, safe='-._~')}/memories/preferences/{key_segment}"
            if target_uri and operation.action in {"put", "retract", "purge"}:
                if target_uri in targets:
                    raise ContextConflictError("A memory batch cannot update one URI more than once.")
                targets.add(target_uri)

    def _prepare_object(self, operation: MemoryOperation) -> tuple[ContextObject, int | None, str]:
        if not operation.uri:
            raise ContextError("INVALID_URI", "A logical URI is required for this memory type.")
        parsed = parse_context_uri(operation.uri, self.runtime)
        self._validate_memory_target(operation.kind, parsed)
        self._authorize_object_scope(operation.kind, parsed)
        scope_key = self._target_scope(parsed)
        receipts = self._load_sources(operation)
        supported = False
        canonical = ""
        for receipt in receipts:
            if operation.kind == "project_decision" and receipt.source_type == "user_message":
                if receipt.project_id != parsed.segments[0]:
                    raise ContextError("INVALID_SOURCE", "The decision source does not belong to the target project.")
                supported = (
                    receipt.user_id == self.runtime.user_id
                    and receipt.account_id == self.runtime.account_id
                    and receipt.excerpt == operation.content
                    and bool(_PROJECT_DECISION_INTENT.search(receipt.excerpt))
                )
            elif receipt.source_type == "tool_result" and operation.subject is not None:
                try:
                    self.sources.validate_claim(receipt.receipt_id, self.runtime, kind=operation.kind,
                        predicate=operation.predicate, value=operation.value, subject=operation.subject,
                        scope_key=scope_key)
                except ContextError as exc:
                    if exc.code == "PERMISSION_DENIED":
                        raise
                    raise ContextError("INVALID_SOURCE", "The source does not support this claim.") from exc
                supported = True
                canonical = canonical_claim_content(subject=operation.subject, predicate=operation.predicate,
                    value=operation.value, scope_key=scope_key)
        if operation.subject is not None:
            if not supported or (operation.content and operation.content != canonical):
                raise ContextError("INVALID_SOURCE", "Typed claim content is not canonical.")
            content = canonical
        else:
            content = operation.content
            if not content.strip():
                raise ContextError("INVALID_SOURCE", "Memory content must be non-empty.")
        status = "active" if supported else "candidate_unverified"
        current = self.store.get(parsed.canonical)
        project_id = parsed.segments[0] if parsed.authority == "projects" else None
        if current is not None:
            if current.kind != operation.kind or current.project_id != project_id:
                raise ContextError("INTEGRITY_ERROR", "The memory object identity does not match its target.")
            if parsed.authority == "users" and current.owner_id != self.runtime.user_id:
                raise ContextError("PERMISSION_DENIED", "The memory object belongs to another user.")
            if operation.expected_revision != current.revision:
                raise ContextConflictError("The context object's expected revision is stale.")
        elif operation.expected_revision is not None:
            raise ContextConflictError("The context object does not have the expected revision.")
        if supported:
            content = canonical if operation.kind != "project_decision" else operation.content
        obj = ContextObject(
            object_id=parsed.segments[-1],
            uri=parsed.canonical,
            kind=operation.kind,
            owner_id=self.runtime.user_id,
            project_id=project_id,
            content=content,
            title=operation.predicate if supported and operation.kind in {"environment_fact", "experience"} else operation.title,
            status=status,
            source_refs=tuple(dict.fromkeys(operation.source_refs)),
            schema_version=2,
            scope_key=scope_key if operation.kind in {"environment_fact", "experience"} else None,
            value=operation.value if supported and operation.kind in {"environment_fact", "experience"} else None,
        )
        return obj, current.revision if current is not None else None, status

    def _validate_memory_target(self, kind: str, parsed) -> None:
        collections = {
            "user_preference": "preferences",
            "environment_fact": "facts",
            "project_decision": "decisions",
            "experience": "experiences",
        }
        collection = collections.get(kind)
        if (
            collection is None or len(parsed.segments) != 4
            or parsed.segments[1] != "memories"
            or parsed.segments[2] != collection
            or not parsed.segments[3]
        ):
            raise ContextError("INVALID_URI", "The memory target is not a supported typed memory path.")
        if kind == "project_decision" and parsed.authority != "projects":
            raise ContextError("INVALID_URI", "Project decisions require a project memory path.")
        if parsed.authority not in {"users", "projects"}:
            raise ContextError("INVALID_URI", "Memory targets require user or project ownership.")

    def _target_scope(self, parsed) -> str:
        return f"project:{parsed.segments[0]}" if parsed.authority == "projects" else f"user:{parsed.segments[0]}"

    def _validate_initial_purge(self, operation: MemoryOperation) -> None:
        parsed = parse_context_uri(operation.uri or "", self.runtime)
        current = self.store.get(parsed.canonical)
        if current is None:
            raise ContextError("NOT_FOUND", "The memory object was not found.")
        if current.kind != operation.kind or (parsed.authority == "users" and current.owner_id != self.runtime.user_id):
            raise ContextError("PERMISSION_DENIED", "The memory object is not owned by this writer.")
        if operation.expected_revision is None or operation.expected_revision != current.revision:
            raise ContextConflictError("The memory object's expected revision is stale.")

    def _prepare_retraction(self, operation: MemoryOperation) -> tuple[ContextObject, int, str]:
        if not operation.uri:
            raise ContextError("INVALID_URI", "A logical URI is required for retraction.")
        parsed = parse_context_uri(operation.uri, self.runtime)
        self._validate_memory_target(operation.kind, parsed)
        self._authorize_object_scope(operation.kind, parsed)
        receipts = self._load_sources(operation)
        if not any(receipt.source_type == "user_message" and _RETRACTION_INTENT.search(receipt.excerpt) for receipt in receipts):
            raise ContextError("INVALID_SOURCE", "Retraction requires an explicit user retraction request.")
        current = self.store.get(parsed.canonical)
        if current is None:
            raise ContextError("NOT_FOUND", "The memory object was not found.")
        if current.kind != operation.kind or (parsed.authority == "users" and current.owner_id != self.runtime.user_id):
            raise ContextError("PERMISSION_DENIED", "The memory object is not owned by this writer.")
        if operation.expected_revision != current.revision:
            raise ContextConflictError("The memory object's expected revision is stale.")
        return _with_status(current, "retracted", tuple(dict.fromkeys((*current.source_refs, *operation.source_refs)))), current.revision, "retracted"

    def _load_sources(
        self,
        operation: MemoryOperation,
        *,
        allow_purged_retry: bool = False,
    ) -> tuple[EvidenceReceipt, ...]:
        if not operation.source_refs:
            raise ContextError("INVALID_SOURCE", "Memory operations require at least one source receipt.")
        receipts: list[EvidenceReceipt] = []
        for receipt_id in operation.source_refs:
            receipt = self.sources.store.get_evidence_receipt(receipt_id)
            if receipt is not None and receipt.revoked and allow_purged_retry:
                if (
                    receipt.account_id != self.runtime.account_id
                    or receipt.user_id != self.runtime.user_id
                    or receipt.last_event_type != "redacted"
                    or receipt.last_event_reason_code != "source_purged"
                ):
                    raise ContextError("INVALID_SOURCE", "A purged source does not match this authorized retry.")
                receipts.append(receipt)
                continue
            if receipt is None or receipt.revoked:
                raise ContextError("INVALID_SOURCE", "A cited source receipt is missing or revoked.")
            if receipt.project_id is not None and (
                receipt.project_id != self.runtime.project_id
                and f"project:{receipt.project_id}:read" not in self.runtime.capabilities
                and "context:admin" not in self.runtime.capabilities
            ):
                raise ContextError("PERMISSION_DENIED", "A project source receipt is outside the current read grants.")
            if receipt.account_id != self.runtime.account_id or receipt.user_id != self.runtime.user_id:
                raise ContextError("PERMISSION_DENIED", "A source receipt belongs to another principal.")
            receipts.append(receipt)
        return tuple(receipts)
    def _preflight_operations(self, operations: Sequence[MemoryOperation]) -> None:
        """Recheck current authority and cited source provenance before writes/replays."""

        for operation in operations:
            if operation.action not in {"put", "retract", "purge"}:
                raise ContextError("INVALID_SOURCE", "The memory operation is unsupported.")
            if operation.kind == "user_preference":
                self._authorize_preference_scope(operation.scope_key or "user")
                if operation.action == "put":
                    self.preference_keys.validate(
                        operation.preference_key or "", operation.value,
                        scope_key=operation.scope_key or "user",
                    )
                    receipts = self._validate_preference_sources(operation)
                    excerpts = tuple(receipt.excerpt for receipt in receipts)
                    if any(_TEMPORARY_INTENT.search(text) for text in excerpts):
                        raise ContextError("INVALID_SOURCE", "Temporary preferences are task-local and cannot update persistent memory.")
                    if not any(_PERSISTENT_INTENT.search(text) for text in excerpts):
                        raise ContextError("INVALID_SOURCE", "Persistent preferences require an explicit lasting user instruction.")
                else:
                    self._load_sources(operation)
            else:
                if not operation.uri:
                    raise ContextError("INVALID_URI", "A logical URI is required for this operation.")
                parsed = parse_context_uri(operation.uri, self.runtime)
                self._validate_memory_target(operation.kind, parsed)
                self._authorize_object_scope(operation.kind, parsed)
                self._load_sources(operation)
    def _authorize_preference_scope(self, scope_key: str) -> str | None:
        if scope_key == "user":
            if self._is_group_context() and "context:personal_write" not in self.runtime.capabilities:
                raise ContextError("PERMISSION_DENIED", "Group conversations cannot write personal memory by default.")
            return None
        if not scope_key.startswith("project:"):
            raise ContextError("INVALID_SOURCE", "Preference scope must be user or project:<id>.")
        project_id = scope_key.partition(":")[2]
        if project_id != self.runtime.project_id or (
            f"project:{project_id}:write" not in self.runtime.capabilities
            and "context:admin" not in self.runtime.capabilities
        ):
            raise ContextError("PERMISSION_DENIED", "The current identity cannot write this project scope.")
        return project_id

    def _authorize_object_scope(self, kind: str, parsed) -> None:
        if parsed.authority == "users":
            if parsed.segments[0] != self.runtime.user_id:
                raise ContextError("PERMISSION_DENIED", "The URI belongs to another user.")
            if self._is_group_context() and "context:personal_write" not in self.runtime.capabilities:
                raise ContextError("PERMISSION_DENIED", "Group conversations cannot write personal memory by default.")
            return
        if parsed.authority == "projects":
            project_id = parsed.segments[0]
            if project_id != self.runtime.project_id or (
                f"project:{project_id}:write" not in self.runtime.capabilities
                and "context:admin" not in self.runtime.capabilities
            ):
                raise ContextError("PERMISSION_DENIED", "The current identity cannot write this project scope.")
            return
        raise ContextError("PERMISSION_DENIED", "Memory updates are limited to user and project objects.")

    def _is_group_context(self) -> bool:
        scope_parts = (self.runtime.channel_scope or "").casefold().split(":")
        return "group" in scope_parts or "session" in scope_parts


def _with_status(obj: ContextObject, status: str, source_refs: tuple[str, ...]) -> ContextObject:
    return ContextObject(
        object_id=obj.object_id,
        uri=obj.uri,
        kind=obj.kind,
        owner_id=obj.owner_id,
        project_id=obj.project_id,
        content=obj.content,
        revision=obj.revision,
        title=obj.title,
        tags=obj.tags,
        status=status,
        source_refs=source_refs,
        preference_key=obj.preference_key,
        scope_key=obj.scope_key,
        value=obj.value,
        schema_version=obj.schema_version,
        created_at=obj.created_at,
        updated_at=obj.updated_at,
    )


def _same_typed_value(first: object, second: object) -> bool:
    return type(first) is type(second) and first == second


def _request_hash(
    runtime: RuntimeContext,
    operations: Sequence[MemoryOperation],
) -> str:
    try:
        payload = {
            "account_id": runtime.account_id,
            "user_id": runtime.user_id,
            "session_id": runtime.session_id,
            "project_id": runtime.project_id,
            "operations": [
                {
                    "action": operation.action,
                    "kind": operation.kind,
                    "source_refs": operation.source_refs,
                    "expected_revision": operation.expected_revision,
                    "uri": operation.uri,
                    "title": operation.title,
                    "content": operation.content,
                    "predicate": operation.predicate,
                    "value": operation.value,
                    "preference_key": operation.preference_key,
                    "scope_key": operation.scope_key,
                    "subject": operation.subject,
                }
                for operation in operations
            ],
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ContextError("INVALID_SOURCE", "Memory operations must contain JSON values.") from exc
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _result_status(objects: Sequence[ContextObject]) -> str:
    if any(obj.status == "candidate_unverified" for obj in objects):
        return "candidate_unverified"
    if any(obj.status == "conflicted" for obj in objects):
        return "conflicted"
    if objects and all(obj.status == "retracted" for obj in objects):
        return "retracted"
    return "active"
def _purge_update_result(change_id: str, purge: PurgeResult) -> MemoryUpdateResult:
    return MemoryUpdateResult(
        change_id=change_id,
        objects=(),
        status="purged" if purge.cleanup_complete else "purge_pending",
        retained_sessions=purge.retained_sessions,
        retained_backups=purge.retained_backups,
        retained_exports=purge.retained_exports,
        source_cleanup_complete=purge.source_cleanup_complete,
        source_cleanup_reason=purge.source_cleanup_reason,
    )
