"""Tests for browser tool compatibility loading."""

from zero_agent.tools.builtin import web


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
