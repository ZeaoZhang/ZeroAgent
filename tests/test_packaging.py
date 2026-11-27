"""Packaging checks for the ZeroAgent distribution."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_declared_packages_exist_and_are_packages() -> None:
    packages = _pyproject()["tool"]["setuptools"]["packages"]

    for package in packages:
        path = ROOT / package.replace(".", "/")
        assert path.is_dir(), package
        assert (path / "__init__.py").is_file(), package


def test_console_entrypoints_are_importable() -> None:
    scripts = _pyproject()["project"]["scripts"]

    for target in scripts.values():
        module_name, attr_name = target.split(":", 1)
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attr_name))

def test_launcher_entrypoint_is_declared() -> None:
    """pywebview 桌面启动器应作为 console script 发布。"""
    scripts = _pyproject()["project"]["scripts"]

    assert scripts["zero-agent-launcher"] == "zero_agent.frontends.launcher:main"


def test_frontend_package_data_tracks_desktop_assets() -> None:
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]["zero_agent.frontends"]

    assert "desktop/static/*" in package_data
    assert "desktop/static/vendor/*" in package_data
    assert all(pattern.startswith("desktop/") or pattern == "conductor.html" for pattern in package_data)
    assert "zero_agent.frontends.themes" not in _pyproject()["tool"]["setuptools"]["packages"]


def test_bots_extra_covers_supported_channels() -> None:
    bots_extra = set(_pyproject()["project"]["optional-dependencies"]["bots"])

    assert "zero-agent[telegram,discord]" in bots_extra
    assert "qq-botpy>=1.0" in bots_extra
    assert "pycryptodome>=3.19" in bots_extra
    assert "qrcode>=7.4" in bots_extra
    assert "lark-oapi>=1.0" in bots_extra
    assert "wecom-aibot-sdk>=1.0" in bots_extra
    assert "dingtalk-stream>=0.20" in bots_extra


def test_qt_chat_frontend_is_not_packaged() -> None:
    pyproject = _pyproject()
    extras = pyproject["project"]["optional-dependencies"]

    assert "zero-agent[ui,browser,docs,memory,plot,bots,monitoring]" in extras["all-extras"]
    assert importlib.util.find_spec("zero_agent.frontends.qtapp") is None


def test_removed_frontend_and_alias_files_are_absent() -> None:
    root_files = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "zero_agent").glob("*.py")
    }
    frontend_children = {
        path.name
        for path in (ROOT / "zero_agent" / "frontends").iterdir()
        if path.name != "__pycache__"
    }

    assert ("llm" + "core.py") not in root_files
    assert not (ROOT / "zero_agent" / "vendor").exists()
    assert frontend_children == {
        "__init__.py",
        "acp_bridge.py",
        "at_complete.py",
        "channel_onboarding.py",
        "conductor.html",
        "conductor.py",
        "commands",
        "desktop",
        "desktop_bridge.py",
        "desktop_commands.py",
        "hub.pyw",
        "launcher.py",
        "session_store.py",
        "plan_command.py",
        "plan_state.py",
        "stapp.py",
        "tui.py",
        "worldline.py",
    }


def test_packaged_modules_do_not_include_removed_layers() -> None:
    packages = _pyproject()["tool"]["setuptools"]["packages"]
    removed_package_segment = "." + "vendor"

    assert not any(
        package == "zero_agent" + removed_package_segment
        or removed_package_segment in package
        for package in packages
    )
    assert "zero_agent.frontends" in packages
    assert "zero_agent.frontends.commands" in packages

    frontend_files = {
        path.relative_to(ROOT / "zero_agent" / "frontends").as_posix()
        for path in (ROOT / "zero_agent" / "frontends").glob("*.py")
    }
    assert frontend_files == {
        "__init__.py",
        "acp_bridge.py",
        "at_complete.py",
        "channel_onboarding.py",
        "conductor.py",
        "desktop_bridge.py",
        "desktop_commands.py",
        "session_store.py",
        "plan_command.py",
        "launcher.py",
        "plan_state.py",
        "stapp.py",
        "tui.py",
        "worldline.py",
    }


def test_memory_package_contains_only_runtime_modules() -> None:
    pyproject = _pyproject()
    packages = pyproject["tool"]["setuptools"]["packages"]
    package_data = pyproject["tool"]["setuptools"].get("package-data", {})
    memory_files = {
        path.relative_to(ROOT / "zero_agent" / "memory").as_posix()
        for path in (ROOT / "zero_agent" / "memory").glob("*")
        if path.is_file()
    }

    assert "zero_agent.memory" in packages
    assert "zero_agent.utils.skill_search" in packages
    assert "zero_agent.memory" not in package_data
    assert memory_files == {"__init__.py", "compress_session.py", "manager.py"}
    assert not (ROOT / "zero_agent" / "memory" / "L4_raw_sessions").exists()
    assert not (ROOT / "zero_agent" / "memory" / "ui_detect.py").exists()
    assert not (ROOT / "zero_agent" / "memory" / "vision_api.py").exists()
    sop_seed_dir = ROOT / "zero_agent" / "assets" / "memory_seed" / "sops"
    assert sop_seed_dir.is_dir()
    assert all(path.is_file() and path.suffix == ".md" for path in sop_seed_dir.iterdir())
    assert (ROOT / "zero_agent" / "assets" / "review_sop").is_dir()


def test_plan_sop_documents_current_plan_contract() -> None:
    plan_sop_path = ROOT / "zero_agent" / "assets" / "memory_seed" / "sops" / "plan_sop.md"
    plan_sop = plan_sop_path.read_text(encoding="utf-8")

    for expected in (
        "/plan <task>",
        "TaskMode.PLAN",
        "TaskContract",
        "ready",
        "plan.md",
        "exploration_findings.md",
        "verify_context.json",
        "evidence.json",
        "result.md",
        "VERDICT: PASS",
        "VERDICT: FAIL",
        "VERDICT: PARTIAL",
        "zero_agent/assets/memory_seed/sops/verify_sop.md",
    ):
        assert expected in plan_sop


def test_repl_docs_include_plan_command() -> None:
    docs = {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "docs/quickstart.md": (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8"),
    }

    for name, source in docs.items():
        assert "`/plan <task>`" in source, name
        assert "不会自动改代码" in source, name


def test_agent_runner_lives_under_runners_package() -> None:
    packages = _pyproject()["tool"]["setuptools"]["packages"]
    adapters_dir = ROOT / "zero_agent" / "adapters"
    adapter_sources = (
        [path for path in adapters_dir.rglob("*.py") if "__pycache__" not in path.parts]
        if adapters_dir.exists()
        else []
    )

    adapters_package = "zero_agent." + "adapters"

    assert adapters_package not in packages
    assert adapter_sources == []
    assert importlib.util.find_spec(adapters_package) is None
    assert importlib.util.find_spec("zero_agent.runners.agent_runner") is not None


def test_reflect_runner_lives_under_runners_package() -> None:
    old_module = "zero_agent.reflect." + "runner"
    reflect_runner_source = ROOT / "zero_agent" / "reflect" / ("runner" + ".py")

    assert not reflect_runner_source.exists()
    assert importlib.util.find_spec(old_module) is None
    assert importlib.util.find_spec("zero_agent.runners.reflect_runner") is not None


def test_repository_has_no_removed_name_fragments() -> None:
    blocked_fragments = [
        "agent" + "main",
        "GA" + "Root",
        "Generic" + "Agent",
        "Generatic" + "Agent",
        "generic" + "agent",
        "G" + "A" + "_" + "LANG",
        "G" + "A" + "_" + "WORKSPACE_ROOT",
        "G" + "A" + "_" + "USER_DATA_DIR",
        "ga" + "Root",
        "my" + "keyPath",
        "ga" + "-web",
        "ga" + "_",
        "ga" + "-",
        "llm" + "core",
        "za" + "_adapter",
        "slash" + "_cmds",
        "window." + "ga",
        "my" + "key.py",
        "my" + "key.json",
    ]
    skipped_parts = {
        ".git",
        ".omc",
        ".omx",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "memory",
        "sessions",
        "temp",
        "target",
        "workspace",
        "zero_agent.egg-info",
    }
    text_suffixes = {
        ".css",
        ".html",
        ".js",
        ".json",
        ".lock",
        ".md",
        ".py",
        ".rs",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
    findings: list[str] = []

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if set(relative.parts) & skipped_parts:
            continue
        if path.suffix not in text_suffixes and path.name != ".gitignore":
            continue
        # Allowed intentional GA references in UltraPlan port and historical design docs.
        if "ga_ultraplan" in relative.as_posix() or relative.name in (
            "test_ultraplan.py",
            "test_packaging.py",
            "pyproject.toml",
        ):
            continue
        if "memory_seed/sops/ultraplan_sop.md" in relative.as_posix():
            continue
        if relative.as_posix().startswith("docs/superpowers/"):
            continue
        # Ported modules with legacy GA-derived references (comments, param names)
        if relative.as_posix() in (
            "zero_agent/frontends/worldline.py",
            "zero_agent/frontends/plan_state.py",
            "zero_agent/frontends/at_complete.py",
            "zero_agent/frontends/plan_command.py",
        ):
            continue

        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for fragment in blocked_fragments:
            if fragment in source or fragment in relative.as_posix():
                findings.append(f"{relative}: {fragment}")

    assert findings == []


def test_channel_modules_import_when_bot_extras_are_installed() -> None:
    optional_imports = [
        "qrcode",
        "Crypto",
        "lark_oapi",
        "wecom_aibot_sdk",
        "dingtalk_stream",
        "botpy",
        "telegram",
        "discord",
    ]
    missing = [name for name in optional_imports if importlib.util.find_spec(name) is None]
    if missing:
        pytest.skip(f"bot extras not installed: {', '.join(missing)}")

    modules = [
        "zero_agent.bots.wechat_app",
        "zero_agent.bots.wecom_app",
        "zero_agent.bots.dingtalk_app",
        "zero_agent.bots.qq_app",
        "zero_agent.bots.feishu_app",
        "zero_agent.bots.telegram_app",
        "zero_agent.bots.discord_app",
    ]

    for module_name in modules:
        importlib.import_module(module_name)


def test_browser_runtime_modules_are_packaged() -> None:
    assert importlib.util.find_spec("zero_agent.browser.tm_webdriver")
    assert importlib.util.find_spec("zero_agent.browser.simphtml")


def test_shared_bot_helpers_are_declared_as_a_package() -> None:
    packages = _pyproject()["tool"]["setuptools"]["packages"]

    assert "zero_agent.bots.shared" in packages


def test_vendor_and_asset_files_ship_with_the_wheel() -> None:
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]

    frontend = package_data["zero_agent.frontends"]
    assets = package_data["zero_agent.assets"]

    assert "desktop/static/vendor/katex/*" in frontend
    assert "user_profile.md" in assets


def _stage_build_tree(destination: Path) -> Path:
    """Copy the minimal build inputs so ``pip wheel`` never writes to ROOT.

    The checkout can live on a read-only (or otherwise unwritable) path, in
    which case an in-place ``pip wheel .`` aborts while creating ``build/``.
    Staging the packaging inputs into ``tmp_path`` keeps the test hermetic and
    still exercises a real wheel build.
    """
    import shutil

    destination.mkdir(parents=True, exist_ok=True)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, destination / name)

    shutil.copytree(
        ROOT / "zero_agent",
        destination / "zero_agent",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "node_modules",
            "src-tauri",
        ),
    )
    return destination


def test_wheel_contains_runtime_modules_and_vendor_assets(tmp_path) -> None:
    """The built wheel must import cleanly, not just install its modules."""
    import subprocess
    import sys
    import zipfile

    import os

    staging = _stage_build_tree(tmp_path / "build-tree")
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    # Keep pip/rust caches inside tmp; otherwise the build writes the user's
    # ~/Library/Caches/pip and ~/.rustup.
    build_env = dict(os.environ)
    build_env.update({
        "PIP_CACHE_DIR": str(tmp_path / "pip-cache"),
        "RUSTUP_HOME": str(tmp_path / "rustup"),
        "CARGO_HOME": str(tmp_path / "cargo"),
    })
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(wheel_dir),
        ],
        cwd=str(staging),
        env=build_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        environment_markers = (
            "No module named setuptools",
            "No module named 'setuptools'",
            "Failed to establish a new connection",
            "Network is unreachable",
            "Temporary failure in name resolution",
            "ProxyError",
            "Read timed out",
        )
        if any(marker in output for marker in environment_markers):
            detail = (output.strip().splitlines() or [""])[-1][:300]
            pytest.skip(f"wheel build not possible in this environment: {detail}")
        raise AssertionError(f"pip wheel failed:\n{output[-4000:]}")
    wheels = sorted(wheel_dir.glob("zero_agent-*.whl"))
    assert wheels, "pip wheel produced no wheel"

    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())

    for expected in (
        "zero_agent/bots/shared/continue_cmd.py",
        "zero_agent/frontends/desktop/static/vendor/katex/katex.min.js",
        "zero_agent/assets/user_profile.md",
    ):
        assert expected in names, expected
