"""Tests for tab lifecycle, native input, CDP and network browser tools."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

from zero_agent.core.handler import BaseHandler
from zero_agent.llm.base import MockResponse
from zero_agent.tools.registry import ToolRegistry

from zero_agent.tools.builtin import web


class FakeDriver:
    default_session_id = "7"

    def __init__(self) -> None:
        self.tabs: list[dict[str, Any]] = []
        self.closed: list[str] = []
        self.activated: list[str] = []
        self.cdp_calls: list[dict[str, Any]] = []
        self.js_calls: list[dict[str, Any]] = []
        self.js_response: Any = {"ok": True}
        self.events: list[dict[str, Any]] = []
        self.cleared: Any = None
        self.intercept_calls: list[dict[str, Any]] = []
        self.intercept_result: Any = {"ok": True}
        self.intercept_list: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.downloads: list[dict[str, Any]] = []

    def get_all_sessions(self) -> list[dict[str, Any]]:
        return [{"id": self.default_session_id, "url": "https://example.test/"}]

    def list_tabs(self, include_all: bool = False) -> list[dict[str, Any]]:
        return list(self.tabs)

    def close_tab(self, tab_id: Any) -> dict[str, Any]:
        self.closed.append(str(tab_id))
        return {"closed": [int(tab_id)], "errors": []}

    def activate_tab(self, tab_id: Any) -> bool:
        self.activated.append(str(tab_id))
        return True

    def navigate_tab(self, tab_id: Any, url: str) -> dict[str, Any]:
        return {"id": int(tab_id), "url": url}

    def create_tab(self, url: str, active: bool = False) -> dict[str, Any]:
        return {"id": 99, "url": url, "active": active}

    def execute_js(self, script: str, timeout: int = 15, session_id: Any = None) -> dict[str, Any]:
        self.js_calls.append({"script": script, "timeout": timeout, "session_id": session_id})
        return {"data": self.js_response}

    def upload(self, tab_id: Any, selector: str, files: list[str],
               index: int = 0, timeout: float = 15) -> dict[str, Any]:
        self.uploads.append({
            "tab_id": tab_id, "selector": selector, "files": files,
            "index": index, "timeout": timeout,
        })
        return {"files": files, "summary": {"ok": True, "files": [{"name": "x"}]}}

    def download(self, method: str = "list", tab_id: Any = None,
                 timeout: float = 20, **kwargs: Any) -> Any:
        self.downloads.append({"method": method, "tab_id": tab_id, "kwargs": kwargs})
        if method == "list":
            return {"downloads": [], "dir": "/downloads"}
        return {"download": {"filename": "/downloads/a.txt", "state": "complete"}, "dir": "/downloads"}

    def cdp(self, method: str, params: dict | None = None, tab_id: Any = None,
            persistent: bool = False, timeout: int = 15) -> dict[str, Any]:
        self.cdp_calls.append({
            "method": method, "params": params, "tab_id": tab_id,
            "persistent": persistent, "timeout": timeout,
        })
        return {"ok": True}

    def get_cdp_events(self, tab_id: Any = None, clear: bool = False,
                       methods: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        return list(self.events)

    def clear_cdp_events(self, tab_id: Any = None) -> None:
        self.cleared = tab_id

    def intercept(self, tab_id: Any, rules: list[dict[str, Any]] | None = None,
                  action: str = "apply", timeout: float = 15) -> Any:
        self.intercept_calls.append({
            "tab_id": str(tab_id), "rules": rules, "action": action,
        })
        return self.intercept_result

    def clear_intercept(self, tab_id: Any, timeout: float = 15) -> Any:
        self.intercept_calls.append({
            "tab_id": str(tab_id), "rules": None, "action": "clear",
        })
        return []

    def list_intercepts(self, tab_id: Any, timeout: float = 15) -> list[dict[str, Any]]:
        self.intercept_calls.append({
            "tab_id": str(tab_id), "rules": None, "action": "list",
        })
        return list(self.intercept_list)



def _browser_registry(config):
    """Core registry plus the optional browser bundle for handler tests."""
    registry = ToolRegistry.with_builtins(config)
    for definition in web.build_web_tools(config):
        registry.register(definition)
    return registry

def test_web_tabs_closes_and_deduplicates(monkeypatch) -> None:
    driver = FakeDriver()
    driver.tabs = [
        {"id": 1, "url": "https://a.test/#one", "active": True, "lastAccessed": 1},
        {"id": 2, "url": "https://a.test/#two", "active": False, "lastAccessed": 2},
        {"id": 3, "url": "https://b.test/", "active": False, "lastAccessed": 3},
    ]
    monkeypatch.setattr(web, "_driver", driver)

    preview = web.web_tabs(action="close_duplicates")
    assert preview["status"] == "success"
    assert preview["dry_run"] is True
    assert preview["would_close"] == 1
    assert preview["duplicate_groups"] == [
        {"url": "https://a.test/", "keep": 1, "close": [2]},
    ]
    assert driver.closed == []

    applied = web.web_tabs(action="close_duplicates", dry_run=False)
    assert applied["status"] == "success"
    assert applied["closed"] == ["2"]
    assert driver.closed == ["2"]

    closed = web.web_tabs(action="close", tab_id="3")
    assert closed["status"] == "success"
    assert closed["closed"] == ["3"]


def test_web_tabs_activate_and_list(monkeypatch) -> None:
    driver = FakeDriver()
    driver.tabs = [{"id": 1, "url": "https://a.test/", "active": False}]
    monkeypatch.setattr(web, "_driver", driver)

    listed = web.web_tabs(action="list")
    assert listed["count"] == 1
    assert listed["tabs"] == driver.tabs

    activated = web.web_tabs(action="activate", tab_id="1")
    assert activated["status"] == "success"
    assert driver.activated == ["1"]


def test_web_input_click_uses_native_mouse_events(monkeypatch) -> None:
    driver = FakeDriver()
    driver.js_response = {"ok": True, "x": 12.5, "y": 30, "tag": "BUTTON"}
    monkeypatch.setattr(web, "_driver", driver)

    result = web.web_input(action="click", selector="#submit")

    assert result["status"] == "success"
    assert result["x"] == 12.5 and result["y"] == 30
    methods = [call["method"] for call in driver.cdp_calls]
    assert methods == [
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert [call["params"]["type"] for call in driver.cdp_calls] == [
        "mouseMoved", "mousePressed", "mouseReleased",
    ]
    assert driver.cdp_calls[1]["params"]["button"] == "left"
    assert driver.cdp_calls[1]["params"]["buttons"] == 1


def test_web_input_type_and_submit_are_native(monkeypatch) -> None:
    driver = FakeDriver()
    monkeypatch.setattr(web, "_driver", driver)

    result = web.web_input(action="type", text="hello", submit=True)

    assert result["status"] == "success"
    assert result["chars"] == 5
    assert [call["method"] for call in driver.cdp_calls] == [
        "Input.insertText",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
    ]
    assert driver.cdp_calls[0]["params"] == {"text": "hello"}
    assert driver.cdp_calls[1]["params"]["key"] == "Enter"
    assert driver.cdp_calls[1]["params"]["type"] == "keyDown"


def test_web_cdp_persistent_and_timeout_are_forwarded(monkeypatch) -> None:
    driver = FakeDriver()
    monkeypatch.setattr(web, "_driver", driver)

    result = web.web_cdp(
        "Network.enable", {"maxTotalBufferSize": 1},
        tab_id="7", persistent=True, timeout_seconds=999,
    )

    assert result["status"] == "success"
    call = driver.cdp_calls[-1]
    assert call["method"] == "Network.enable"
    assert call["persistent"] is True
    assert call["timeout"] == 120
    assert call["params"] == {"maxTotalBufferSize": 1}


def test_web_cdp_saves_base64_results(tmp_path, monkeypatch) -> None:
    driver = FakeDriver()
    encoded = base64.b64encode(b"%PDF-1.4").decode("ascii")
    monkeypatch.setattr(driver, "cdp", lambda *args, **kwargs: {"data": encoded})
    monkeypatch.setattr(web, "_driver", driver)
    target = tmp_path / "paper.pdf"

    result = web.web_cdp("Page.printToPDF", {}, save_to_file=str(target))

    assert result["status"] == "success"
    assert target.read_bytes() == b"%PDF-1.4"
    assert result["saved"] == {"path": str(target), "bytes": 8}
    assert "data" not in result["result"]


def test_web_network_summarizes_and_filters_events(monkeypatch) -> None:
    driver = FakeDriver()
    driver.events = [
        {
            "tabId": 7, "method": "Network.requestWillBeSent", "ts": 1,
            "params": {
                "requestId": "r1", "type": "Document",
                "request": {"url": "https://a.test/", "method": "GET"},
            },
        },
        {
            "tabId": 7, "method": "Network.responseReceived", "ts": 2,
            "params": {
                "requestId": "r1", "type": "Document",
                "response": {"url": "https://a.test/", "status": 200, "mimeType": "text/html"},
            },
        },
        {
            "tabId": 7, "method": "Network.loadingFailed", "ts": 3,
            "params": {"requestId": "r2", "errorText": "net::ERR_FAILED", "canceled": False},
        },
    ]
    monkeypatch.setattr(web, "_driver", driver)

    requests = web.web_network(action="dump", filter="requests")
    assert requests["count"] == 1
    assert requests["events"][0]["url"] == "https://a.test/"

    every = web.web_network(action="dump", filter="all")
    assert every["count"] == 3
    assert [item["event"] for item in every["events"]] == ["request", "response", "failure"]


def test_web_wait_returns_success_and_timeout(monkeypatch) -> None:
    driver = FakeDriver()
    driver.js_response = True
    monkeypatch.setattr(web, "_driver", driver)

    matched = web.web_wait(selector="#app", timeout_seconds=1)
    assert matched["status"] == "success"

    driver.js_response = False
    timed_out = web.web_wait(selector="#missing", timeout_seconds=1, poll_interval=0.1)
    assert timed_out["status"] == "error"
    assert "超时" in timed_out["msg"]


def test_web_network_exposes_raw_events_and_releases_fetch(monkeypatch) -> None:
    driver = FakeDriver()
    driver.events = [{
        "tabId": 7, "method": "Fetch.requestPaused", "ts": 1,
        "params": {"requestId": "f1"},
    }]
    monkeypatch.setattr(web, "_driver", driver)

    events = web.web_network(action="events", methods=["Fetch.requestPaused"])
    assert events["status"] == "success"
    assert events["count"] == 1
    assert events["events"][0]["params"]["requestId"] == "f1"

    continued = web.web_network(action="continue")
    assert continued["status"] == "success"
    assert continued["continued"] == ["f1"]
    assert driver.cdp_calls[-1]["method"] == "Fetch.continueRequest"
    assert driver.cdp_calls[-1]["params"] == {"requestId": "f1"}
    assert driver.cleared == "7"


def test_web_wait_falls_back_to_cdp_when_scripting_hangs(monkeypatch) -> None:
    driver = FakeDriver()
    monkeypatch.setattr(driver, "execute_js", lambda *args, **kwargs: {
        "result": "No response data in 5s (ACK received, script may still be running)",
    })
    monkeypatch.setattr(driver, "cdp", lambda *args, **kwargs: {
        "result": {"type": "boolean", "value": True},
    })
    monkeypatch.setattr(web, "_driver", driver)

    result = web.web_wait(selector="#app", timeout_seconds=1)

    assert result["status"] == "success"


def test_web_intercept_applies_lists_and_clears(monkeypatch) -> None:
    driver = FakeDriver()
    driver.intercept_result = {"tabId": 7, "rules": [{"url_contains": "/x"}]}
    driver.intercept_list = [{"id": "keep"}]
    monkeypatch.setattr(web, "_driver", driver)

    applied = web.web_intercept(rules=[{"url_contains": "/x"}], tab_id="7")
    assert applied["status"] == "success"
    assert driver.intercept_calls[-1] == {
        "tab_id": "7", "rules": [{"url_contains": "/x"}], "action": "apply",
    }

    listed = web.web_intercept(action="list", tab_id="7")
    assert listed["rules"] == [{"id": "keep"}]

    cleared = web.web_intercept(action="clear", tab_id="7")
    assert cleared["status"] == "success"
    assert driver.intercept_calls[-1]["action"] == "clear"


def test_web_captcha_captures_and_ocrs(tmp_path, monkeypatch) -> None:
    driver = FakeDriver()
    driver.js_response = {
        "ok": True, "x": 10, "y": 20, "width": 80, "height": 30,
        "tag": "IMG", "src": "https://example.test/captcha.png",
    }
    encoded = base64.b64encode(b"fake-png").decode("ascii")
    monkeypatch.setattr(web, "_driver", driver)
    monkeypatch.setattr(driver, "cdp", lambda *args, **kwargs: {"data": encoded})
    import zero_agent.utils.ocr_utils as ocr_utils
    monkeypatch.setattr(ocr_utils, "ocr_image", lambda path, enhance=False: {
        "text": "A B 1 2", "lines": ["A B 1 2"], "details": [{"conf": 0.9}],
    })
    target = tmp_path / "captcha.png"

    result = web.web_captcha("#captcha", save_to_file=str(target))

    assert result["status"] == "success"
    assert target.read_bytes() == b"fake-png"
    assert result["text"] == "A B 1 2"
    assert result["normalized_text"] == "AB12"
    assert result["confidence"] == 0.9
    assert result["element"]["x"] == 10


def test_web_captcha_auto_falls_back_to_vision(
    tmp_path, mock_config, monkeypatch,
) -> None:
    mock_config.workspace_dir = str(tmp_path)
    mock_config.llm_backends["default"].vision = True
    registry = _browser_registry(mock_config)
    handler = BaseHandler(registry=registry, cwd=str(tmp_path))

    class VisualSession:
        def vision(self, image_path, prompt):
            return "AB12"

    handler.parent = SimpleNamespace(_sessions={"default": VisualSession()})
    monkeypatch.setattr(web, "web_captcha", lambda **kwargs: {
        "status": "success",
        "path": str(tmp_path / "captcha.png"),
        "text": "",
        "normalized_text": "",
    })
    outcome = _exhaust(handler.dispatch(
        "web_captcha", {"selector": "#captcha"}, MockResponse(content=""),
    ))

    assert outcome.data["status"] == "success"
    assert outcome.data["text"] == "AB12"
    assert outcome.data["normalized_text"] == "AB12"
    assert outcome.data["ocr_engine"] == "vision"


def _exhaust(gen):
    try:
        while True:
            next(gen)
    except StopIteration as exc:
        return exc.value


def test_web_execute_js_timeout_cap_is_120(monkeypatch) -> None:
    driver = FakeDriver()
    captured: dict[str, Any] = {}

    class FakeSimp:
        @staticmethod
        def execute_js_rich(script, driver, no_monitor=False, timeout=15):
            captured["timeout"] = timeout
            return {"status": "success", "js_return": True, "tab_id": "7"}

    monkeypatch.setattr(web, "_driver", driver)
    monkeypatch.setattr(web, "_load_simphtml", lambda: FakeSimp)

    result = web.web_execute_js("return 1", timeout_seconds=999)

    assert result["status"] == "success"
    assert captured["timeout"] == 120


def test_web_input_hover_wheel_and_drag_use_native_events(monkeypatch) -> None:
    driver = FakeDriver()
    driver.js_response = {"ok": True, "x": 10, "y": 20}
    monkeypatch.setattr(web, "_driver", driver)

    hovered = web.web_input(action="hover", selector="#menu")
    assert hovered["status"] == "success"
    hover_calls = list(driver.cdp_calls)
    assert hover_calls[-1]["method"] == "Input.dispatchMouseEvent"
    assert hover_calls[-1]["params"]["type"] == "mouseMoved"
    assert hover_calls[-1]["params"]["buttons"] == 0

    wheeled = web.web_input(action="wheel", x=10, y=20, delta_y=-300)
    assert wheeled["status"] == "success"
    wheel_call = driver.cdp_calls[-1]
    assert wheel_call["method"] == "Input.dispatchMouseEvent"
    assert wheel_call["params"]["type"] == "mouseWheel"
    assert wheel_call["params"]["deltaY"] == -300

    dragged = web.web_input(
        action="drag", x=1, y=2, to_x=11, to_y=22,
        duration_ms=0, steps=4,
    )
    assert dragged["status"] == "success"
    drag_calls = driver.cdp_calls[-7:]
    assert [call["params"]["type"] for call in drag_calls] == [
        "mouseMoved", "mousePressed", "mouseMoved", "mouseMoved",
        "mouseMoved", "mouseMoved", "mouseReleased",
    ]
    assert drag_calls[1]["params"]["buttons"] == 1
    assert drag_calls[-1]["params"]["buttons"] == 0
    assert drag_calls[-1]["params"]["x"] == 11
    assert drag_calls[-1]["params"]["y"] == 22


def test_web_tabs_reload_and_history_actions(monkeypatch) -> None:
    driver = FakeDriver()
    monkeypatch.setattr(web, "_driver", driver)

    reloaded = web.web_tabs(action="reload", tab_id="3", ignore_cache=True)
    assert reloaded == {"status": "success", "action": "reload", "tab_id": "3"}
    assert driver.cdp_calls[-1]["method"] == "Page.reload"
    assert driver.cdp_calls[-1]["params"] == {"ignoreCache": True}

    back = web.web_tabs(action="back", tab_id="3")
    assert back == {"status": "success", "action": "back", "tab_id": "3"}
    assert driver.cdp_calls[-1]["method"] == "Runtime.evaluate"
    assert driver.cdp_calls[-1]["params"]["expression"] == "history.back()"

    forward = web.web_tabs(action="forward", tab_id="3")
    assert forward == {"status": "success", "action": "forward", "tab_id": "3"}
    assert driver.cdp_calls[-1]["params"]["expression"] == "history.forward()"


def test_upload_and_download_tools_use_driver(tmp_path, monkeypatch) -> None:
    driver = FakeDriver()
    monkeypatch.setattr(web, "_driver", driver)
    source = tmp_path / "a.txt"
    source.write_text("hello", encoding="utf-8")

    uploaded = web.web_upload("#file", [str(source)], tab_id="3")
    assert uploaded["status"] == "success"
    assert driver.uploads == [{
        "tab_id": "3", "selector": "#file", "files": [str(source)],
        "index": 0, "timeout": 15.0,
    }]
    assert uploaded["input"]["ok"] is True

    listed = web.web_download(action="list", limit=3)
    assert listed["status"] == "success"
    assert listed["downloads"] == []
    assert driver.downloads[-1]["method"] == "list"

    started = web.web_download(
        action="url", url="https://example.test/a.txt",
        filename="a.txt", timeout_seconds=5,
    )
    assert started["status"] == "success"
    assert driver.downloads[-1]["method"] == "start"
    assert driver.downloads[-1]["kwargs"]["timeout_ms"] == 5000
    assert started["download"]["state"] == "complete"
