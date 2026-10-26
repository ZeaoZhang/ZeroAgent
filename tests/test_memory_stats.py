"""Context access statistics are scoped logical-object counters."""

from __future__ import annotations

import sqlite3

from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.service import ContextService
from zero_agent.context.store import InMemoryContextStore
from zero_agent.context.types import ContextObject, RuntimeContext


def _object(uri: str, owner: str, content: str, *, project_id: str | None = None) -> ContextObject:
    return ContextObject(
        object_id=uri.rsplit("/", 1)[-1],
        uri=uri,
        kind="user_preference",
        owner_id=owner,
        content=content,
        project_id=project_id,
        title="Reply style",
    )


def test_same_basename_stats_are_isolated_by_principal_and_scope() -> None:
    store = InMemoryContextStore()
    alice_uri = "za://users/alice/memories/preferences/style"
    bob_uri = "za://users/bob/memories/preferences/style"
    project_uri = "za://projects/project-a/memories/preferences/style"
    store.commit(_object(alice_uri, "alice", "alice style"), expected_revision=None)
    store.commit(_object(bob_uri, "bob", "bob style"), expected_revision=None)
    store.commit(_object(project_uri, "project-writer", "project style", project_id="project-a"), expected_revision=None)

    runtime = RuntimeContext(
        "account-a",
        "alice",
        "session-a",
        project_id="project-a",
        capabilities={"project:project-a:read"},
    )
    service = ContextService(store, runtime)
    listed = service.list("za://users/alice/memories/preferences")
    project_listed = service.list("za://projects/project-a/memories/preferences")

    assert [obj.uri for obj in listed["items"]] == [alice_uri]
    assert [obj.uri for obj in project_listed["items"]] == [project_uri]
    stats = service.list_access_stats()
    assert {(item.scope_key, item.uri, item.count, item.operation) for item in stats} == {
        ("user:alice", alice_uri, 1, "list"),
        ("project:project-a", project_uri, 1, "list"),
    }


def test_only_returned_objects_are_counted() -> None:
    store = InMemoryContextStore()
    first = "za://users/alice/memories/preferences/first"
    second = "za://users/alice/memories/preferences/second"
    store.commit(_object(first, "alice", "first"), expected_revision=None)
    store.commit(_object(second, "alice", "second"), expected_revision=None)
    service = ContextService(store, RuntimeContext("account", "alice", "session"))

    page = service.list("za://users/alice/memories/preferences", limit=1)
    assert len(page["items"]) == 1
    stats = service.list_access_stats()
    assert {(item.uri, item.operation) for item in stats} == {(first, "list")}


def test_unauthorized_damaged_object_is_not_opened(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    alice_uri = "za://users/alice/memories/preferences/style"
    bob_uri = "za://users/bob/memories/preferences/style"
    store.commit(_object(alice_uri, "alice", "readable"), expected_revision=None)
    store.commit(_object(bob_uri, "bob", "not readable"), expected_revision=None)
    with store._connection() as connection:
        relative_path = connection.execute(
            "SELECT relative_path FROM revisions WHERE uri=? AND revision=1",
            (bob_uri,),
        ).fetchone()["relative_path"]
    (store.root / relative_path).unlink()

    service = ContextService(store, RuntimeContext("account", "alice", "session"))
    result = service.list("za://users/alice/memories/preferences")
    assert [obj.uri for obj in result["items"]] == [alice_uri]


def test_stats_failure_does_not_fail_content_reads(monkeypatch) -> None:
    store = InMemoryContextStore()
    uri = "za://users/alice/memories/preferences/style"
    store.commit(_object(uri, "alice", "readable"), expected_revision=None)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("stats unavailable")

    monkeypatch.setattr(store, "record_access", fail)
    service = ContextService(store, RuntimeContext("account", "alice", "session"))
    assert service.read(uri).content == "readable"


def test_encoded_user_and_project_scope_keys_match_across_store_providers(tmp_path) -> None:
    user_uri = "za://users/alice%E4%B8%80/memories/preferences/style"
    project_uri = "za://projects/project%E4%B8%80/memories/preferences/style"
    for store in (InMemoryContextStore(), LocalContextStore(tmp_path / "context")):
        user_runtime = RuntimeContext("account", "alice一", "session")
        project_runtime = RuntimeContext(
            "account", "alice一", "session", capabilities={"project:project一:read"},
        )

        store.commit(_object(user_uri, "alice一", "user"), expected_revision=None)
        store.commit(_object(project_uri, "writer", "project", project_id="project一"), expected_revision=None)
        service = ContextService(store, user_runtime)
        service.read(user_uri)
        ContextService(store, project_runtime).read(project_uri)
        stats = ContextService(store, user_runtime).list_access_stats()
        project_stats = ContextService(store, project_runtime).list_access_stats()
        scopes = {item.uri: item.scope_key for item in (*stats, *project_stats)}
        assert scopes[user_uri] == "user:alice一"
        assert scopes[project_uri] == "project:project一"


def test_access_stats_are_principal_scoped_and_rechecked_against_current_grants(tmp_path) -> None:
    store = LocalContextStore(tmp_path / "context")
    alice_uri = "za://users/alice/memories/preferences/style"
    bob_uri = "za://users/bob/memories/preferences/style"
    project_uri = "za://projects/project-a/memories/preferences/style"
    store.commit(_object(alice_uri, "alice", "alice style"), expected_revision=None)
    store.commit(_object(bob_uri, "bob", "bob style"), expected_revision=None)
    store.commit(
        _object(project_uri, "alice", "project style", project_id="project-a"),
        expected_revision=None,
    )
    alice = RuntimeContext(
        "account-a", "alice", "session-a", project_id="project-a",
        capabilities={"project:project-a:read"},
    )
    bob = RuntimeContext("account-a", "bob", "session-b")
    ContextService(store, alice).read(alice_uri)
    ContextService(store, alice).read(project_uri)
    ContextService(store, bob).read(bob_uri)

    alice_stats = ContextService(store, alice).list_access_stats()
    bob_stats = ContextService(store, bob).list_access_stats()
    no_project_grant = ContextService(
        store, RuntimeContext("account-a", "alice", "session-c"),
    ).list_access_stats()

    assert {item.uri for item in alice_stats} == {alice_uri, project_uri}
    assert {item.uri for item in bob_stats} == {bob_uri}
    assert {item.uri for item in no_project_grant} == {alice_uri}
    assert all(item.count == 1 and item.operation == "read" for item in alice_stats)
    assert {item.scope_key for item in alice_stats} == {"user:alice", "project:project-a"}


def test_access_stats_hide_noncurrent_object_revisions() -> None:
    store = InMemoryContextStore()
    uri = "za://users/alice/memories/preferences/style"
    original = store.commit(_object(uri, "alice", "original"), expected_revision=None)
    runtime = RuntimeContext("account", "alice", "session")
    service = ContextService(store, runtime)
    service.read(uri)

    store.commit(_object(uri, "alice", "updated"), expected_revision=original.revision)

    assert service.list_access_stats() == ()


def test_access_stats_hide_currently_retracted_objects() -> None:
    store = InMemoryContextStore()
    uri = "za://users/alice/memories/preferences/style"
    initial = _object(uri, "alice", "original")
    saved = store.commit(initial, expected_revision=None)
    runtime = RuntimeContext("account", "alice", "session")
    service = ContextService(store, runtime)
    service.read(uri)

    from dataclasses import replace

    store.commit(replace(saved, status="retracted"), expected_revision=saved.revision)

    assert service.list_access_stats() == ()


def test_access_stats_include_readable_builtins_without_store_objects() -> None:
    store = InMemoryContextStore()
    runtime = RuntimeContext("account", "alice", "session")
    builtin = ContextObject(
        object_id="sop", uri="za://skills/builtin/sop", kind="skill",
        owner_id="builtin", content="Reference body",
    )
    service = ContextService(store, runtime, builtin_objects=(builtin,))
    assert service.read(builtin.uri).content == "Reference body"

    stats = service.list_access_stats()

    assert [(item.uri, item.scope_key, item.operation) for item in stats] == [
        (builtin.uri, "skill:builtin", "read"),
    ]


def test_access_stats_hide_records_for_objects_now_owned_by_another_principal() -> None:
    store = InMemoryContextStore()
    uri = "za://users/alice/memories/preferences/style"
    initial = _object(uri, "alice", "original")
    saved = store.commit(initial, expected_revision=None)
    alice = RuntimeContext("account", "alice", "session-a")
    ContextService(store, alice).read(uri)
    store.commit(
        ContextObject(
            object_id=saved.object_id, uri=uri, kind=saved.kind, owner_id="bob",
            content="rebound", revision=saved.revision, status="active",
        ),
        expected_revision=saved.revision,
    )

    assert ContextService(store, alice).list_access_stats() == ()
