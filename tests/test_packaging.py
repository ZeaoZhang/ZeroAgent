"""Packaging and legacy-frontend compatibility checks."""

from __future__ import annotations

import importlib
import importlib.util
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


def test_frontend_package_data_tracks_current_ga_style_assets() -> None:
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]["zero_agent.frontends"]

    assert "desktop/static/*" in package_data
    assert "skins/**/*" in package_data
    assert "conductor_im_plugins/*" in package_data
    assert "*.html" in package_data
    assert "zero_agent.frontends.themes" not in _pyproject()["tool"]["setuptools"]["packages"]


def test_bots_extra_covers_ga_frontend_channels() -> None:
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


def test_legacy_frontend_bare_import_aliases_are_available() -> None:
    modules = [
        "zero_agent.chatapp_common",
        "zero_agent.continue_cmd",
        "zero_agent.btw_cmd",
        "zero_agent.export_cmd",
        "zero_agent.review_cmd",
        "zero_agent.session_names",
        "zero_agent.slash_cmds",
        "zero_agent.cost_tracker",
        "zero_agent.llmcore",
        "llmcore",
    ]

    for module_name in modules:
        importlib.import_module(module_name)

    from zero_agent.chatapp_common import (  # noqa: F401
        _handle_continue_frontend,
        _reset_conversation,
        _restore_native_history,
        _restore_text_pairs,
    )


def test_ga_style_channel_modules_import_when_bot_extras_are_installed() -> None:
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
        "zero_agent.frontends.wechatapp",
        "zero_agent.frontends.wecomapp",
        "zero_agent.frontends.dingtalkapp",
        "zero_agent.frontends.qqapp",
        "zero_agent.frontends.fsapp",
        "zero_agent.frontends.tgapp",
        "zero_agent.frontends.dcapp",
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
