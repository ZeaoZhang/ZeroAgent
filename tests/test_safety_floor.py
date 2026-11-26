"""不可逆破坏底线：系统目录/凭据/用户数据/.git 与沙箱缺失时的破坏性 shell."""

from __future__ import annotations

from pathlib import Path

import pytest

from zero_agent.core import safety
from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.types import StepOutcome
from zero_agent.tools.builtin import code as code_tools
from zero_agent.tools.builtin import file as file_tools
from zero_agent.tools.registry import ToolRegistry


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
        memory_dir=str(tmp_path / "mem"),
        sessions_dir=str(tmp_path / "sessions"),
        extensions_home=str(tmp_path / "home"),
    )
    base.update(overrides)
    config = AgentConfig(**base)
    for path in (config.workspace_dir, config.memory_dir):
        Path(path).mkdir(parents=True, exist_ok=True)
    return config


def _run(definition, args):
    gen = definition.handler(args, None, None)
    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as stop:
        return "".join(chunks), stop.value


def _registry(config: AgentConfig) -> ToolRegistry:
    registry = ToolRegistry()
    file_tools.register_file_tools(registry, config)
    code_tools.register_code_tools(registry, config)
    return registry


def test_write_denial_blocks_system_secrets_and_user_data(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for target in (
        "/usr/local/bin/tool",
        "/etc/hosts",
        "/System/Library/x",
        "/opt/homebrew/bin/brew",
        str(Path.home() / ".ssh" / "id_ed25519"),
        str(Path.home() / ".aws" / "credentials"),
        str(Path.home() / "Library" / "Keychains" / "login.keychain-db"),
        str(Path.home() / "Documents" / "contract.pdf"),
        str(Path.home() / "Desktop" / "notes.md"),
        str(Path.home() / "Downloads" / "invoice.pdf"),
    ):
        assert safety.write_denial(target, config), target


def test_write_denial_blocks_git_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path)

    denial = safety.write_denial(str(tmp_path / "repo" / ".git" / "hooks" / "pre-commit"), config)

    assert "git 元数据" in denial


def test_write_denial_allows_self_upgrade_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for target in (
        str(tmp_path / "ws" / "scripts" / "a.py"),
        str(tmp_path / "mem" / "MEMORY.md"),
        str(Path(__file__).resolve().parents[1] / "zero_agent" / "core" / "loop.py"),
        "/tmp/za_scratch.txt",
    ):
        assert safety.write_denial(target, config) == "", target


def test_allow_paths_unprotects_and_extra_paths_protect(tmp_path: Path) -> None:
    downloads = Path.home() / "Downloads"
    config = _config(
        tmp_path,
        safety_allow_paths=[str(downloads)],
        safety_protected_paths=[str(tmp_path / "keep")],
    )

    assert safety.write_denial(str(downloads / "report.pdf"), config) == ""
    assert safety.write_denial(str(Path.home() / "Documents" / "x"), config)
    assert safety.write_denial(str(tmp_path / "keep" / "db.sqlite"), config)


def test_file_write_refuses_protected_path_without_touching_disk(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = _registry(config)
    target = Path.home() / "Documents" / "za_floor_probe.md"

    text, outcome = _run(registry.get("file_write"), {"path": str(target), "content": "x"})

    assert isinstance(outcome, StepOutcome)
    assert outcome.data["status"] == "error"
    assert "受保护路径" in outcome.data["msg"]
    assert not target.exists()


def test_file_write_still_writes_inside_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = _registry(config)
    target = tmp_path / "ws" / "notes" / "a.md"

    _text, outcome = _run(registry.get("file_write"), {"path": str(target), "content": "hi"})

    assert (outcome or {}).get("status") == "success"
    assert target.read_text() == "hi"


def test_file_patch_refuses_protected_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = _registry(config)

    _text, outcome = _run(
        registry.get("file_patch"),
        {
            "path": str(Path.home() / ".ssh" / "authorized_keys"),
            "old_content": "a",
            "new_content": "b",
        },
    )

    assert isinstance(outcome, StepOutcome)
    assert "受保护路径" in outcome.data["msg"]


def test_code_run_without_sandbox_refuses_destructive_shell(tmp_path: Path) -> None:
    config = _config(tmp_path, sandbox_mode="off")
    registry = _registry(config)

    for script in ("rm -rf /", "mkfs.ext4 /dev/disk0", "rm -rf ~/Documents"):
        text, outcome = _run(registry.get("code_run"), {"script": script, "type": "bash"})
        assert isinstance(outcome, StepOutcome), script
        assert outcome.data["status"] == "error"
        assert "已拒绝执行" in text


def test_code_run_without_sandbox_still_runs_benign_commands(tmp_path: Path) -> None:
    config = _config(tmp_path, sandbox_mode="off")
    registry = _registry(config)

    text, outcome = _run(registry.get("code_run"), {"script": "echo floor-ok", "type": "bash"})

    assert (outcome or {}).get("status") == "success"
    assert "floor-ok" in text


def test_code_run_script_path_is_analyzed_without_sandbox(tmp_path: Path) -> None:
    config = _config(tmp_path, sandbox_mode="off")
    registry = _registry(config)
    script = tmp_path / "ws" / "scripts" / "danger.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("rm -rf ~/Documents\n", encoding="utf-8")

    _text, outcome = _run(
        registry.get("code_run"),
        {"type": "bash", "script_path": str(script)},
    )

    assert isinstance(outcome, StepOutcome)
    assert outcome.data["status"] == "error"


def test_sandbox_active_skips_static_floor(tmp_path: Path) -> None:
    from zero_agent.extensions.sandbox import will_apply

    config = _config(tmp_path, sandbox_mode="auto")
    if not will_apply(str(config.workspace_dir), config.sandbox_mode):
        pytest.skip("本机沙箱不可用：走的是静态兜底分支")

    registry = _registry(config)
    # 沙箱生效时命令不被静态判定限制（机器保护由内核策略提供）
    text, outcome = _run(
        registry.get("code_run"),
        {"script": "rm -rf ./build-cache 2>/dev/null; echo sandbox-ok", "type": "bash"},
    )

    assert (outcome or {}).get("status") == "success"
    assert "sandbox-ok" in text
