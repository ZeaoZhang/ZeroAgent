from __future__ import annotations

import json
import time
import threading
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from zero_agent.context.errors import ContextError
from zero_agent.context.local_store import LocalContextStore
from zero_agent.context.service import ContextService
from zero_agent.context.types import RuntimeContext
from zero_agent.core.handler import BaseHandler
from zero_agent.skills.runtime import SkillInvocation, SkillRuntime
from zero_agent.skills.manifest import builtin_context_objects, load_builtin_catalog
from zero_agent.skills import runtime as skill_runtime_module
from zero_agent.tools.builtin.code import code_run


def _runtime(root: Path, context: RuntimeContext | None = None) -> SkillRuntime:
    store = LocalContextStore(root)
    runtime_context = context or RuntimeContext("local", "alice", "session-1")
    service = ContextService(
        store,
        runtime_context,
        receipt_store=store,
        builtin_objects=builtin_context_objects(),
    )
    return SkillRuntime(root, service, runtime_context)


def _run(generator):
    while True:
        try:
            next(generator)
        except StopIteration as stop:
            return stop.value


def test_catalog_covers_every_packaged_sop_and_verifies_source_hashes() -> None:
    skills = load_builtin_catalog()

    assert len(skills) == 29
    assert {skill.source_path.rsplit("/", 1)[-1] for skill in skills} == {
        path.name for path in Path("zero_agent/assets/memory_seed/sops").glob("*.md")
    }
    assert next(skill for skill in skills if skill.skill_id == "code_review_principles").category == "triggered_rule"
    search = next(skill for skill in skills if skill.skill_id == "skill_search")
    assert search.category == "on_demand_skill"
    assert search.entrypoint["module"] == "zero_agent.utils.skill_search"


def test_builtin_skill_is_navigable_and_loaded_by_exact_revision(tmp_path) -> None:
    runtime = _runtime(tmp_path / "context")
    skill = runtime.catalog["za://skills/builtin/code_review_principles"]
    visible = runtime.service.list("za://skills/builtin")["items"]
    assert skill.uri in {item.uri for item in visible}

    result = _run(runtime.run(
        SkillInvocation(skill.uri, skill.revision, "load", {}),
        handler=SimpleNamespace(cwd=str(tmp_path), code_stop_signal=[]),
    ))

    assert result["status"] == "loaded"
    assert result["content"] == skill.content
    assert result["skill_revision"] == skill.revision
    assert BaseHandler._evidence_status("skill_run", result, evidence_kind="execute") == "unknown"
    with pytest.raises(ContextError) as missing_revision:
        runtime.service.read(skill.uri, revision=2)
    assert missing_revision.value.code == "NOT_FOUND"


def test_executable_skill_can_be_loaded_without_running_its_entrypoint(tmp_path) -> None:
    runtime = _runtime(tmp_path / "context")
    skill = runtime.catalog["za://skills/builtin/skill_search"]
    loaded = _run(runtime.run(
        SkillInvocation(skill.uri, skill.revision, "load", {}),
        handler=SimpleNamespace(cwd=str(tmp_path), code_stop_signal=[]),
    ))

    assert loaded["status"] == "loaded"
    assert loaded["content"] == skill.content
    assert loaded["skill_revision"] == skill.revision


def test_execution_requires_entrypoint_capabilities_and_exact_revision(tmp_path) -> None:
    runtime = _runtime(tmp_path / "context")
    skill = runtime.catalog["za://skills/builtin/skill_search"]
    handler = SimpleNamespace(cwd=str(tmp_path), code_stop_signal=[])

    denied = _run(runtime.run(
        SkillInvocation(skill.uri, skill.revision, "search", {"query": "pytest"}),
        handler=handler,
    ))
    assert denied["status"] == "error"
    assert denied["code"] == "PERMISSION_DENIED"
    wrong_revision = _run(runtime.run(
        SkillInvocation(skill.uri, "0" * 64, "search", {"query": "pytest"}),
        handler=handler,
    ))
    assert wrong_revision["code"] == "REVISION_CONFLICT"
    wrong_entrypoint = _run(runtime.run(
        SkillInvocation(skill.uri, skill.revision, "arbitrary", {"query": "pytest"}),
        handler=handler,
    ))
    assert wrong_entrypoint["code"] == "INVALID_SOURCE"


@pytest.mark.parametrize("workspace_name", ["workspace with spaces", "工作区 中文"])
def test_skill_search_runs_through_the_existing_executor_and_mocked_http(
    tmp_path, monkeypatch, workspace_name,
) -> None:
    received: list[dict] = []

    class SearchHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(size)))
            body = json.dumps({
                "results": [{
                    "skill": {
                        "key": "mock/pytest",
                        "name": "Pytest helper",
                        "one_line_summary": "A mocked result",
                    },
                    "final_score": 0.97,
                }],
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SearchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SKILL_SEARCH_API", f"http://127.0.0.1:{server.server_port}")
    workspace = tmp_path / workspace_name
    workspace.mkdir()
    runtime_context = RuntimeContext(
        "local",
        "alice",
        f"session-{workspace_name}",
        capabilities={
            "skill:skill_search:network",
            "skill:skill_search:environment:read",
        },
    )
    runtime = _runtime(tmp_path / f"context-{len(workspace_name)}", runtime_context)
    skill = runtime.catalog["za://skills/builtin/skill_search"]
    # This input resembles Python syntax but remains JSON data in the subprocess.
    query = "pytest'); __import__('os').system('false') #"
    handler = SimpleNamespace(cwd=str(workspace), task_dir=None, code_stop_signal=[])

    try:
        result = _run(runtime.run(
            SkillInvocation(
                skill.uri,
                skill.revision,
                "search",
                {"query": query, "top_k": 3},
                timeout_seconds=10,
            ),
            handler=handler,
        ))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["status"] == "success"
    assert result["exit_code"] == 0
    assert result["skill_revision"] == skill.revision
    assert "mocked result" in result["output"].casefold()
    assert received[0]["query"] == query
    assert not list((tmp_path / f"context-{len(workspace_name)}" / "runtime").rglob("SKILL.md"))


def test_skill_run_success_is_the_only_skill_execution_evidence() -> None:
    assert BaseHandler._evidence_status(
        "skill_run", {"status": "success", "exit_code": 0}, evidence_kind="execute"
    ) == "success"
    assert BaseHandler._evidence_status(
        "skill_run", {"status": "error", "exit_code": 0}, evidence_kind="execute"
    ) == "error"

def test_skill_dependency_hash_drift_fails_closed(monkeypatch, tmp_path) -> None:
    dependency = tmp_path / "engine.py"
    dependency.write_bytes(b"tampered")
    monkeypatch.setattr(
        skill_runtime_module.resources,
        "files",
        lambda _module: tmp_path,
    )
    skill = SimpleNamespace(
        entrypoint={
            "module": "fake.skill",
            "dependency_sha256": "0" * 64,
        }
    )
    with pytest.raises(ContextError) as exc_info:
        SkillRuntime._read_entrypoint_dependency(skill)
    assert exc_info.value.code == "INTEGRITY_ERROR"


def test_skill_dependency_snapshot_missing_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        skill_runtime_module.resources,
        "files",
        lambda _module: tmp_path,
    )
    skill = SimpleNamespace(
        entrypoint={
            "module": "fake.skill",
            "dependency_sha256": "0" * 64,
        }
    )
    with pytest.raises(ContextError) as exc_info:
        SkillRuntime._read_entrypoint_dependency(skill)
    assert exc_info.value.code == "DEPENDENCY_MISSING"


def test_skill_environment_uses_only_private_snapshot(tmp_path) -> None:
    runtime = _runtime(tmp_path / "context")
    snapshot = tmp_path / "snapshot"
    environment = runtime._execution_environment(snapshot)
    assert environment["PYTHONPATH"] == str(snapshot)
    assert environment["PYTHONNOUSERSITE"] == "1"


def test_isolated_skill_cancellation_does_not_share_process_group(tmp_path) -> None:
    first_stop = [False]
    second_stop = [False]
    first_ready = tmp_path / "one" / "ready"
    second_ready = tmp_path / "two" / "ready"
    code = "\n\nfrom pathlib import Path; import time; Path({!r}).write_text('started'); time.sleep(30)\n"
    first_code = code.format(str(first_ready))
    second_code = code.format(str(second_ready))
    first = code_run(code=first_code, timeout=30, code_cwd=str(first_ready.parent), stop_signal=first_stop, isolated_process=True)
    second = code_run(code=second_code, timeout=30, code_cwd=str(second_ready.parent), stop_signal=second_stop, isolated_process=True)
    results: list[dict] = []

    def consume(generator) -> None:
        results.append(_run(generator))

    first_ready.parent.mkdir(parents=True)
    second_ready.parent.mkdir(parents=True)
    first_thread = threading.Thread(target=consume, args=(first,))
    second_thread = threading.Thread(target=consume, args=(second,))
    first_thread.start()
    second_thread.start()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not (first_ready.exists() and second_ready.exists()):
        time.sleep(0.02)
    assert first_ready.is_file() and second_ready.is_file(), results

    first_stop[0] = True
    first_thread.join(timeout=8)
    assert not first_thread.is_alive()
    assert second_thread.is_alive()
    second_stop[0] = True
    second_thread.join(timeout=8)
    assert not second_thread.is_alive()
    assert all(result["status"] == "error" for result in results)


def test_isolated_cancellation_kills_descendant_after_parent_exit(tmp_path) -> None:
    stop = [False]
    child_pid = tmp_path / "child.pid"
    child = "import time; time.sleep(30)"
    program = (
        "import subprocess, sys; "
        f"p=subprocess.Popen([sys.executable, '-c', {child!r}]); "
        f"open({str(child_pid)!r}, 'w').write(str(p.pid))"
    )
    results: list[dict] = []
    generator = code_run(
        code=program, timeout=30, code_cwd=str(tmp_path),
        stop_signal=stop, isolated_process=True,
    )
    thread = threading.Thread(target=lambda: results.append(_run(generator)))
    thread.start()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not child_pid.exists():
        time.sleep(0.02)
    assert child_pid.is_file()
    child_pid_value = int(child_pid.read_text(encoding="utf-8"))
    time.sleep(0.1)

    stop[0] = True
    thread.join(timeout=8)

    assert not thread.is_alive()
    if os.name != "nt":
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid_value, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("isolated cancellation left the child process running")
