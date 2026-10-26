"""Explicit, bounded imports of UTF-8 text resources into context storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.sources import EvidenceReceiptService
from zero_agent.context.types import ContextObject, RuntimeContext
from zero_agent.context.uri import parse_context_uri


@dataclass(frozen=True)
class ResourceImportResult:
    imported: tuple[ContextObject, ...]
    skipped: tuple[tuple[str, str], ...]


class ResourceService:
    """Import explicit local text files as immutable, source-hashed resources."""

    def __init__(
        self,
        store: LocalContextStore,
        receipts: EvidenceReceiptService,
        runtime: RuntimeContext,
        *,
        max_file_bytes: int = 1_000_000,
        max_files: int = 200,
        max_depth: int = 8,
        max_total_bytes: int = 20_000_000,
    ) -> None:
        if min(max_file_bytes, max_files, max_depth, max_total_bytes) < 1:
            raise ValueError("resource import limits must be positive")
        self.store = store
        self.receipts = receipts
        self.runtime = runtime
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.max_depth = max_depth
        self.max_total_bytes = max_total_bytes

    def import_file(
        self,
        source_path: str | Path,
        *,
        target_uri: str,
        title: str | None = None,
        expected_revision: int | None = None,
        task_id: str = "resource-import",
        event_sequence: int = 1,
        idempotency_key: str | None = None,
    ) -> ContextObject:
        parsed = parse_context_uri(target_uri, self.runtime)
        self._authorize_project_target(parsed)
        source = Path(source_path).expanduser()
        if source.is_symlink() or not source.is_file():
            raise ContextError("INVALID_SOURCE", "The import source must be a regular non-symlink file.")
        size = source.stat().st_size
        if size > self.max_file_bytes:
            raise ContextError("INVALID_SOURCE", "The resource exceeds the per-file size limit.")
        try:
            raw = source.read_bytes()
            content = raw.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            raise ContextError("INVALID_SOURCE", "Only readable UTF-8 text resources can be imported.") from exc
        if "\x00" in content:
            raise ContextError("INVALID_SOURCE", "Binary content is not a text resource.")
        content_hash = hashlib.sha256(raw).hexdigest()
        request_hash = _request_hash(parsed.canonical, content_hash, expected_revision)
        key = idempotency_key or f"resource:{request_hash[:48]}"
        prior = self.store.lookup_idempotency(key, request_hash)
        if prior is not None:
            return prior[0]
        current = self.store.get(parsed.canonical)
        if (current.revision if current is not None else None) != expected_revision:
            raise ContextConflictError("The resource target revision changed before import.")
        receipt = self.receipts.record_resource_import(
            self.runtime,
            task_id=task_id,
            event_sequence=event_sequence,
            content_sha256=content_hash,
            size_bytes=len(raw),
        )
        obj = ContextObject(
            object_id=parsed.segments[-1],
            uri=parsed.canonical,
            kind="resource",
            owner_id=self.runtime.user_id,
            project_id=parsed.segments[0],
            content=content,
            title=title or parsed.segments[-1],
            status="active",
            source_refs=(receipt.receipt_id,),
        )
        return self.store.commit(
            obj,
            expected_revision=expected_revision,
            idempotency_key=key,
            idempotency_payload_hash=request_hash,
        )

    def import_directory(
        self,
        source_dir: str | Path,
        *,
        target_prefix: str,
        task_id: str = "resource-import",
        idempotency_key: str | None = None,
    ) -> ResourceImportResult:
        prefix = parse_context_uri(target_prefix, self.runtime)
        self._authorize_project_target(prefix)
        root = Path(source_dir).expanduser()
        if root.is_symlink() or not root.is_dir():
            raise ContextError("INVALID_SOURCE", "The import root must be an explicit regular directory.")
        resolved_root = root.resolve(strict=True)
        candidates, skipped = self._enumerate(root)
        planned: list[tuple[Path, str, str, bytes, str]] = []
        total_bytes = 0
        for path, relative in candidates:
            if path.is_symlink() or not path.is_file():
                skipped.append((relative, "not_a_regular_file"))
                continue
            try:
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                skipped.append((relative, "path_escape"))
                continue
            if len(planned) >= self.max_files:
                skipped.append((relative, "file_count_limit"))
                continue
            size = path.stat().st_size
            if size > self.max_file_bytes:
                skipped.append((relative, "file_size_limit"))
                continue
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                skipped.append((relative, "not_utf8_text"))
                continue
            if "\x00" in text:
                skipped.append((relative, "binary_content"))
                continue
            if total_bytes + len(raw) > self.max_total_bytes:
                skipped.append((relative, "total_size_limit"))
                continue
            total_bytes += len(raw)
            digest = hashlib.sha256(raw).hexdigest()
            stable_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]
            object_uri = f"za://{prefix.authority}/" + "/".join(
                quote(segment, safe="-._~") for segment in (*prefix.segments, stable_id)
            )
            planned.append((path, relative, object_uri, raw, digest))

        request_hash = _directory_request_hash(
            [(uri, digest) for _, _, uri, _, digest in planned]
        )
        key = idempotency_key or f"directory:{request_hash[:48]}"
        prior = self.store.lookup_idempotency(key, request_hash)
        if prior is not None:
            return ResourceImportResult(tuple(prior), tuple(skipped))

        operations: list[tuple[ContextObject, int | None]] = []
        source_receipts = []
        for index, (path, relative, uri, raw, digest) in enumerate(planned, start=1):
            current = self.store.get(uri)
            expected = current.revision if current is not None else None
            receipt = self.receipts.record_resource_import(
                self.runtime,
                task_id=task_id,
                event_sequence=index,
                content_sha256=digest,
                size_bytes=len(raw),
            )
            source_receipts.append(receipt)
            parsed = parse_context_uri(uri, self.runtime)
            text = raw.decode("utf-8")
            operations.append((
                ContextObject(
                    object_id=parsed.segments[-1],
                    uri=parsed.canonical,
                    kind="resource",
                    owner_id=self.runtime.user_id,
                    project_id=parsed.segments[0],
                    content=text,
                    title=Path(relative).name,
                    source_refs=(receipt.receipt_id,),
                ),
                expected,
            ))
        if not operations:
            return ResourceImportResult((), tuple(skipped))
        imported = self.store.commit_many(
            operations,
            idempotency_key=key,
            idempotency_payload_hash=request_hash,
        )
        return ResourceImportResult(tuple(imported), tuple(skipped))

    def _enumerate(self, root: Path) -> tuple[list[tuple[Path, str]], list[tuple[str, str]]]:
        candidates: list[tuple[Path, str]] = []
        skipped: list[tuple[str, str]] = []
        for current, dirs, files in os.walk(root, followlinks=False):
            if len(candidates) + len(skipped) >= self.max_files * 2:
                skipped.append(("", "enumeration_limit"))
                break
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            if depth >= self.max_depth:
                for name in dirs:
                    skipped.append(((current_path / name).relative_to(root).as_posix(), "depth_limit"))
                dirs[:] = []
            else:
                kept_dirs = []
                for name in dirs:
                    candidate = current_path / name
                    if candidate.is_symlink():
                        skipped.append((candidate.relative_to(root).as_posix(), "symlink_directory"))
                    else:
                        kept_dirs.append(name)
                dirs[:] = kept_dirs
            for name in files:
                path = current_path / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    skipped.append((relative, "symlink_file"))
                else:
                    candidates.append((path, relative))
        candidates.sort(key=lambda item: item[1])
        return candidates, skipped

    def _authorize_project_target(self, parsed) -> None:
        if parsed.authority != "projects" or len(parsed.segments) < 3 or parsed.segments[1] != "resources":
            raise ContextError("INVALID_URI", "Resources must be stored under a project resource collection.")
        project_id = parsed.segments[0]
        if project_id != self.runtime.project_id or (
            f"project:{project_id}:write" not in self.runtime.capabilities
            and "context:admin" not in self.runtime.capabilities
        ):
            raise ContextError("PERMISSION_DENIED", "The current identity cannot import into this project.")


def _request_hash(uri: str, content_sha256: str, expected_revision: int | None) -> str:
    payload = json.dumps(
        [uri, content_sha256, expected_revision],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _directory_request_hash(resources: list[tuple[str, str]]) -> str:
    payload = sorted(resources)
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
