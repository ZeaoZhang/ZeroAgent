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


def test_frontend_package_data_tracks_desktop_assets() -> None:
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]["zero_agent.frontends"]

    assert "desktop/static/*" in package_data
    assert "desktop/static/vendor/*" in package_data
    assert all(pattern.startswith("desktop/") for pattern in package_data)
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

    assert "qt" not in extras
    assert "zero-agent[ui,browser,memory,plot,bots]" in extras["all-extras"]
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
        "desktop",
        "desktop_bridge.py",
        "desktop_commands.py",
        "launcher.py",
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

    frontend_files = {
        path.relative_to(ROOT / "zero_agent" / "frontends").as_posix()
        for path in (ROOT / "zero_agent" / "frontends").glob("*.py")
    }
    assert frontend_files == {
        "__init__.py",
        "desktop_bridge.py",
        "desktop_commands.py",
        "launcher.py",
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
    assert not (ROOT / "zero_agent" / "memory" / "vision_api.py").exists()
    assert not (ROOT / "zero_agent" / "memory" / "ui_detect.py").exists()
    sop_seed_dir = ROOT / "zero_agent" / "assets" / "memory_seed" / "sops"
    assert sop_seed_dir.is_dir()
    assert all(path.is_file() and path.suffix == ".md" for path in sop_seed_dir.iterdir())
    assert (ROOT / "zero_agent" / "assets" / "review_sop").is_dir()


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
        "sessions",
        "target",
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
