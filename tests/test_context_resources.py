"""Explicit resource import limits, provenance, and project scope."""

from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.resources import ResourceService
from zero_agent.context.sources import EvidenceReceiptService
from zero_agent.context.types import RuntimeContext


def _runtime():
    return RuntimeContext(
        "local",
        "alice",
        "session-1",
        project_id="p-1",
        capabilities={"project:p-1:write"},
    )


def test_explicit_text_import_stores_source_hash_and_logical_uri(tmp_path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("A short project decision.", encoding="utf-8")
    store = LocalContextStore(tmp_path / "context")
    runtime = _runtime()
    receipts = EvidenceReceiptService(store)
    resources = ResourceService(store, receipts, runtime)

    obj = resources.import_file(
        source,
        target_uri="za://projects/p-1/resources/docs/notes",
        title="Notes",
        task_id="task-import",
    )

    assert obj.kind == "resource"
    assert obj.content == "A short project decision."
    assert obj.uri == "za://projects/p-1/resources/docs/notes"
    receipt = store.get_evidence_receipt(obj.source_refs[0])
    assert receipt is not None
    assert receipt.source_type == "resource_import"
    assert receipt.source_id.startswith("sha256:")


def test_directory_import_is_bounded_and_reports_skips(tmp_path) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    (root / "one.md").write_text("one", encoding="utf-8")
    (root / "two.md").write_text("two", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"\xff\x00")
    store = LocalContextStore(tmp_path / "context")
    resources = ResourceService(
        store,
        EvidenceReceiptService(store),
        _runtime(),
        max_files=1,
    )

    result = resources.import_directory(
        root,
        target_prefix="za://projects/p-1/resources/docs",
    )

    assert len(result.imported) == 1
    assert {reason for _, reason in result.skipped} >= {"file_count_limit", "not_utf8_text"}
    assert len(store.list_objects()) == 1


def test_directory_import_retry_does_not_create_duplicate_source_receipts(tmp_path) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    (root / "one.md").write_text("one", encoding="utf-8")
    store = LocalContextStore(tmp_path / "context")
    resources = ResourceService(store, EvidenceReceiptService(store), _runtime())

    first = resources.import_directory(root, target_prefix="za://projects/p-1/resources/docs")
    second = resources.import_directory(root, target_prefix="za://projects/p-1/resources/docs")
    with store._connection() as connection:
        receipt_count = connection.execute("SELECT COUNT(*) FROM evidence_receipts").fetchone()[0]

    assert first.imported == second.imported
    assert receipt_count == 1
