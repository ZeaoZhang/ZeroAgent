"""Authorization-filtered logical context reads and lexical search."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from zero_agent.context.errors import ContextError
from zero_agent.context.retrieval import match_text
from zero_agent.context.sources import EvidenceReceiptService
from zero_agent.context.types import ContextAccessStat, ContextObject, ContextStore, RuntimeContext
from zero_agent.context.uri import ParsedContextURI, parse_context_uri, uri_is_within


@dataclass(frozen=True)
class ContextSearchResult:
    uri: str
    title: str
    excerpt: str
    revision: int
    content_sha256: str
    kind: str
    status: str
    score: float
    matched_terms: tuple[str, ...]
    positions: tuple[tuple[int, int], ...]


class ContextService:
    """Resolve logical URIs and filter all reads through the runtime grants."""
    def __init__(
        self,
        store: ContextStore,
        runtime: RuntimeContext,
        *,
        sources: EvidenceReceiptService | None = None,
        receipt_store: Any | None = None,
        builtin_objects: tuple[ContextObject, ...] = (),
    ) -> None:
        """Bind one object store to a trusted runtime and its provenance sources.

        Args:
            store: Revisioned context-object provider.
            runtime: Trusted principal, project, session, and read grants.
            sources: Receipt validator for source-backed memory objects.
            receipt_store: Persistent receipt reader for revocation checks.
            builtin_objects: Version-pinned built-in context objects.
        """
        self.store = store
        self.runtime = runtime
        self.sources = sources
        self.receipt_store = receipt_store
        self.builtin_objects = {obj.uri: obj for obj in builtin_objects}

    def list_task_receipts(
        self,
        *,
        task_id: str,
        source_type: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List authorized source receipts for a task.

        Args:
            task_id: Stable ID of the current task.
            source_type: Optional receipt type filter.
            limit: Maximum receipts returned on this page.
            cursor: Snapshot-bound continuation cursor.

        Returns:
            Public receipt records and an optional next cursor.
        """

        return self.sources.list_current(
            self.runtime,
            task_id=task_id,
            source_type=source_type,
            limit=limit,
            cursor=cursor,
        )

    def read_receipt(self, receipt_id: str) -> Any:
        """Read one source receipt after current-runtime authorization.

        Args:
            receipt_id: Exact persistent source-receipt identity.

        Returns:
            The authorized evidence receipt.

        Raises:
            ContextError: If the receipt is missing, revoked, or outside current grants.
        """
        return self.sources.read(receipt_id, self.runtime)

    def read(self, uri: str, *, revision: int | None = None) -> ContextObject:
        """Read an authorized logical context object at the requested revision.

        Args:
            uri: Canonical or ``me``-relative logical context URI.
            revision: Optional immutable revision to read.

        Returns:
            The current or requested context object.

        Raises:
            ContextError: If access is denied, the URI is invalid, or the object is unavailable.
        """
        parsed = parse_context_uri(uri, self.runtime)
        self._authorize(parsed)
        obj = self.builtin_objects.get(parsed.canonical)
        if obj is None:
            obj = self.store.get(parsed.canonical, revision)
        if obj is None:
            raise ContextError("NOT_FOUND", "The requested context object was not found.")
        if revision is not None and obj.revision != revision:
            raise ContextError("NOT_FOUND", "The requested context revision was not found.")
        self._validate_object_scope(obj, parsed)
        if obj.status == "sensitive_quarantined":
            raise ContextError("PERMISSION_DENIED", "This legacy object is held outside model-readable context.")
        obj = self._effective_source_status(obj)
        self._record_access((obj,), operation="read")
        return obj

    def read_view(
        self,
        uri: str,
        *,
        view: str,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Read an authorized abstract, overview, or detail view of an object.

        Args:
            uri: Logical URI of the object.
            view: Requested reading granularity.
            revision: Optional immutable source revision.

        Returns:
            View content and its source revision/integrity metadata.

        Raises:
            ContextError: If the URI, view, authorization, or revision is invalid.
        """
        if view not in {"abstract", "overview", "detail"}:
            raise ContextError("INVALID_URI", "The requested context view is unsupported.")
        obj = self.read(uri, revision=revision)
        if view == "detail":
            content = obj.content
            summary_status = "not_applicable"
        elif view == "abstract":
            content = obj.title or " ".join(obj.tags)
            summary_status = "author_provided"
        else:
            getter = getattr(self.store, "get_derived_summary", None)
            summary = getter(obj.uri, view="overview", input_revision=obj.revision) if callable(getter) else None
            if summary is None:
                content = obj.title or ""
                summary_status = "unavailable"
            else:
                content = summary.content
                summary_status = "stale" if summary.stale else "current"
        return {
            "uri": obj.uri,
            "revision": obj.revision,
            "content_sha256": obj.content_sha256,
            "kind": obj.kind,
            "status": obj.status,
            "view": view,
            "summary_status": summary_status,
            "content": content,
        }

    def list(self, uri: str, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """List authorized objects under a logical collection URI.

        Args:
            uri: Collection URI to enumerate.
            cursor: Snapshot-bound continuation cursor.
            limit: Maximum number of objects in the page.

        Returns:
            Authorized objects and an optional next cursor.

        Raises:
            ContextError: If the collection is unauthorized, invalid, or changed between pages.
        """
        prefix = parse_context_uri(uri, self.runtime)
        if limit < 1 or limit > 500:
            raise ContextError("INVALID_URI", "limit must be between 1 and 500.")
        self._authorize(prefix)
        matches = sorted(
            (
                self._effective_source_status(obj)
                for obj in self._all_objects(
                    lambda object_uri: self._uri_visible_in_collection(object_uri, prefix)
                )
                if self._visible_in_collection(obj, prefix)
                and obj.status != "sensitive_quarantined"
            ),
            key=lambda obj: obj.uri,
        )
        snapshot = _snapshot({
            "scope": prefix.canonical,
            "objects": [(obj.uri, obj.revision, obj.status) for obj in matches],
        })
        start = _parse_cursor(cursor, snapshot)
        page = matches[start : start + limit]
        next_offset = start + len(page)
        self._record_access(page, operation="list")
        return {
            "items": page,
            "next_cursor": _make_cursor(snapshot, next_offset) if next_offset < len(matches) else None,
        }

    def list_access_stats(self) -> tuple[ContextAccessStat, ...]:
        """List access counters visible to the current runtime.

        Returns:
            Per-object counters filtered using current authorization grants.
        """
        stats = self.store.list_access_stats(self.runtime)
        if not stats:
            return ()
        visible: list[ContextAccessStat] = []
        for stat in stats:
            try:
                parsed = parse_context_uri(stat.uri, self.runtime)
                if not self._can_read(parsed):
                    continue
                obj = self.store.get(parsed.canonical)
                if obj is None:
                    obj = self.builtin_objects.get(parsed.canonical)
                if obj is None:
                    continue
                if obj.revision != stat.revision or obj.status in {"sensitive_quarantined", "retracted"}:
                    continue
                self._validate_object_scope(obj, parsed)
                visible.append(stat)
            except ContextError:
                continue
        return tuple(visible)


    def search(
        self,
        query: str,
        *,
        scope: str | None = None,
        kinds: tuple[str, ...] = (),
        limit: int = 10,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search active objects in the current user/project or an explicit scope.

        Args:
            query: Lexical search text.
            scope: Optional authorized collection; omission stays in the current user and project.
            kinds: Optional object-kind filter.
            limit: Maximum number of results in the page.
            cursor: Query-, scope-, and snapshot-bound continuation cursor.

        Returns:
            Ranked authorized results and an optional next cursor.

        Raises:
            ContextError: If the query, scope, cursor, or authorization is invalid.
        """
        if not isinstance(query, str) or not query.strip():
            raise ContextError("INVALID_URI", "A non-empty search query is required.")
        if limit < 1 or limit > 100:
            raise ContextError("INVALID_URI", "limit must be between 1 and 100.")
        if scope is not None:
            prefix = parse_context_uri(scope, self.runtime)
            self._authorize(prefix)
            search_prefixes = (prefix,)
        else:
            user_scope = parse_context_uri("za://users/me/memories", self.runtime)
            project_scopes = (
                (parse_context_uri(f"za://projects/{self.runtime.project_id}", self.runtime),)
                if self.runtime.project_id
                else ()
            )
            search_prefixes = (user_scope, *project_scopes)
        uri_filter = lambda object_uri: any(
            self._uri_visible_in_collection(object_uri, prefix)
            for prefix in search_prefixes
        )
        returned_objects: list[ContextObject] = []
        matches: list[tuple[float, str, str, ContextSearchResult]] = []
        for obj in self._all_objects(uri_filter):
            parsed = parse_context_uri(obj.uri, self.runtime)
            self._validate_object_scope(obj, parsed)
            if obj.status == "sensitive_quarantined":
                continue
            if kinds and obj.kind not in kinds:
                continue
            obj = self._effective_source_status(obj)
            if obj.status != "active":
                continue
            text = f"{obj.title}\n{obj.content}\n{' '.join(obj.tags)}"
            match = match_text(query, text)
            if match is None:
                continue
            start_pos = min(start for start, _ in match.positions)
            end_pos = max(end for _, end in match.positions)
            left = max(0, start_pos - 100)
            right = min(len(text), end_pos + 180)
            result = ContextSearchResult(
                uri=obj.uri,
                title=obj.title,
                excerpt=text[left:right],
                revision=obj.revision,
                content_sha256=obj.content_sha256,
                kind=obj.kind,
                status=obj.status,
                score=match.score,
                matched_terms=match.terms,
                positions=tuple(
                    (start - left, end - left)
                    for start, end in match.positions
                ),
            )
            matches.append((match.score, obj.uri, obj.object_id, result))
            returned_objects.append(obj)
        matches.sort(key=lambda item: (-item[0], item[1]))
        deduplicated: list[tuple[float, str, str, ContextSearchResult]] = []
        seen_versions: set[tuple[str, int]] = set()
        for match in matches:
            identity = (match[1], match[3].revision)
            if identity in seen_versions:
                continue
            seen_versions.add(identity)
            deduplicated.append(match)
        results = [item[3] for item in deduplicated]
        snapshot = _snapshot({
            "query": query.casefold().strip(),
            "scopes": tuple(prefix.canonical for prefix in search_prefixes),
            "kinds": tuple(kinds),
            "objects": [(item.uri, item.revision, item.score) for item in results],
        })
        start = _parse_cursor(cursor, snapshot)
        page = results[start : start + limit]
        next_offset = start + len(page)
        page_identities = {(item.uri, item.revision) for item in page}
        page_objects = {
            (obj.uri, obj.revision): obj
            for obj in returned_objects
            if (obj.uri, obj.revision) in page_identities
        }
        self._record_access(tuple(page_objects.values()), operation="search")
        return {
            "results": page,
            "next_cursor": _make_cursor(snapshot, next_offset) if next_offset < len(results) else None,
        }

    def _authorize(self, parsed: ParsedContextURI) -> None:
        if not self._can_read(parsed):
            raise ContextError("PERMISSION_DENIED", "The context URI is outside the current grants.")

    def _visible_in_collection(
        self,
        obj: ContextObject,
        prefix: ParsedContextURI,
    ) -> bool:
        parsed = parse_context_uri(obj.uri, self.runtime)
        if not uri_is_within(parsed, prefix) or not self._can_read(parsed):
            return False
        self._validate_object_scope(obj, parsed)
        return obj.status != "sensitive_quarantined"

    def _uri_visible_in_collection(self, uri: str, prefix: ParsedContextURI) -> bool:
        parsed = parse_context_uri(uri, self.runtime)
        return uri_is_within(parsed, prefix) and self._can_read(parsed)

    def _record_access(
        self,
        objects: Sequence[ContextObject],
        *,
        operation: str,
    ) -> None:
        recorder = getattr(self.store, "record_access", None)
        if not callable(recorder) or not objects:
            return
        unique = {(obj.uri, obj.revision): obj for obj in objects}
        try:
            recorder(self.runtime, tuple(unique.values()), operation=operation)
        except (OSError, sqlite3.Error):
            import logging

            logging.getLogger(__name__).warning("Context access statistics unavailable.")


    def _effective_source_status(self, obj: ContextObject) -> ContextObject:
        if obj.status != "active" or obj.uri in self.builtin_objects:
            return obj
        from dataclasses import replace

        if obj.kind == "user_preference":
            for receipt_id in obj.source_refs:
                receipt = self.receipt_store.get_evidence_receipt(receipt_id) if self.receipt_store else None
                if receipt is None or receipt.revoked:
                    return replace(obj, status="candidate_unverified")
            return obj
        if obj.kind in {"environment_fact", "experience", "project_decision"}:
            if obj.schema_version < 2 or self.sources is None:
                return replace(obj, status="candidate_unverified")
            if not self.sources.validate_stored_claim(obj, self.runtime):
                return replace(obj, status="candidate_unverified")
        elif self.receipt_store is not None:
            if not obj.source_refs:
                return replace(obj, status="candidate_unverified")
            for receipt_id in obj.source_refs:
                receipt = self.receipt_store.get_evidence_receipt(receipt_id)
                if receipt is None or receipt.revoked:
                    return replace(obj, status="candidate_unverified")
        return obj

    def _all_objects(
        self,
        uri_filter: Callable[[str], bool] | None = None,
    ) -> tuple[ContextObject, ...]:
        def allowed(uri: str) -> bool:
            if uri_filter is not None and not uri_filter(uri):
                return False
            try:
                parsed = parse_context_uri(uri, self.runtime)
            except ContextError:
                return False
            return self._can_read(parsed)

        merged = {obj.uri: obj for obj in self.store.list_objects(uri_filter=allowed)}
        merged.update({uri: obj for uri, obj in self.builtin_objects.items() if allowed(uri)})
        return tuple(merged.values())

    @staticmethod
    def _validate_object_scope(obj: ContextObject, parsed: ParsedContextURI) -> None:
        expected_owner: str | None = None
        if parsed.authority == "users":
            expected_owner = parsed.segments[0]
        elif parsed.authority == "skills" and parsed.segments[0] == "users":
            expected_owner = parsed.segments[1]
        if expected_owner is not None and obj.owner_id != expected_owner:
            raise ContextError("INTEGRITY_ERROR", "The context object owner does not match its URI.")
        if parsed.authority == "projects" and obj.project_id != parsed.segments[0]:
            raise ContextError("INTEGRITY_ERROR", "The context object project does not match its URI.")

    def _can_read(self, parsed: ParsedContextURI) -> bool:
        parts = parsed.segments
        caps = self.runtime.capabilities
        if "context:admin" in caps:
            return True
        if parsed.authority == "users":
            return parts[0] == self.runtime.user_id
        if parsed.authority == "projects":
            project_id = parts[0]
            return project_id == self.runtime.project_id or f"project:{project_id}:read" in caps
        if parsed.authority == "sessions":
            session_id = parts[0]
            return session_id == self.runtime.session_id or f"session:{session_id}:read" in caps
        if parsed.authority == "skills":
            return parts[0] == "builtin" or parts[1] == self.runtime.user_id
        return False


def _snapshot(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_cursor(snapshot: str, offset: int) -> str:
    raw = json.dumps((snapshot, offset), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_cursor(cursor: str | None, snapshot: str) -> int:
    if cursor is None:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        bound_snapshot, offset = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (TypeError, ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ContextError("INVALID_URI", "The pagination cursor is invalid.") from exc
    if bound_snapshot != snapshot:
        raise ContextError("REVISION_CONFLICT", "The context collection changed during pagination.")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ContextError("INVALID_URI", "The pagination cursor is invalid.")
    return offset
