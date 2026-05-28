"""Tests for browser tool compatibility loading."""

from importlib import resources

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


def test_bundled_browser_extension_assets_are_readable() -> None:
    assets = resources.files("zero_agent.assets")

    assert assets.joinpath("tmwd_cdp_bridge", "manifest.json").is_file()
    assert assets.joinpath("tmwd_cdp_bridge", "config.js").is_file()
    assert assets.joinpath("tmwd_cdp_bridge", "background.js").is_file()
    assert assets.joinpath("tmwd_cdp_bridge", "content.js").is_file()
    assert "tmwd_cdp_bridge" in web.browser_extension_dir()
