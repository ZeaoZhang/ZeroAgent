"""Context service scope filtering and in-memory CAS contract."""

import pytest

from zero_agent.context.errors import ContextConflictError, ContextError
from zero_agent.context.service import ContextService
from zero_agent.context.store import InMemoryContextStore
from zero_agent.context.types import ContextObject, RuntimeContext


def _object(
    uri: str,
    owner: str,
    content: str,
    *,
    project_id: str | None = None,
) -> ContextObject:
    return ContextObject(
        object_id=uri.rsplit("/", 1)[-1],
        uri=uri,
        kind="user_preference",
        owner_id=owner,
        content=content,
        project_id=project_id,
        title="Reply style",
    )


def test_user_reads_are_scoped_and_search_does_not_leak_counts() -> None:
    store = InMemoryContextStore()
    store.commit(
        _object("za://users/alice/memories/preferences/style", "alice", "brief responses"),
        expected_revision=None,
    )
    store.commit(
        _object("za://users/bob/memories/preferences/style", "bob", "brief responses"),
        expected_revision=None,
    )
    service = ContextService(
        store,
        RuntimeContext("local", "alice", "session-a"),
    )

    results = service.search("brief", limit=1)

    assert [result.uri for result in results["results"]] == [
        "za://users/alice/memories/preferences/style"
    ]
    assert results["next_cursor"] is None
    with pytest.raises(ContextError) as error:
        service.read("za://users/bob/memories/preferences/style")
    assert error.value.code == "PERMISSION_DENIED"


def test_search_excludes_unverified_and_conflicted_objects() -> None:
    store = InMemoryContextStore()
    for suffix, status in (("verified", "active"), ("candidate", "candidate_unverified"), ("conflict", "conflicted")):
        store.commit(
            ContextObject(
                object_id=suffix,
                uri=f"za://skills/builtin/{suffix}",
                kind="skill",
                owner_id="builtin",
                content="macOS arm64",
                status=status,
            ),
            expected_revision=None,
        )
    service = ContextService(store, RuntimeContext("local", "alice", "session-a"))

    results = service.search("macOS", scope="za://skills/builtin")

    assert [result.uri for result in results["results"]] == ["za://skills/builtin/verified"]


def test_revoked_source_makes_an_active_memory_non_searchable(tmp_path) -> None:
    from zero_agent.context.local_store import LocalContextStore
    from zero_agent.context.sources import EvidenceReceiptService

    store = LocalContextStore(tmp_path / "context")
    runtime = RuntimeContext("local", "alice", "session-a")
    receipts = EvidenceReceiptService(store)
    receipt = receipts.record_user_message(
        runtime,
        task_id="task-1",
        event_sequence=1,
        message_id="message-1",
        text="Remember that I prefer concise answers.",
    )
    obj = ContextObject(
        object_id="style",
        uri="za://users/alice/memories/preferences/style",
        kind="user_preference",
        owner_id="alice",
        content="concise",
        source_refs=(receipt.receipt_id,),
    )
    store.commit(obj, expected_revision=None)
    service = ContextService(store, runtime, receipt_store=store)
    assert len(service.search("concise")["results"]) == 1

    receipts.revoke(receipt.receipt_id, runtime, reason_code="user_retracted")

    assert service.search("concise")["results"] == []
    listed = service.list("za://users/alice/memories/preferences")
    assert listed["items"][0].status == "candidate_unverified"


def test_mismatched_record_metadata_fails_closed() -> None:
    store = InMemoryContextStore()
    obj = _object("za://users/alice/memories/preferences/style", "bob", "private")
    store.commit(obj, expected_revision=None)
    service = ContextService(store, RuntimeContext("local", "alice", "session-a"))

    with pytest.raises(ContextError) as error:
        service.read(obj.uri)
    assert error.value.code == "INTEGRITY_ERROR"


def test_project_access_requires_current_binding_or_read_capability() -> None:
    store = InMemoryContextStore()
    obj = _object(
        "za://projects/p-1/memories/decisions/db",
        "alice",
        "sqlite",
        project_id="p-1",
    )
    store.commit(obj, expected_revision=None)
    denied = ContextService(store, RuntimeContext("local", "alice", "s-1"))
    with pytest.raises(ContextError, match="outside the current grants"):
        denied.read(obj.uri)

    granted = ContextService(
        store,
        RuntimeContext("local", "alice", "s-1", capabilities={"project:p-1:read"}),
    )
    assert granted.read(obj.uri).content == "sqlite"


def test_unscoped_search_stays_within_the_current_project_and_user() -> None:
    store = InMemoryContextStore()
    for uri, owner, project_id, content in (
        ("za://users/alice/memories/resources/note", "alice", None, "shared marker personal"),
        ("za://users/bob/memories/resources/note", "bob", None, "shared marker foreign"),
        ("za://projects/current/resources/docs/note", "alice", "current", "shared marker current"),
        ("za://projects/granted/resources/docs/note", "alice", "granted", "shared marker granted"),
    ):
        store.commit(
            ContextObject(
                object_id="note", uri=uri, kind="resource", owner_id=owner,
                project_id=project_id, title="Note", content=content,
            ),
            expected_revision=None,
        )
    service = ContextService(
        store,
        RuntimeContext(
            "local", "alice", "session", project_id="current",
            capabilities=frozenset({"project:current:read", "project:granted:read"}),
        ),
    )

    default_results = service.search("shared marker")["results"]
    expanded_results = service.search("shared marker", scope="za://projects/granted/resources")["results"]
    with pytest.raises(ContextError) as denied_expansion:
        service.search("shared marker", scope="za://projects/other/resources")
    assert denied_expansion.value.code == "PERMISSION_DENIED"

    assert {item.uri for item in default_results} == {
        "za://users/alice/memories/resources/note",
        "za://projects/current/resources/docs/note",
    }
    assert [item.uri for item in expanded_results] == ["za://projects/granted/resources/docs/note"]



def test_lists_only_authorized_items_under_a_collection() -> None:
    store = InMemoryContextStore()
    for uri, owner in (
        ("za://users/alice/memories/preferences/tone", "alice"),
        ("za://users/alice/memories/preferences/language", "alice"),
        ("za://users/bob/memories/preferences/tone", "bob"),
    ):
        store.commit(_object(uri, owner, "content"), expected_revision=None)
    service = ContextService(store, RuntimeContext("local", "alice", "s-1"))

    page = service.list("za://users/me/memories/preferences", limit=1)
    second = service.list("za://users/alice/memories/preferences", cursor=page["next_cursor"])

    assert len(page["items"]) == 1
    assert page["next_cursor"]
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None


def test_pagination_cursor_is_bound_to_the_collection_snapshot() -> None:
    store = InMemoryContextStore()
    first_uri = "za://users/alice/memories/facts/one"
    second_uri = "za://users/alice/memories/facts/two"
    store.commit(_object(first_uri, "alice", "same"), expected_revision=None)
    store.commit(_object(second_uri, "alice", "same"), expected_revision=None)
    service = ContextService(store, RuntimeContext("local", "alice", "s-1"))
    first_page = service.list("za://users/alice/memories/facts", limit=1)
    store.commit(_object("za://users/alice/memories/facts/three", "alice", "same"), expected_revision=None)

    with pytest.raises(ContextError) as error:
        service.list(
            "za://users/alice/memories/facts",
            cursor=first_page["next_cursor"],
            limit=1,
        )
    assert error.value.code == "REVISION_CONFLICT"


def test_search_cursor_is_bound_to_query_and_scope() -> None:
    store = InMemoryContextStore()
    for suffix in ("one", "two"):
        store.commit(
            _object(
                f"za://users/alice/memories/resources/{suffix}",
                "alice",
                "shared search marker",
            ),
            expected_revision=None,
        )
    service = ContextService(store, RuntimeContext("local", "alice", "s-1"))
    first_page = service.search("shared search marker", limit=1)

    with pytest.raises(ContextError) as error:
        service.search(
            "different search marker",
            cursor=first_page["next_cursor"],
            limit=1,
        )

    assert error.value.code == "REVISION_CONFLICT"



def test_lexical_search_uses_chinese_bigrams() -> None:
    store = InMemoryContextStore()
    obj = ContextObject(
        object_id="preference",
        uri="za://users/alice/memories/preferences/detail",
        kind="user_preference",
        owner_id="alice",
        content="我通常希望回答更简洁，并保留关键细节。",
        title="回答风格",
    )
    store.commit(obj, expected_revision=None)
    service = ContextService(store, RuntimeContext("local", "alice", "s-1"))

    results = service.search("简洁回答")

    assert len(results["results"]) == 1
    assert results["results"][0].matched_terms == ("简洁", "回答")


def test_search_preserves_distinct_logical_uris_with_same_basename() -> None:
    store = InMemoryContextStore()
    for uri in (
        "za://users/alice/memories/resources/shared-note",
        "za://projects/p-1/resources/docs/shared-note",
    ):
        store.commit(
            ContextObject(
                object_id="shared-note",
                uri=uri,
                kind="resource",
                owner_id="alice",
                project_id="p-1" if uri.startswith("za://projects/") else None,
                title="Migration note",
                content="SQLite migration checkpoint",
            ),
            expected_revision=None,
        )
    service = ContextService(store, RuntimeContext("local", "alice", "s-1", project_id="p-1"))
    results = service.search("migration checkpoint", limit=10)["results"]
    assert {result.uri for result in results} == {
        "za://users/alice/memories/resources/shared-note",
        "za://projects/p-1/resources/docs/shared-note",
    }

def test_read_view_uses_only_the_summary_for_the_requested_revision(tmp_path) -> None:
    from zero_agent.context.local_store import LocalContextStore

    store = LocalContextStore(tmp_path / "context")
    uri = "za://users/alice/memories/resources/guide"
    first = store.commit(
        ContextObject(
            object_id="guide",
            uri=uri,
            kind="resource",
            owner_id="alice",
            title="Guide",
            content="Original full text.",
        ),
        expected_revision=None,
    )
    store.save_derived_summary(
        uri,
        view="overview",
        input_revision=first.revision,
        generator_version="summary-v1",
        content="Original overview.",
    )
    service = ContextService(store, RuntimeContext("local", "alice", "s-1"))
    assert service.read_view(uri, view="overview")["content"] == "Original overview."

    store.commit(
        ContextObject(
            object_id="guide",
            uri=uri,
            kind="resource",
            owner_id="alice",
            title="Guide",
            content="Updated full text.",
        ),
        expected_revision=first.revision,
    )

    overview = service.read_view(uri, view="overview")
    detail = service.read_view(uri, view="detail")
    assert overview["summary_status"] == "unavailable"
    assert overview["content"] == "Guide"
    assert detail["content"] == "Updated full text."


def test_memory_store_preserves_old_revision_and_rejects_stale_cas() -> None:
    store = InMemoryContextStore()
    initial = store.commit(
        _object("za://users/alice/memories/preferences/style", "alice", "concise"),
        expected_revision=None,
    )
    revised = store.commit(
        _object("za://users/alice/memories/preferences/style", "alice", "detailed"),
        expected_revision=initial.revision,
    )

    assert revised.revision == 2
    assert store.get(initial.uri, initial.revision).content == "concise"
    with pytest.raises(ContextConflictError):
        store.commit(
            _object(initial.uri, "alice", "stale update"),
            expected_revision=initial.revision,
        )
