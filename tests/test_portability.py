"""可移植性护栏：源码/配置/文档里不得出现机器特定的家目录路径.

换用户、换机器（CI、容器、另一台 Mac/Linux）都必须能直接跑：所有路径在运行时
从 config / ``~`` / 包位置推导，提示词里也只出现 ``~/...`` 形式。
"""

from __future__ import annotations

import re
from pathlib import Path

from zero_agent.core import safety, self_prompt
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.tools.builtin import file as file_tools
from zero_agent.tools.registry import ToolRegistry

_REPO = Path(__file__).resolve().parents[1]
# ``/Users/<name>`` 与 ``/home/<name>`` 是家目录字面量的两种写法；
# 前面紧贴单词字符的是 "enabled/home/browser" 这类组合词，不算路径
_HOME_LITERAL = re.compile(r"(?<![\w])/(?:Users|home)/[A-Za-z0-9._-]+")
# 测试夹具里允许的示例用户名（不是真实机器路径）
_PLACEHOLDER_USERS = frozenset({"alice", "user", "someone", "example"})


def _offenders(paths) -> list[str]:
    found: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            match = _HOME_LITERAL.search(line)
            if match and match.group(0).split("/")[-1] not in _PLACEHOLDER_USERS:
                found.append(f"{path.relative_to(_REPO)}:{lineno}: {match.group(0)}")
    return found


def test_package_has_no_machine_specific_home_paths() -> None:
    paths = [
        path
        for path in (_REPO / "zero_agent").rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    assert _offenders(paths) == []


def test_config_example_and_docs_have_no_machine_specific_paths() -> None:
    paths = [_REPO / "config.example.yaml", *_REPO.joinpath("docs").rglob("*.md")]
    assert _offenders(paths) == []


def test_python_and_js_tests_have_no_machine_specific_paths() -> None:
    paths = [
        *(_REPO / "tests").glob("*.py"),
        *(_REPO / "tests").glob("*.js"),
    ]
    assert _offenders(paths) == []


def _config(tmp_path: Path, **overrides) -> AgentConfig:
    base = dict(
        llm_backends={
            "d": LLMBackendConfig(
                name="d",
                provider="openai",
                api_key="k",
                api_base="https://api.openai.com/v1",
                model="gpt-4o",
            )
        },
        default_backend="d",
        workspace_dir=str(tmp_path / "ws"),
        memory_dir="./memory",
        sessions_dir=str(tmp_path / "sessions"),
    )
    base.update(overrides)
    return AgentConfig(**base)


def test_prompt_uses_tilde_instead_of_absolute_home(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)
    config = _config(tmp_path, memory_dir="./memory")

    text = self_prompt.build(config, "zh")

    assert "~/.zeroagent/skills" in text
    assert "~/.zeroagent/extensions" in text
    assert "~/.zeroagent/mcp.json" in text
    assert str(tmp_path) not in text


def test_safety_roots_follow_the_current_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)

    assert safety.write_denial(str(tmp_path / "Documents" / "x"))
    assert safety.write_denial(str(tmp_path / ".ssh" / "id_ed25519"))
    assert safety.write_denial(str(tmp_path / "projects" / "x")) == ""


def test_file_tools_expand_tilde(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)
    config = _config(tmp_path)
    registry = ToolRegistry()
    file_tools.register_file_tools(registry, config)

    gen = registry.get("file_write").handler(
        {"path": "~/notes/a.md", "content": "hi"}, None, None,
    )
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        outcome = stop.value

    assert (outcome or {}).get("status") == "success"
    assert (tmp_path / "notes" / "a.md").read_text() == "hi"
    assert file_tools.resolve_file_path("~/notes/a.md", config) == str(
        tmp_path / "notes" / "a.md",
    )
