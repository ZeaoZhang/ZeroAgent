"""Small in-memory ContextStore used by contract tests and local composition."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from threading import RLock
from typing import Sequence
from collections.abc import Callable

from zero_agent.context.errors import ContextConflictError
from zero_agent.context.types import (
    RuntimeContext,
    CollectionPrecondition,
    ContextObject,
    ContextAccessStat,
    collection_fingerprint,
    context_access_scope_key,
)


class InMemoryContextStore:
    """Thread-safe store implementing conditional object revision updates."""

    def __init__(self) -> None:
        self._objects: dict[str, ContextObject] = {}
        self._history: dict[tuple[str, int], ContextObject] = {}
        self._idempotency: dict[str, tuple[str, tuple[ContextObject, ...]]] = {}
        self._access_stats: dict[tuple[str, str, str, str, int, str], tuple[int, str]] = {}
        self._lock = RLock()

    def get(self, uri: str, revision: int | None = None) -> ContextObject | None:
        with self._lock:
            if revision is None:
                return self._objects.get(uri)
            return self._history.get((uri, revision))

    def list_objects(
        self,
        *,
        uri_filter: Callable[[str], bool] | None = None,
    ) -> Sequence[ContextObject]:
        with self._lock:
            if uri_filter is None:
                return tuple(self._objects.values())
            return tuple(
                obj for uri, obj in self._objects.items()
                if obj.status != "sensitive_quarantined" and uri_filter(uri)
            )

    def record_access(
        self,
        runtime: RuntimeContext,
        objects: Sequence[ContextObject],
        *,
        operation: str,
    ) -> None:
        if operation not in {"read", "list", "search"}:
            raise ValueError("unsupported context access operation")
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for obj in objects:
                scope_key = context_access_scope_key(obj, runtime)
                key = (runtime.account_id, runtime.user_id, scope_key, obj.uri, obj.revision, operation)
                count, _ = self._access_stats.get(key, (0, now))
                self._access_stats[key] = (count + 1, now)

    def list_access_stats(self, runtime: RuntimeContext) -> tuple[ContextAccessStat, ...]:
        with self._lock:
            return tuple(
                ContextAccessStat(
                    uri=uri, revision=revision, scope_key=scope_key,
                    operation=operation, count=count, last_access_at=last_access_at,
                )
                for (account_id, user_id, scope_key, uri, revision, operation),
                (count, last_access_at) in self._access_stats.items()
                if account_id == runtime.account_id and user_id == runtime.user_id
            )


    def commit(
        self,
        obj: ContextObject,
        *,
        expected_revision: int | None,
        idempotency_key: str | None = None,
        idempotency_payload_hash: str | None = None,
    ) -> ContextObject:
        return self.commit_many(
            ((obj, expected_revision),),
            idempotency_key=idempotency_key,
            idempotency_payload_hash=idempotency_payload_hash,
        )[0]

    def commit_many(
        self,
        operations: Sequence[tuple[ContextObject, int | None]],
        *,
        idempotency_key: str | None = None,
        idempotency_payload_hash: str | None = None,
        preconditions: Sequence[CollectionPrecondition] = (),
    ) -> Sequence[ContextObject]:
        if not operations:
            raise ValueError("commit_many requires at least one operation")
        if len({obj.uri for obj, _ in operations}) != len(operations):
            raise ValueError("commit_many cannot contain duplicate URIs")
        payload_hash = idempotency_payload_hash or _payload_hash(operations)
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._idempotency:
                prior_hash, prior_result = self._idempotency[idempotency_key]
                if prior_hash != payload_hash:
                    raise ContextConflictError("Idempotency key payload mismatch.")
                return prior_result
            for obj, expected_revision in operations:
                current = self._objects.get(obj.uri)
                actual_revision = current.revision if current is not None else None
                if actual_revision != expected_revision:
                    raise ContextConflictError()
            if len({item.scope for item in preconditions}) != len(preconditions):
                raise ValueError("commit_many cannot repeat collection scopes")
            for condition in preconditions:
                actual = collection_fingerprint(
                    tuple(
                        (uri, obj.revision)
                        for uri, obj in self._objects.items()
                        if uri == condition.scope or uri.startswith(condition.scope + "/")
                    )
                )
                if actual != condition.fingerprint:
                    raise ContextConflictError("Collection changed since its preimage was read.")
            committed = tuple(
                replace(
                    obj,
                    revision=1 if expected is None else expected + 1,
                )
                for obj, expected in operations
            )
            for obj in committed:
                self._objects[obj.uri] = obj
                self._history[(obj.uri, obj.revision)] = obj
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = (payload_hash, committed)
            return committed
    def lookup_idempotency(
        self,
        idempotency_key: str,
        payload_sha256: str,
    ) -> Sequence[ContextObject] | None:
        with self._lock:
            result = self._idempotency.get(idempotency_key)
            if result is None:
                return None
            stored_hash, objects = result
            if stored_hash != payload_sha256:
                raise ContextConflictError("Idempotency key payload mismatch.")
            return objects


def _payload_hash(operations: Sequence[tuple[ContextObject, int | None]]) -> str:
    payload = [
        (
            expected,
            obj.object_id,
            obj.uri,
            obj.kind,
            obj.owner_id,
            obj.project_id,
            obj.content_sha256,
            obj.title,
            obj.tags,
            obj.status,
            obj.source_refs,
            obj.preference_key,
            obj.scope_key,
            obj.value,
            obj.schema_version,
        )
        for obj, expected in operations
    ]
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
