"""Tests for browser tool compatibility loading."""

import json
from importlib import resources

from zero_agent.core.handler import BaseHandler
from zero_agent.llm.base import MockResponse
from zero_agent.tools.builtin import web
from zero_agent.tools.registry import ToolRegistry


def test_web_scan_reports_browser_extra_hint_when_runtime_missing(monkeypatch) -> None:
    monkeypatch.setattr(web, "_driver", None)
    monkeypatch.setattr(web, "_driver_error", "missing browser runtime")
    monkeypatch.setattr(web, "_get_driver", lambda: None)

    result = web.web_scan()

    assert result["status"] == "error"
    assert "missing browser runtime" in result["msg"]


def test_vendored_browser_modules_are_importable() -> None:
    import importlib.util

    assert importlib.util.find_spec("zero_agent.vendor.genericagent.tm_webdriver")
    assert importlib.util.find_spec("zero_agent.vendor.genericagent.simphtml")


def test_bundled_browser_extension_assets_are_readable() -> None:
    assets = resources.files("zero_agent.assets")

    assert assets.joinpath("tmwd_cdp_bridge", "manifest.json").is_file()
    assert assets.joinpath("tmwd_cdp_bridge", "config.js").is_file()
    assert assets.joinpath("tmwd_cdp_bridge", "background.js").is_file()
    assert assets.joinpath("tmwd_cdp_bridge", "content.js").is_file()
    assert "tmwd_cdp_bridge" in web.browser_extension_dir()


def test_web_execute_js_handler_reads_script_file_and_saves_result(
    tmp_path, mock_config, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script_path = workspace / "probe.js"
    script_path.write_text("return document.title", encoding="utf-8")
    mock_config.workspace_dir = str(workspace)

    captured = {}

    def fake_web_execute_js(script, switch_tab_id=None, no_monitor=False):
        captured["script"] = script
        captured["switch_tab_id"] = switch_tab_id
        captured["no_monitor"] = no_monitor
        return {"status": "success", "js_return": "long browser result"}

    monkeypatch.setattr(web, "web_execute_js", fake_web_execute_js)

    registry = ToolRegistry.with_builtins(mock_config)
    handler = BaseHandler(registry=registry, cwd=str(workspace))
    gen = handler.dispatch(
        "web_execute_js",
        {
            "script": "probe.js",
            "tab_id": "tab-1",
            "no_monitor": True,
            "save_to_file": "result.txt",
        },
        MockResponse(content=""),
    )

    outcome = _exhaust(gen)

    assert captured == {
        "script": "return document.title",
        "switch_tab_id": "tab-1",
        "no_monitor": True,
    }
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "long browser result"
    data = json.loads(outcome.data)
    assert data["status"] == "success"
    assert "[已保存完整内容到" in data["js_return"]


def test_web_execute_js_handler_uses_javascript_code_block(
    tmp_path, mock_config, monkeypatch
) -> None:
    mock_config.workspace_dir = str(tmp_path)
    captured = {}

    def fake_web_execute_js(script, switch_tab_id=None, no_monitor=False):
        captured["script"] = script
        return {"status": "success", "js_return": "ok"}

    monkeypatch.setattr(web, "web_execute_js", fake_web_execute_js)

    registry = ToolRegistry.with_builtins(mock_config)
    handler = BaseHandler(registry=registry, cwd=str(tmp_path))
    response = MockResponse(content="```javascript\nreturn location.href\n```")
    outcome = _exhaust(handler.dispatch("web_execute_js", {}, response))

    assert captured["script"] == "return location.href"
    data = json.loads(outcome.data)
    assert data["status"] == "success"


def test_web_execute_js_handler_missing_script_matches_ga(
    tmp_path, mock_config
) -> None:
    mock_config.workspace_dir = str(tmp_path)
    registry = ToolRegistry.with_builtins(mock_config)
    handler = BaseHandler(registry=registry, cwd=str(tmp_path))

    outcome = _exhaust(handler.dispatch(
        "web_execute_js",
        {},
        MockResponse(content="没有脚本。"),
    ))

    assert outcome.data == (
        "[Error] Script missing. Use ```javascript block or 'script' arg."
    )
    assert outcome.next_prompt == "\n"


def test_web_scan_handler_returns_ga_shaped_html_string(
    tmp_path, mock_config, monkeypatch
) -> None:
    mock_config.workspace_dir = str(tmp_path)

    def fake_web_scan(tabs_only=False, switch_tab_id=None, text_only=False, maxlen=35000):
        return {
            "status": "success",
            "metadata": {"tabs_count": 1, "tabs": [], "active_tab": "tab-1"},
            "content": "<main>Hello</main>",
        }

    monkeypatch.setattr(web, "web_scan", fake_web_scan)
    registry = ToolRegistry.with_builtins(mock_config)
    handler = BaseHandler(registry=registry, cwd=str(tmp_path))

    outcome = _exhaust(handler.dispatch("web_scan", {}, MockResponse(content="")))

    assert isinstance(outcome.data, str)
    assert '"status": "success"' in outcome.data
    assert "```html\n<main>Hello</main>\n```" in outcome.data
    assert outcome.next_prompt == "\n"


def _exhaust(gen):
    try:
        while True:
            next(gen)
    except StopIteration as exc:
        return exc.value
