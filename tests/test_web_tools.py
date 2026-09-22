"""Tests for ZeroAgent browser tools."""

import base64
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


def test_browser_runtime_modules_are_importable() -> None:
    import importlib.util

    assert importlib.util.find_spec("zero_agent.browser.tm_webdriver")
    assert importlib.util.find_spec("zero_agent.browser.simphtml")


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
    data = outcome.data
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
    data = outcome.data
    assert data["status"] == "success"


def test_web_screenshot_decodes_cdp_payload_and_saves_png(tmp_path, monkeypatch) -> None:
    captured = {}
    encoded = base64.b64encode(b"fake-png").decode("ascii")

    def fake_web_execute_js(script, switch_tab_id=None, no_monitor=False):
        captured.update({
            "script": script,
            "switch_tab_id": switch_tab_id,
            "no_monitor": no_monitor,
        })
        return {"status": "success", "js_return": {"data": encoded}, "tab_id": "7"}

    monkeypatch.setattr(web, "web_execute_js", fake_web_execute_js)
    target = tmp_path / "screenshots" / "browser.png"

    result = web.web_screenshot(str(target), switch_tab_id="7")

    assert result["status"] == "success"
    assert result["path"] == str(target.resolve())
    assert target.read_bytes() == b"fake-png"
    assert captured["switch_tab_id"] == "7"
    assert captured["no_monitor"] is True
    command = json.loads(captured["script"])
    assert command["method"] == "Page.captureScreenshot"
    assert command["tabId"] == 7
    assert command["params"]["captureBeyondViewport"] is True


def test_web_screenshot_uses_the_current_controlled_tab_when_unspecified(
    tmp_path, monkeypatch
) -> None:
    captured = {}
    encoded = base64.b64encode(b"fake-png").decode("ascii")

    def fake_web_execute_js(script, switch_tab_id=None, no_monitor=False):
        captured["script"] = script
        return {"status": "success", "js_return": {"data": encoded}}

    monkeypatch.setattr(web, "web_execute_js", fake_web_execute_js)
    result = web.web_screenshot(str(tmp_path / "browser.png"))

    assert result["status"] == "success"
    assert "tabId" not in json.loads(captured["script"])


def test_web_screenshot_tool_defaults_to_workspace_path(tmp_path, mock_config, monkeypatch) -> None:
    mock_config.workspace_dir = str(tmp_path)
    monkeypatch.setattr(
        web,
        "web_screenshot",
        lambda path, **kwargs: {"status": "success", "path": path, **kwargs},
    )
    registry = ToolRegistry.with_builtins(mock_config)
    handler = BaseHandler(registry=registry, cwd=str(tmp_path))

    outcome = _exhaust(handler.dispatch("web_screenshot", {}, MockResponse(content="")))

    assert outcome.data["status"] == "success"
    assert outcome.data["path"] == str(tmp_path / "screenshots" / "browser.png")


def test_web_execute_js_handler_missing_script_returns_error(
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


def test_web_scan_handler_returns_html_string(
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

    assert outcome.data["status"] == "success"
    assert outcome.data["content"] == "<main>Hello</main>"
    assert outcome.next_prompt is not None
    assert "ref=1" in outcome.next_prompt
    assert "tool=web_scan" in outcome.next_prompt


def _exhaust(gen):
    try:
        while True:
            next(gen)
    except StopIteration as exc:
        return exc.value
