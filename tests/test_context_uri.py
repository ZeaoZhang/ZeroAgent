"""Logical URI normalization and traversal protection."""

import pytest

from zero_agent.context.errors import ContextError
from zero_agent.context.types import RuntimeContext
from zero_agent.context.uri import parse_context_uri


@pytest.fixture
def runtime() -> RuntimeContext:
    return RuntimeContext(
        account_id="local",
        user_id="alice",
        session_id="session-1",
        project_id="project-1",
    )


def test_me_expands_only_from_trusted_runtime_identity(runtime: RuntimeContext) -> None:
    parsed = parse_context_uri(
        "za://users/me/memories/preferences/reply-style",
        runtime,
    )

    assert parsed.canonical == "za://users/alice/memories/preferences/reply-style"


def test_uri_is_canonicalized_and_unicode_is_preserved(runtime: RuntimeContext) -> None:
    parsed = parse_context_uri(
        "ZA://USERS/alice/memories/facts/%E4%B8%AD%E6%96%87",
        runtime,
    )

    assert parsed.canonical == "za://users/alice/memories/facts/%E4%B8%AD%E6%96%87"


@pytest.mark.parametrize(
    "uri",
    [
        "za://users/alice/memories/facts/..",
        "za://users/alice/memories/facts/%2e%2e",
        "za://users/alice/memories/facts/%2Fetc",
        "za://users/alice/memories/facts/a%5Cb",
        "za://users/alice/memories/facts/%00",
        "za://users:invalid/alice/memories/facts/x",
        "za://users/alice/memories/facts/%ZZ",
        "file:///users/alice/memories/facts/x",
        "za://users/alice/memories/facts/x?other=1",
    ],
)
def test_rejects_unsafe_or_unsupported_uri(uri: str, runtime: RuntimeContext) -> None:
    with pytest.raises(ContextError) as error:
        parse_context_uri(uri, runtime)

    assert error.value.code == "INVALID_URI"

def test_context_errors_remain_catchable_as_zero_agent_errors(runtime: RuntimeContext) -> None:
    from zero_agent.core.exceptions import ZeroAgentError

    with pytest.raises(ZeroAgentError):
        parse_context_uri("file:///invalid", runtime)



def test_allows_scoped_collection_addresses(runtime: RuntimeContext) -> None:
    assert parse_context_uri("za://projects/project-1/memories", runtime).canonical == (
        "za://projects/project-1/memories"
    )
