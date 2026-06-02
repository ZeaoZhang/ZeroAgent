"""Regression tests for the supported ZeroAgent frontend surface."""

from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = PROJECT_ROOT / "zero_agent" / "frontends"


def _source(relative: str) -> str:
    return (FRONTENDS / relative).read_text(encoding="utf-8")


def _imported_names(relative: str, module: str) -> set[str]:
    tree = ast.parse(_source(relative))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.name for alias in node.names)
    return names


def test_supported_frontend_entrypoints_are_explicit() -> None:
    kept = {
        "__init__.py",
        "acp_bridge.py",
        "desktop_bridge.py",
        "desktop_pet.pyw",
        "launch.pyw",
        "launcher.py",
        "plan_state.py",
        "qtapp.py",
        "stapp.py",
        "ui_contract.py",
    }
    existing = {
        path.name
        for path in FRONTENDS.iterdir()
        if path.is_file() and path.suffix in {".py", ".pyw", ".html"}
    }

    assert existing == kept


def test_removed_legacy_frontends_do_not_return() -> None:
    removed = {
        "conductor.html",
        "conductor.py",
        "desktop_pet_basic.pyw",
        "hub.pyw",
        "stapp2.py",
        "tui_app.py",
    }

    assert not any((FRONTENDS / name).exists() for name in removed)


def test_web_and_qt_use_shared_ui_contract() -> None:
    contract = "zero_agent.frontends.ui_contract"

    assert {
        "APP_NAME",
        "APP_KICKER",
        "CHAT_PLACEHOLDER",
        "CHAT_PLACEHOLDER_ZH",
        "DEFAULT_SESSION_TITLE",
        "EXPORT_BUTTON_LABEL",
        "EXPORT_FILE_PREFIX",
        "WELCOME_SUBTITLE",
        "WELCOME_TITLE",
    }.issubset(_imported_names("stapp.py", contract))

    assert {
        "APP_NAME",
        "DEFAULT_SESSION_TITLE",
        "DEFAULT_SESSION_TITLE_ZH",
        "QT_AUTO_DISABLE_LABEL_ZH",
        "QT_AUTO_ENABLE_LABEL_ZH",
        "QT_READY_NOTICE_ZH",
        "QT_UNTITLED_SESSION_ZH",
    }.issubset(_imported_names("qtapp.py", contract))


def test_desktop_web_shell_uses_same_copy() -> None:
    app_js = _source("desktop/static/app.js")
    index_html = _source("desktop/static/index.html")

    assert "const UI =" in app_js
    assert "APP_NAME: 'ZeroAgent'" in app_js
    assert "DEFAULT_SESSION_TITLE: 'New chat'" in app_js
    assert "CHAT_PLACEHOLDER: 'Ask ZeroAgent to work on something'" in app_js
    assert "WELCOME_TITLE: 'Ready'" in app_js
    assert 'data-ui="app-name"' in index_html
    assert 'data-ui="chat-placeholder"' in index_html
    assert 'id="sidebar"' in index_html
    assert 'id="sidebar-active-title"' in index_html
    assert 'id="sidebar-session-list"' in index_html
    assert 'id="sidebar-settings-title"' in index_html
    assert 'id="sidebar-settings-btn"' in index_html
    assert 'id="sidebar-stop-btn"' in index_html
    assert 'id="sidebar-tools-btn"' in index_html
    assert 'id="command-palette"' in index_html
    assert 'sidebar-commands-title' not in index_html
    assert 'sidebar-command-list' not in index_html
    assert 'command-chip' not in index_html
    assert "const COMMANDS =" in app_js
    assert "function renderCommandPalette()" in app_js
    assert "function renderSidebarSessions()" in app_js
    assert "function reinjectTools()" in app_js
    assert "function bindCommandButtons()" not in app_js
    assert "$('sidebar-new-btn').addEventListener('click', newSession)" in app_js
    assert "$('sidebar-stop-btn').addEventListener('click', forceStopActiveSession)" in app_js
    assert "$('sidebar-tools-btn').addEventListener('click', reinjectTools)" in app_js


def test_desktop_web_bridge_exposes_sidebar_actions() -> None:
    bridge = _source("desktop_bridge.py")
    web_adapter = _source("desktop/static/ga-web.js")

    assert "def reinject_tools(self, sid: str) -> dict:" in bridge
    assert 'app.router.add_post("/session/{sid}/reinject-tools", reinject_tools_handler)' in bridge
    assert "case 'session/reinject-tools':" in web_adapter


def test_packaging_includes_frontend_assets() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'qt = ["PySide6>=6.6", "markdown>=3.5"]' in pyproject
    assert 'all-extras = ["zero-agent[ui,qt,browser,memory,plot,bots]"]' in pyproject
    assert '"zero_agent.frontends.themes"' in pyproject
    assert '"desktop/static/*"' in pyproject
    assert '"desktop/static/vendor/*"' in pyproject
    assert '"skins/**/*"' in pyproject
    assert '"zero_agent.frontends.themes" = ["*.css"]' in pyproject


def test_desktop_pet_docs_match_supported_entrypoint_and_port() -> None:
    readme = (FRONTENDS / "DESKTOP_PET_README.md").read_text(encoding="utf-8")

    assert "desktop_pet.pyw" in readme
    assert "desktop_pet_v2.pyw" not in readme
    assert "127.0.0.1:41983" in readme
    assert "127.0.0.1:51983" not in readme
