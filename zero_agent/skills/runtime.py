"""Pinned built-in skill loading and permission-gated execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Generator

from zero_agent.context.errors import ContextError
from zero_agent.context.service import ContextService
from zero_agent.context.types import RuntimeContext
from zero_agent.skills.manifest import BuiltinSkill, load_builtin_catalog


@dataclass(frozen=True)
class SkillInvocation:
    skill_uri: str
    revision: str
    entrypoint: str
    arguments: dict[str, Any]
    timeout_seconds: int = 60


class SkillRuntime:
    """Load exact package revisions; run only explicitly authorized entrypoints."""

    def __init__(
        self,
        context_root: str | Path,
        service: ContextService,
        runtime_context: RuntimeContext,
    ) -> None:
        self.context_root = Path(context_root).expanduser().resolve()
        self.service = service
        self.runtime_context = runtime_context
        self.catalog = {skill.uri: skill for skill in load_builtin_catalog()}

    def run(
        self,
        invocation: SkillInvocation,
        *,
        handler: Any,
    ) -> Generator[str, None, dict[str, Any]]:
        try:
            skill = self._resolve(invocation)
            self.service.read(skill.uri)
            if invocation.entrypoint == "load":
                return {
                    "status": "loaded",
                    "skill_uri": skill.uri,
                    "skill_revision": skill.revision,
                    "category": skill.category,
                    "content": skill.content,
                    "dependency_info": [],
                    "exit_code": None,
                    "artifacts": [],
                }
            if skill.entrypoint is None:
                raise ContextError("INVALID_SOURCE", "This reference skill can only be loaded.")
            self._check_permissions(skill)
            self._validate_arguments(skill, invocation)
            dependency = self._read_entrypoint_dependency(skill)
            task_dir = getattr(handler, "task_dir", None)
            task_cwd = Path(task_dir).expanduser().resolve() if task_dir else Path(handler.cwd).expanduser().resolve()
            if not task_cwd.is_dir():
                raise ContextError("INVALID_SOURCE", "The current task directory is unavailable.")
            yield "Preparing the pinned skill revision.\n"
            with self._prepared_session_dir(skill) as runtime_dir_string:
                runtime_dir = Path(runtime_dir_string)
                _write_private(runtime_dir / "SKILL.md", skill.content.encode("utf-8"))
                _write_private(runtime_dir / "engine.py", dependency)
                code = self._build_search_program(invocation.arguments)
                from zero_agent.tools.builtin.code import code_run

                executor = code_run(
                    code=code,
                    code_type="python",
                    timeout=invocation.timeout_seconds,
                    cwd=str(task_cwd),
                    code_cwd=str(runtime_dir),
                    stop_signal=getattr(handler, "code_stop_signal", None),
                    maxlen=10000,
                    env=self._execution_environment(runtime_dir),
                    isolated_process=True,
                )
                while True:
                    try:
                        progress = next(executor)
                    except StopIteration as stop:
                        result = stop.value
                        break
                    if isinstance(progress, str):
                        yield _sanitize(progress, self.context_root, runtime_dir)
                if not isinstance(result, dict):
                    return self._error_result(skill, "The skill executor returned an invalid result.")
                stdout = _sanitize(str(result.get("stdout", "")), self.context_root, runtime_dir)
                exit_code = result.get("exit_code")
                status = "success" if result.get("status") == "success" and exit_code == 0 else "error"
                return {
                    "status": status,
                    "skill_uri": skill.uri,
                    "skill_revision": skill.revision,
                    "entrypoint": invocation.entrypoint,
                    "dependency_info": [{"name": "zero_agent.utils.skill_search", "available": True}],
                    "exit_code": exit_code,
                    "output": stdout,
                    "artifacts": [],
                }
        except ContextError as exc:
            return {
                "status": "error",
                "code": exc.code,
                "message": str(exc),
            }
        except Exception:
            return {
                "status": "error",
                "code": "INVALID_SOURCE",
                "message": "The skill could not be prepared or executed.",
            }

    def _resolve(self, invocation: SkillInvocation) -> BuiltinSkill:
        if not isinstance(invocation, SkillInvocation):
            raise ContextError("INVALID_URI", "A structured skill invocation is required.")
        skill = self.catalog.get(invocation.skill_uri)
        if skill is None:
            raise ContextError("NOT_FOUND", "The requested built-in skill is not installed.")
        if invocation.revision != skill.revision:
            raise ContextError("REVISION_CONFLICT", "The requested skill revision is not available.")
        if not isinstance(invocation.entrypoint, str):
            raise ContextError("INVALID_SOURCE", "The skill entrypoint is invalid.")
        if invocation.entrypoint != "load" and (
            skill.entrypoint is None or invocation.entrypoint != skill.entrypoint.get("id")
        ):
            raise ContextError("INVALID_SOURCE", "The requested entrypoint is not declared by this skill.")
        return skill

    def _check_permissions(self, skill: BuiltinSkill) -> None:
        assert skill.entrypoint is not None
        for permission in skill.entrypoint["permissions"]:
            capability = f"skill:{skill.skill_id}:{permission}"
            if capability not in self.runtime_context.capabilities:
                raise ContextError("PERMISSION_DENIED", "The skill requires an entrypoint-granted capability.")

    @staticmethod
    def _validate_arguments(skill: BuiltinSkill, invocation: SkillInvocation) -> None:
        if skill.skill_id != "skill_search" or invocation.entrypoint != "search":
            raise ContextError("INVALID_SOURCE", "The requested skill entrypoint is not declared.")
        schema = skill.entrypoint["parameters"] if skill.entrypoint else {}
        args = invocation.arguments
        if not isinstance(args, dict) or set(args) - set(schema):
            raise ContextError("INVALID_SOURCE", "Skill arguments do not match the declared schema.")
        query = args.get("query")
        category = args.get("category")
        top_k = args.get("top_k", 10)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ContextError("INVALID_SOURCE", "The skill query must be a non-empty string up to 500 characters.")
        if category is not None and (not isinstance(category, str) or len(category) > 80):
            raise ContextError("INVALID_SOURCE", "The skill category is invalid.")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise ContextError("INVALID_SOURCE", "top_k must be between 1 and 20.")
        if isinstance(invocation.timeout_seconds, bool) or not isinstance(invocation.timeout_seconds, int) or not 1 <= invocation.timeout_seconds <= 300:
            raise ContextError("INVALID_SOURCE", "The skill timeout must be between 1 and 300 seconds.")

    @staticmethod
    def _read_entrypoint_dependency(skill: BuiltinSkill) -> bytes:
        assert skill.entrypoint is not None
        try:
            module = resources.files(skill.entrypoint["module"])
            dependency = module.joinpath("engine.py")
            path = Path(os.fspath(dependency))
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ContextError("INTEGRITY_ERROR", "The skill dependency is not a regular file.")
            content = path.read_bytes()
        except ContextError:
            raise
        except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError) as exc:
            raise ContextError("DEPENDENCY_MISSING", "The pinned skill dependency is unavailable.") from exc
        if hashlib.sha256(content).hexdigest() != skill.entrypoint["dependency_sha256"]:
            raise ContextError("INTEGRITY_ERROR", "A pinned skill dependency hash does not match.")
        return content

    @staticmethod
    def _build_search_program(arguments: dict[str, Any]) -> str:
        encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        literal = repr(encoded)
        return (
            "import importlib.util, json, os, sys\n"
            "os.chdir(os.path.dirname(os.path.abspath(__file__)))\n"
            "spec = importlib.util.spec_from_file_location('skill_engine_snapshot', 'engine.py')\n"
            "if spec is None or spec.loader is None: raise ImportError('engine snapshot unavailable')\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules[spec.name] = module\n"
            "spec.loader.exec_module(module)\n"
            f"args = json.loads({literal})\n"
            "results = module.search(args['query'], category=args.get('category'), "
            "top_k=args.get('top_k', 10))\n"
            "from dataclasses import asdict\n"
            "print(json.dumps([asdict(item) for item in results], ensure_ascii=False))\n"
        )

    def _execution_environment(self, runtime_dir: Path) -> dict[str, str]:
        environment = os.environ.copy()
        # The engine is loaded from the verified bytes written to runtime_dir.
        # Do not expose the host interpreter's import roots or user site there:
        # a mutable workspace/global module must never replace the snapshot.
        environment["PYTHONPATH"] = str(runtime_dir)
        environment["PYTHONNOUSERSITE"] = "1"
        return environment

    def _prepared_session_dir(self, skill: BuiltinSkill):
        session_hash = hashlib.sha256(self.runtime_context.session_id.encode("utf-8")).hexdigest()[:24]
        session_root = self.context_root / "runtime" / session_hash
        _ensure_private_directory(self.context_root, session_root)
        return tempfile.TemporaryDirectory(prefix=f"{skill.skill_id}-", dir=session_root)

    def _error_result(self, skill: BuiltinSkill, message: str) -> dict[str, Any]:
        return {
            "status": "error",
            "code": "BACKEND_UNAVAILABLE",
            "message": message,
            "skill_uri": skill.uri,
            "skill_revision": skill.revision,
        }


def _ensure_private_directory(root: Path, target: Path) -> None:
    cursor = target
    while cursor != root:
        if cursor.is_symlink():
            raise ContextError("INTEGRITY_ERROR", "The skill runtime directory contains a symlink.")
        cursor = cursor.parent
        if cursor == cursor.parent:
            raise ContextError("INTEGRITY_ERROR", "The skill runtime directory escaped context storage.")
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ContextError("INTEGRITY_ERROR", "The skill runtime directory escaped context storage.") from exc
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink() or not target.is_dir():
        raise ContextError("INTEGRITY_ERROR", "The skill runtime directory is not a regular directory.")
    if os.name != "nt":
        os.chmod(target, 0o700)


def _write_private(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ContextError("INTEGRITY_ERROR", "A prepared skill resource already exists.")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _sanitize(text: str, context_root: Path, runtime_dir: str | Path) -> str:
    for value in (str(context_root), str(runtime_dir)):
        if value:
            text = text.replace(value, "[private skill directory]")
    return re.sub(r"(?m)^\s*File \"[^\"]*\", line (\d+)", r"File \"[skill module]\", line \1", text)
