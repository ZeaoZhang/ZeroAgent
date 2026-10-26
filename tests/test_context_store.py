"""Persistent context manifest and revision transaction contracts."""

import multiprocessing
import os
import sqlite3
import subprocess
import sys
import hashlib
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
 
from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.store import InMemoryContextStore
from zero_agent.context.types import CollectionPrecondition, ContextObject, collection_fingerprint


def _context_object(uri: str, content: str, *, title: str = "") -> ContextObject:
    parts = uri.split("/")
    owner = parts[3] if uri.startswith("za://users/") else "alice"
    project_id = parts[3] if uri.startswith("za://projects/") else None
    return ContextObject(
        object_id=parts[-1],
        uri=uri,
        kind="user_preference" if "/preferences/" in uri else "project_decision",
        owner_id=owner,
        project_id=project_id,
        content=content,
        title=title,
        tags=("test",),
        source_refs=("receipt-1",),
    )



def test_collection_fingerprint_is_sorted_compact_and_revision_sensitive() -> None:
    assert collection_fingerprint((('za://users/alice/a', 2), ('za://users/alice/b', 1))) == collection_fingerprint((('za://users/alice/b', 1), ('za://users/alice/a', 2)))
    assert collection_fingerprint((('za://users/alice/a', 2),)) != collection_fingerprint((('za://users/alice/a', 3),))
    assert collection_fingerprint((('za://users/alice/a', 2),)) != collection_fingerprint((('za://users/alice/a?rev=2', 2),))


@pytest.mark.parametrize("store_factory", [lambda path: LocalContextStore(path), lambda path: InMemoryContextStore()])
def test_collection_precondition_rejects_changed_member_atomically(tmp_path: Path, store_factory) -> None:
    store = store_factory(tmp_path / 'context')
    scope = 'za://users/alice/memories/preferences'
    member = scope + '/format'
    outside = scope + '-other/value'
    original = store.commit(_context_object(member, 'brief'), expected_revision=None)
    store.commit(_context_object(outside, 'outside'), expected_revision=None)
    condition = CollectionPrecondition(scope, collection_fingerprint(((member, original.revision),)))
    assert store.commit_many(((_context_object(scope + '/style', 'plain'), None),), preconditions=(condition,))
    changed = store.get(member)
    stale = CollectionPrecondition(scope, collection_fingerprint(((member, original.revision + 1),)))
    with pytest.raises(ContextConflictError):
        store.commit_many(((_context_object(scope + '/new', 'new'), None),), preconditions=(stale,))
    assert store.get(scope + '/new') is None


@pytest.mark.parametrize('component', ['percent%literal', 'under_score'])
def test_collection_scope_treats_uri_component_as_literal_not_like_pattern(tmp_path: Path, component: str) -> None:
    store = LocalContextStore(tmp_path / 'context')
    scope = 'za://users/alice/memories/preferences/' + component
    outsider = scope + '-other/value'
    store.commit(_context_object(outsider, 'outside'), expected_revision=None)
    condition = CollectionPrecondition(scope, collection_fingerprint(()))
    store.commit_many(((_context_object(scope + '/value', 'inside'), None),), preconditions=(condition,))


def test_revision_reads_preserve_metadata_and_idempotent_original_snapshot(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/preferences/format"
    first_obj = replace(
        _context_object(uri, "concise", title="Concise title"),
        status="active", source_refs=("receipt-old",), value="concise",
        preference_key="format", scope_key="user:alice", schema_version=2,
        created_at="created-old", updated_at="updated-old",
    )
    first = store.commit(first_obj, expected_revision=None, idempotency_key="snapshot-old")
    second_obj = replace(
        first_obj, content="detailed", title="Detailed title", status="superseded",
        source_refs=("receipt-new",), value="detailed", schema_version=3,
        created_at="created-new", updated_at="updated-new",
    )
    store.commit(second_obj, expected_revision=first.revision)
    historical = store.get(uri, first.revision)
    assert historical == first
    assert store.commit(first_obj, expected_revision=None, idempotency_key="snapshot-old") == first
    assert (historical.title, historical.status, historical.source_refs, historical.value) == (
        "Concise title", "active", ("receipt-old",), "concise"
    )
    assert (historical.preference_key, historical.scope_key, historical.schema_version) == (
        "format", "user:alice", 2
    )
    assert historical.updated_at == first.updated_at
def test_local_store_keeps_immutable_revisions_and_cas(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/preferences/format"
    first = store.commit(
        _context_object(uri, "concise", title="Format"),
        expected_revision=None,
    )
    second = store.commit(
        _context_object(uri, "detailed", title="Format"),
        expected_revision=first.revision,
    )

    assert first.revision == 1
    assert second.revision == 2
    assert store.get(uri).content == "detailed"
    assert store.get(uri, 1).content == "concise"
    assert store.list_objects() == (second,)
    with pytest.raises(ContextConflictError):
        store.commit(_context_object(uri, "stale"), expected_revision=1)


def test_idempotency_replays_same_payload_and_rejects_changed_payload(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/preferences/format"
    obj = _context_object(uri, "concise")
    committed = store.commit(obj, expected_revision=None, idempotency_key="request-1")

    replay = store.commit(obj, expected_revision=None, idempotency_key="request-1")
    assert replay == committed
    with pytest.raises(ContextConflictError, match="idempotency key"):
        store.commit(
            _context_object(uri, "detailed"),
            expected_revision=None,
            idempotency_key="request-1",
        )


def test_commit_many_is_atomic_when_one_revision_conflicts(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    existing_uri = "za://users/alice/memories/preferences/style"
    other_uri = "za://users/alice/memories/preferences/format"
    existing = store.commit(_context_object(existing_uri, "brief"), expected_revision=None)

    with pytest.raises(ContextConflictError):
        store.commit_many(
            (
                (_context_object(other_uri, "markdown"), None),
                (_context_object(existing_uri, "detailed"), existing.revision + 10),
            ),
            idempotency_key="batch-1",
        )

    assert store.get(other_uri) is None
    assert store.get(existing_uri).content == "brief"


def _race_update(root: str, start, ready, results, content: str) -> None:
    store = LocalContextStore(root)
    ready.put("ready")
    start.wait(10)
    uri = "za://users/alice/memories/preferences/style"
    try:
        obj = store.commit(_context_object(uri, content), expected_revision=1)
        results.put(("success", obj.content))
    except ContextConflictError as exc:
        results.put((exc.code, "conflict"))


def test_two_processes_using_same_preimage_have_one_winner(tmp_path: Path) -> None:
    root = str(tmp_path / "context")
    store = LocalContextStore(root)
    uri = "za://users/alice/memories/preferences/style"
    store.commit(_context_object(uri, "initial"), expected_revision=None)

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    ready = context.Queue()
    results = context.Queue()
    processes = [
        context.Process(target=_race_update, args=(root, start, ready, results, value))
        for value in ("one", "two")
    ]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=15) == "ready"
    start.set()
    outcomes = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted(kind for kind, _ in outcomes) == ["REVISION_CONFLICT", "success"]
    assert store.get(uri).content in {"one", "two"}


@pytest.mark.parametrize(
    ("crash_stage", "expected_content"),
    [
        ("staged_file", "initial"),
        ("published_revision", "initial"),
        ("before_manifest_commit", "initial"),
        ("after_manifest_commit", "updated"),
    ],
)
def test_process_crash_exposes_only_old_or_new_revision(
    tmp_path: Path,
    crash_stage: str,
    expected_content: str,
) -> None:
    root = tmp_path / "context"
    uri = "za://users/alice/memories/preferences/style"
    store = LocalContextStore(root)
    store.commit(_context_object(uri, "initial"), expected_revision=None)
    code = r"""
import os
import sys
from pathlib import Path
from zero_agent.context.local_store import LocalContextStore
root, stage = sys.argv[1:]
if stage == "staged_file":
    original = os.replace
    def crash_after_stage(source, target):
        if Path(source).name.startswith(".revision-"):
            os._exit(71)
        return original(source, target)
    os.replace = crash_after_stage
elif stage == "published_revision":
    original = LocalContextStore._publish_revision
    def crash_after_publish(self, *args):
        original(self, *args)
        os._exit(72)
    LocalContextStore._publish_revision = crash_after_publish
elif stage == "before_manifest_commit":
    def crash_before_commit(connection):
        os._exit(73)
    LocalContextStore._commit_transaction = staticmethod(crash_before_commit)
elif stage == "after_manifest_commit":
    original = LocalContextStore._commit_transaction
    def crash_after_commit(connection):
        original(connection)
        os._exit(74)
    LocalContextStore._commit_transaction = staticmethod(crash_after_commit)
store = LocalContextStore(root)
uri = "za://users/alice/memories/preferences/style"
store.commit(
    __import__("zero_agent.context.types", fromlist=["ContextObject"]).ContextObject(
        object_id="style", uri=uri, kind="user_preference", owner_id="alice", content="updated"
    ),
    expected_revision=1,
)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root), crash_stage],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode in {71, 72, 73, 74}
    reopened = LocalContextStore(root)
    assert reopened.get(uri).content == expected_content


def test_backup_restore_and_export_preserve_current_revisions(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/preferences/style"
    current = store.commit(_context_object(uri, "brief", title="Brief"), expected_revision=None)
    store.commit(_context_object(uri, "detailed", title="Detailed"), expected_revision=current.revision)

    backup = store.backup_to(tmp_path / "backup")
    restored = LocalContextStore.restore_backup(backup, tmp_path / "restored")
    export = store.export_json(tmp_path / "export.json")

    assert restored.get(uri).content == "detailed"
    assert restored.get(uri, 1).content == "brief"
    assert restored.get(uri, 1).title == "Brief"
    assert restored.get(uri, 1) == store.get(uri, 1)
    with restored._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        revision_columns = {row[1] for row in connection.execute("PRAGMA table_info(revisions)")}
        assert "metadata_json" in revision_columns
        assert {"context_access_stats", "evidence_receipts"}.issubset(
            {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        )
    assert '"revision": 2' in export.read_text(encoding="utf-8")
    assert '"content": "detailed"' in export.read_text(encoding="utf-8")


def test_backup_verifier_rejects_v5_manifest_missing_required_column(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    backup = store.backup_to(tmp_path / "backup")
    malformed = tmp_path / "malformed"
    import shutil
    shutil.copytree(backup, malformed)
    with sqlite3.connect(malformed / "manifest.sqlite") as connection:
        connection.execute("ALTER TABLE revisions DROP COLUMN metadata_json")
    with pytest.raises(ContextError) as error:
        LocalContextStore.restore_backup(malformed, tmp_path / "rejected")
    assert error.value.code == "INTEGRITY_ERROR"


def test_derived_summary_is_revision_bound_and_marked_stale_on_content_change(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/resources/guide"
    first = store.commit(
        _context_object(uri, "Original source text", title="Guide"),
        expected_revision=None,
    )
    summary = store.save_derived_summary(
        uri,
        view="overview",
        input_revision=first.revision,
        generator_version="extractive-v1",
        content="The original guide explains imports.",
    )
    assert not summary.stale
    store.commit(
        _context_object(uri, "Updated source text", title="Guide"),
        expected_revision=first.revision,
    )

    old_summary = store.get_derived_summary(
        uri,
        view="overview",
        input_revision=first.revision,
        generator_version="extractive-v1",
    )

    assert old_summary is not None and old_summary.stale
    assert store.get_derived_summary(uri, view="overview") is None
    with pytest.raises(ContextConflictError, match="source revision changed"):
        store.save_derived_summary(
            uri,
            view="overview",
            input_revision=first.revision,
            generator_version="extractive-v1",
            content="A late old summary.",
        )


def test_read_detects_revision_hash_tampering(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/preferences/style"
    store.commit(_context_object(uri, "brief"), expected_revision=None)
    with sqlite3.connect(store.manifest_path) as connection:
        relative_path = connection.execute(
            "SELECT relative_path FROM revisions WHERE uri=?", (uri,)
        ).fetchone()[0]
    (store.root / relative_path).write_text("tampered", encoding="utf-8")

    with pytest.raises(ContextError) as error:
        store.get(uri)
    assert error.value.code == "INTEGRITY_ERROR"


def test_missing_manifest_does_not_infer_committed_state_from_revision_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "context"
    store = LocalContextStore(root)
    uri = "za://users/alice/memories/preferences/style"
    store.commit(_context_object(uri, "brief"), expected_revision=None)
    store.manifest_path.unlink()

    with pytest.raises(ContextError, match="manifest is missing") as error:
        LocalContextStore(root)
    assert error.value.code == "INTEGRITY_ERROR"


def test_v1_context_manifest_migrates_without_losing_current_content(tmp_path: Path) -> None:
    root = tmp_path / "context-v1"
    root.mkdir()
    uri = "za://users/alice/memories/facts/os-version"
    content = "macOS 14.5"
    previous_content = "macOS 14.4"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    previous_hash = hashlib.sha256(previous_content.encode("utf-8")).hexdigest()
    uri_hash = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    previous_path = f"objects/{uri_hash[:2]}/{uri_hash}/revisions/1-{previous_hash}.md"
    relative_path = f"objects/{uri_hash[:2]}/{uri_hash}/revisions/2-{content_hash}.md"
    revision_path = root / relative_path
    revision_path.parent.mkdir(parents=True)
    revision_path.write_text(content, encoding="utf-8")
    (root / previous_path).write_text(previous_content, encoding="utf-8")
    connection = sqlite3.connect(root / "manifest.sqlite")
    connection.executescript(
        """
        CREATE TABLE objects (
            uri TEXT PRIMARY KEY, object_id TEXT NOT NULL, kind TEXT NOT NULL,
            owner_id TEXT NOT NULL, project_id TEXT, current_revision INTEGER NOT NULL,
            title TEXT NOT NULL, tags_json TEXT NOT NULL, status TEXT NOT NULL,
            source_refs_json TEXT NOT NULL, schema_version INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE revisions (
            uri TEXT NOT NULL, revision INTEGER NOT NULL, content_sha256 TEXT NOT NULL,
            relative_path TEXT NOT NULL, committed_at TEXT NOT NULL,
            PRIMARY KEY(uri, revision), FOREIGN KEY(uri) REFERENCES objects(uri)
        );
        CREATE TABLE changes (
            change_id TEXT PRIMARY KEY, uri TEXT NOT NULL, before_revision INTEGER,
            after_revision INTEGER NOT NULL, committed_at TEXT NOT NULL
        );
        CREATE TABLE idempotency (
            idempotency_key TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
            result_json TEXT NOT NULL
        );
        """
    )
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        """INSERT INTO objects VALUES (?, ?, ?, ?, NULL, 2, ?, '[]', 'active',
           '[]', 1, 'created', 'updated')""",
        (uri, "os-version", "environment_fact", "alice", "OS version"),
    )
    connection.execute("INSERT INTO revisions VALUES (?, 1, ?, ?, 'committed')", (uri, previous_hash, previous_path))
    connection.execute("INSERT INTO revisions VALUES (?, 2, ?, ?, 'committed')", (uri, content_hash, relative_path))
    connection.commit()
    connection.close()

    store = LocalContextStore(root)
    migrated_obj = store.get(uri)
    assert migrated_obj.content == content
    assert migrated_obj.title == "OS version"
    assert migrated_obj.kind == "environment_fact"
    assert migrated_obj.status == "active"
    assert migrated_obj.source_refs == ()
    assert migrated_obj.tags == ()
    assert migrated_obj.preference_key is None
    assert migrated_obj.value is None
    assert migrated_obj.scope_key is None
    historical = store.get(uri, 1)
    assert historical.content == previous_content
    assert historical.status == "legacy_unverified"
    assert historical.title == ""
    assert historical.source_refs == ()
    assert historical.tags == ()
    assert historical.preference_key is None
    assert historical.value is None
    assert historical.scope_key is None
    with sqlite3.connect(store.manifest_path) as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 5

@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available on Windows")
def test_store_uses_private_data_permissions(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")

    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.manifest_path.stat().st_mode & 0o777 == 0o600


def test_orphan_collection_removes_only_aged_unreferenced_revision_files(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    live_uri = "za://users/alice/memories/preferences/live"
    live = store.commit(_context_object(live_uri, "committed"), expected_revision=None)
    with store._connection() as connection:
        live_relative = connection.execute(
            "SELECT relative_path FROM revisions WHERE uri=?", (live_uri,)
        ).fetchone()[0]

    orphan_digest = hashlib.sha256(b"orphan body").hexdigest()
    orphan_uri = "za://users/alice/memories/preferences/orphan"
    orphan_uri_hash = hashlib.sha256(orphan_uri.encode("utf-8")).hexdigest()
    orphan = store.root / "objects" / orphan_uri_hash[:2] / orphan_uri_hash / "revisions" / f"1-{orphan_digest}.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("orphan body", encoding="utf-8")
    os.utime(orphan, (1, 1))

    preview = store.collect_orphans(grace_seconds=0, dry_run=True)
    assert preview["candidate_files"] == 1
    assert preview["removed_files"] == 0
    assert orphan.is_file()
    assert (store.root / live_relative).is_file()

    collected = store.collect_orphans(grace_seconds=0, dry_run=False)
    assert collected["removed_files"] == 1
    assert collected["reclaimed_bytes"] == len(b"orphan body")
    assert not orphan.exists()
    assert store.get(live_uri).revision == live.revision
    assert (store.root / live_relative).is_file()


def test_purge_preserves_receipt_still_referenced_by_another_object(tmp_path: Path) -> None:
    from zero_agent.context.sources import EvidenceReceiptService
    from zero_agent.context.types import RuntimeContext

    store = LocalContextStore(tmp_path / "context")
    runtime = RuntimeContext("local", "alice", "session-1")
    receipt = EvidenceReceiptService(store).record_user_message(
        runtime, task_id="task-1", event_sequence=1,
        message_id="message-1", text="Remember that concise answers are preferred.",
    )
    first_uri = "za://users/alice/memories/preferences/first"
    second_uri = "za://users/alice/memories/preferences/second"
    first = replace(_context_object(first_uri, "concise"), source_refs=(receipt.receipt_id,))
    second = replace(_context_object(second_uri, "concise"), source_refs=(receipt.receipt_id,))
    committed_first = store.commit(first, expected_revision=None)
    store.commit(second, expected_revision=None)

    result = store.purge_object(
        first_uri, expected_revision=committed_first.revision, principal_id="alice",
        idempotency_key="purge-shared-source",
        idempotency_payload_hash=hashlib.sha256(b"purge-shared-source").hexdigest(),
    )

    surviving = store.get_evidence_receipt(receipt.receipt_id)
    assert result.cleanup_complete
    assert not result.source_cleanup_complete
    assert result.source_cleanup_reason == "source_still_referenced"
    assert surviving is not None and not surviving.revoked
    assert surviving.excerpt == "Remember that concise answers are preferred."


def test_failed_export_replacement_preserves_published_file_and_catalog(tmp_path: Path, monkeypatch) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/preferences/exported"
    original = store.commit(_context_object(uri, "original"), expected_revision=None)
    destination = tmp_path / "exports" / "memory.json"
    store.export_json(destination)
    original_bytes = destination.read_bytes()
    store.commit(_context_object(uri, "updated"), expected_revision=original.revision)

    def fail_object_read(_row):
        raise OSError("simulated export serialization failure")

    monkeypatch.setattr(store, "_object_from_row", fail_object_read)
    with pytest.raises(OSError, match="simulated export serialization failure"):
        store.export_json(destination)

    assert destination.read_bytes() == original_bytes
    assert len(store._find_exports_containing_uri(uri)) == 1
    with store._connection() as connection:
        row = connection.execute(
            "SELECT state, staging_path FROM export_catalog WHERE path=?",
            (str(destination.resolve()),),
        ).fetchone()
    assert row["state"] == "committed"
    assert row["staging_path"] is None


def test_export_replacement_fsync_failure_restores_previous_file(tmp_path: Path, monkeypatch) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/preferences/fsync-export"
    original = store.commit(_context_object(uri, "original"), expected_revision=None)
    destination = tmp_path / "exports" / "memory.json"
    store.export_json(destination)
    previous_bytes = destination.read_bytes()
    store.commit(_context_object(uri, "updated"), expected_revision=original.revision)

    from zero_agent.context import local_store

    real_fsync_dir = local_store._fsync_dir
    calls = 0

    def fail_after_replace(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated post-replacement directory sync failure")
        real_fsync_dir(path)

    monkeypatch.setattr(local_store, "_fsync_dir", fail_after_replace)
    with pytest.raises(OSError, match="post-replacement directory sync failure"):
        store.export_json(destination)

    payload = __import__("json").loads(destination.read_text(encoding="utf-8"))
    assert payload["objects"][0]["content"] == "updated"
    with store._connection() as connection:
        row = connection.execute(
            "SELECT state, staging_path FROM export_catalog WHERE path=?",
            (str(destination.resolve()),),
        ).fetchone()
    assert row["state"] == "pending"
    assert row["staging_path"] is not None


def test_pending_export_is_recovered_after_publication_interruption(tmp_path: Path, monkeypatch) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/preferences/pending-export"
    obj = store.commit(_context_object(uri, "exported"), expected_revision=None)
    destination = tmp_path / "exports" / "pending.json"
    real_fsync_dir = __import__("zero_agent.context.local_store", fromlist=["_fsync_dir"])._fsync_dir
    calls = 0

    def interrupt_after_replace(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated interruption after replacement")
        real_fsync_dir(path)

    monkeypatch.setattr("zero_agent.context.local_store._fsync_dir", interrupt_after_replace)
    with pytest.raises(OSError, match="after replacement"):
        store.export_json(destination)
    with store._connection() as connection:
        row = connection.execute(
            "SELECT state FROM export_catalog WHERE path=?", (str(destination.resolve()),)
        ).fetchone()
    assert row["state"] == "pending"

    monkeypatch.setattr("zero_agent.context.local_store._fsync_dir", real_fsync_dir)
    store.export_json(destination)
    with store._connection() as connection:
        row = connection.execute(
            "SELECT state, staging_path FROM export_catalog WHERE path=?",
            (str(destination.resolve()),),
        ).fetchone()
    payload = __import__("json").loads(destination.read_text(encoding="utf-8"))
    assert payload["objects"][0]["revision"] == obj.revision
    assert row["state"] == "committed"


def test_pending_export_rejects_content_not_bound_to_current_revision(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/preferences/tampered-export"
    obj = store.commit(_context_object(uri, "committed"), expected_revision=None)
    destination = tmp_path / "exports" / "tampered.json"
    staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.staging"
    destination.parent.mkdir(parents=True)
    staging.write_text("incomplete", encoding="utf-8")
    payload = {
        "format": "zero-agent-context-export/v1",
        "objects": [{
            "object_id": obj.object_id, "uri": obj.uri, "kind": obj.kind,
            "owner_id": obj.owner_id, "project_id": obj.project_id,
            "revision": obj.revision, "content_sha256": obj.content_sha256,
            "title": obj.title, "tags": list(obj.tags), "status": obj.status,
            "source_refs": list(obj.source_refs), "schema_version": obj.schema_version,
            "content": "tampered", "preference_key": obj.preference_key,
            "scope_key": obj.scope_key, "value": obj.value,
        }],
    }
    destination.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with store._connection() as connection:
        connection.execute(
            "INSERT INTO export_catalog (export_id,path,created_at,state,staging_path) VALUES (?,?,?,?,?)",
            ("pending-export", str(destination.resolve()), "now", "pending", str(staging)),
        )

    with pytest.raises(ContextError) as error:
        store.export_json(destination)

    assert error.value.code == "INTEGRITY_ERROR"
    with store._connection() as connection:
        row = connection.execute(
            "SELECT state, staging_path FROM export_catalog WHERE export_id='pending-export'"
        ).fetchone()
    assert row["state"] == "pending" and row["staging_path"] == str(staging)


def test_failed_initial_backup_publication_removes_pending_catalog_and_staging(tmp_path: Path) -> None:
    store = LocalContextStore(tmp_path / "context")
    target = tmp_path / "broken-backup"

    def fail_copy(_staging: Path, _connection: sqlite3.Connection) -> None:
        raise OSError("simulated backup build failure")

    with pytest.raises(OSError, match="backup build failure"):
        store._publish_cataloged_copy(kind="backup", destination=target, build=fail_copy)

    with store._connection() as connection:
        rows = connection.execute("SELECT path, state FROM backup_catalog").fetchall()
    assert rows == []
    assert not target.exists()


def test_post_replace_backup_sync_failure_rolls_back_publication(tmp_path: Path, monkeypatch) -> None:
    store = LocalContextStore(tmp_path / "context")
    target = tmp_path / "pending-backup"
    real_fsync_dir = __import__("zero_agent.context.local_store", fromlist=["_fsync_dir"])._fsync_dir
    calls = 0

    def interrupt_after_replace(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated backup publication interruption")
        real_fsync_dir(path)

    monkeypatch.setattr("zero_agent.context.local_store._fsync_dir", interrupt_after_replace)
    with pytest.raises(OSError, match="backup publication interruption"):
        store.backup_to(target)
    assert not target.exists()
    with store._connection() as connection:
        row = connection.execute(
            "SELECT state FROM backup_catalog WHERE path=?",
            (str(target.resolve()),),
        ).fetchone()
    assert row is None
