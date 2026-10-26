"""Identity and object contracts shared by context providers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Callable
from urllib.parse import unquote
from typing import Protocol, Sequence


@dataclass(frozen=True)
class RuntimeContext:
    """Trusted identity and grants supplied by an application entry point."""

    account_id: str
    user_id: str
    session_id: str
    project_id: str | None = None
    channel_scope: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for name in ("account_id", "user_id", "session_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("user_id", "session_id", "project_id"):
            value = getattr(self, name)
            if value is None:
                continue
            if (
                not value.strip()
                or value in {".", ".."}
                or "/" in value
                or "\\" in value
                or "\x00" in value
                or any(ord(char) < 0x20 for char in value)
            ):
                raise ValueError(f"{name} must be a safe logical URI segment")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


@dataclass(frozen=True)
class ContextObject:
    """One versioned logical context object."""

    object_id: str
    uri: str
    kind: str
    owner_id: str
    content: str
    revision: int = 1
    project_id: str | None = None
    title: str = ""
    tags: tuple[str, ...] = ()
    status: str = "active"
    source_refs: tuple[str, ...] = ()
    preference_key: str | None = None
    scope_key: str | None = None
    value: object | None = None
    schema_version: int = 1
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CollectionPrecondition:
    """Expected fingerprint of every current object within one canonical URI collection."""

    scope: str
    fingerprint: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, str)
            or not self.scope.startswith("za://")
            or self.scope.endswith("/")
        ):
            raise ValueError("collection scope must be a canonical collection URI")
        if (
            not isinstance(self.fingerprint, str)
            or len(self.fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in self.fingerprint)
        ):
            raise ValueError("collection fingerprint must be a SHA-256 digest")


def collection_fingerprint(entries: Sequence[tuple[str, int]]) -> str:
    """Hash the sorted URI/revision preimage for a collection compare-and-swap."""

    payload = sorted((uri, revision) for uri, revision in entries)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def context_access_scope_key(obj: ContextObject, runtime: RuntimeContext) -> str:
    """Return the non-secret logical scope used to partition access counters."""
    parts = obj.uri.split("/")
    if len(parts) > 3 and parts[2] == "users":
        return f"user:{unquote(parts[3])}"
    if len(parts) > 3 and parts[2] == "projects":
        return f"project:{obj.project_id or unquote(parts[3])}"
    if len(parts) > 3 and parts[2] == "sessions":
        return f"session:{unquote(parts[3])}"
    if len(parts) > 4 and parts[2:4] == ["skills", "builtin"]:
        return "skill:builtin"
    if len(parts) > 4 and parts[2:4] == ["skills", "users"]:
        return f"skill:user:{unquote(parts[4])}"
    return "unknown"


@dataclass(frozen=True)
class EvidenceReceipt:
    """Durable, minimal reference to an observed message, tool result, or resource."""

    receipt_id: str
    source_type: str
    account_id: str
    user_id: str
    session_id: str
    task_id: str
    event_sequence: int
    source_id: str
    tool_name: str = ""
    tool_contract_version: str = ""
    outcome_status: str = "success"
    result_fields: dict[str, object] = field(default_factory=dict)
    project_id: str | None = None
    excerpt: str = ""
    source_revision: str = ""
    source_content_sha256: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    revoked: bool = False
    last_event_type: str = ""
    last_event_reason_code: str = ""


@dataclass(frozen=True)
class ContextAccessStat:
    """One principal- and logical-scope-partitioned access counter."""

    uri: str
    revision: int
    scope_key: str
    operation: str
    count: int
    last_access_at: str


@dataclass(frozen=True)
class PurgeResult:
    """Physical cleanup state and durable copies that still retain source material."""

    cleanup_complete: bool
    retained_sessions: tuple[str, ...] = ()
    retained_backups: tuple[str, ...] = ()
    retained_exports: tuple[str, ...] = ()
    source_cleanup_complete: bool = True
    source_cleanup_reason: str = ""


@dataclass(frozen=True)
class DerivedSummary:
    uri: str
    view: str
    input_revision: int
    generator_version: str
    content: str
    content_sha256: str
    stale: bool
    created_at: str


class ContextStore(Protocol):
    """Provider contract; implementations must preserve object revisions."""

    def get(self, uri: str, revision: int | None = None) -> ContextObject | None:
        """Return the current or requested immutable revision."""

    def list_objects(
        self,
        *,
        uri_filter: Callable[[str], bool] | None = None,
    ) -> Sequence[ContextObject]: ...
    def record_access(
        self,
        runtime: RuntimeContext,
        objects: Sequence[ContextObject],
        *,
        operation: str,
    ) -> None: ...
    def list_access_stats(self, runtime: RuntimeContext) -> Sequence[ContextAccessStat]: ...

    def commit(
        self,
        obj: ContextObject,
        *,
        expected_revision: int | None,
        idempotency_key: str | None = None,
        idempotency_payload_hash: str | None = None,
    ) -> ContextObject:
        """Conditionally publish an object and return its committed revision."""

    def commit_many(
        self,
        operations: Sequence[tuple[ContextObject, int | None]],
        *,
        idempotency_key: str | None = None,
        idempotency_payload_hash: str | None = None,
        preconditions: Sequence[CollectionPrecondition] = (),
    ) -> Sequence[ContextObject]:
        """Conditionally publish all objects as one transaction."""

    def lookup_idempotency(
        self,
        idempotency_key: str,
        payload_sha256: str,
    ) -> Sequence[ContextObject] | None:
        """Return a prior result when the same idempotency request was committed."""

    def lookup_purge_status(
        self,
        uri: str,
        *,
        idempotency_key: str,
        idempotency_payload_hash: str,
        principal_id: str | None = None,
    ) -> PurgeResult | None:
        """Return completed/pending status for an earlier purge request."""

    def purge_object(
        self,
        uri: str,
        *,
        expected_revision: int | None,
        principal_id: str,
        idempotency_key: str,
        idempotency_payload_hash: str,
        additional_source_refs: Sequence[str] = (),
    ) -> PurgeResult:
        """Remove an object and its no-longer-referenced source payloads."""

    def save_derived_summary(
        self,
        uri: str,
        *,
        view: str,
        input_revision: int,
        generator_version: str,
        content: str,
    ) -> DerivedSummary:
        """Save a derived view bound to one exact source revision."""

    def get_derived_summary(
        self,
        uri: str,
        *,
        view: str,
        input_revision: int | None = None,
        generator_version: str | None = None,
    ) -> DerivedSummary | None:
        """Read a summary, optionally constrained to an exact input revision."""
