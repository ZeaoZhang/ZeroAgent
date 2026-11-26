"""Tests for connection-level extension commands in TMWebDriver."""

from __future__ import annotations

import json

import pytest

from zero_agent.browser.tm_webdriver import TMWebDriver


class FakeClient:
    def __init__(self, driver: TMWebDriver, data=None, success=True, error="boom") -> None:
        self.driver = driver
        self.data = data
        self.success = success
        self.error = error
        self.sent: list[dict] = []

    def send_message(self, payload: str) -> None:
        command = json.loads(payload)
        self.sent.append(command)
        self.driver.results[command["id"]] = {
            "success": self.success,
            "data": self.data if self.success else self.error,
            "newTabs": [],
        }


def make_driver() -> TMWebDriver:
    driver = TMWebDriver.__new__(TMWebDriver)
    driver.host, driver.port = "127.0.0.1", 18765
    driver._init_state()
    driver.is_remote = False
    return driver


def sent_command(client: FakeClient, index: int = -1) -> tuple[dict, dict]:
    payload = client.sent[index]
    return payload, json.loads(payload["code"])


def test_extension_commands_use_connection_level_ws() -> None:
    driver = make_driver()
    client = FakeClient(driver, data=[{"id": 7, "url": "https://example.test/"}])
    driver.ext_client = client

    tabs = driver.list_tabs()

    assert tabs == [{"id": 7, "url": "https://example.test/"}]
    payload, command = sent_command(client)
    assert command == {"cmd": "tabs", "method": "list"}
    assert "tabId" not in payload


def test_close_and_cdp_forward_the_target_tab() -> None:
    driver = make_driver()
    client = FakeClient(driver, data={"closed": [7], "errors": []})
    driver.ext_client = client

    assert driver.close_tab("7") == {"closed": [7], "errors": []}
    payload, command = sent_command(client)
    assert payload["tabId"] == 7
    assert command == {"cmd": "tabs", "method": "close", "tabId": 7}

    client.data = {"ok": True}
    driver.cdp("Network.enable", {"maxTotalBufferSize": 1}, tab_id=7, persistent=True)
    payload, command = sent_command(client)
    assert payload["tabId"] == 7
    assert command == {
        "cmd": "cdp",
        "method": "Network.enable",
        "params": {"maxTotalBufferSize": 1},
        "tabId": 7,
        "persistent": True,
    }


def test_extension_error_is_raised() -> None:
    driver = make_driver()
    client = FakeClient(driver, success=False, error="another debugger attached")
    driver.ext_client = client

    with pytest.raises(Exception, match="another debugger attached"):
        driver.close_tab(7)


def test_cdp_events_command_is_not_shadowed_by_the_buffer() -> None:
    driver = make_driver()
    client = FakeClient(driver, data=[{"method": "Network.requestWillBeSent"}])
    driver.ext_client = client

    events = driver.cdp_events(tab_id=7, limit=5)

    assert events == [{"method": "Network.requestWillBeSent"}]
    payload, command = sent_command(client)
    assert payload["tabId"] == 7
    assert command == {"cmd": "cdpEvents", "clear": False, "limit": 5}


def test_intercept_rules_are_sent_and_cleared() -> None:
    driver = make_driver()
    client = FakeClient(driver, data={"tabId": 7, "rules": [{"url_contains": "/x"}]})
    driver.ext_client = client

    applied = driver.intercept("7", [{"url_contains": "/x"}])
    assert applied == {"tabId": 7, "rules": [{"url_contains": "/x"}]}
    payload, command = sent_command(client)
    assert payload["tabId"] == 7
    assert command == {
        "cmd": "intercept",
        "action": "apply",
        "tabId": 7,
        "rules": [{"url_contains": "/x"}],
    }

    client.data = []
    assert driver.clear_intercept("7") == []
    _, command = sent_command(client)
    assert command["action"] == "clear"

    client.data = [{"id": "keep"}]
    assert driver.list_intercepts("7") == [{"id": "keep"}]
    _, command = sent_command(client)
    assert command == {"cmd": "intercept", "action": "list", "tabId": 7}


def test_cdp_events_are_buffered_and_filterable() -> None:
    driver = make_driver()
    driver._record_cdp_event(7, "Network.requestWillBeSent", {"requestId": "r1"}, ts=1)
    driver._record_cdp_event(7, "Network.responseReceived", {"requestId": "r2"}, ts=2)
    driver._record_cdp_event(8, "Network.requestWillBeSent", {"requestId": "r3"}, ts=3)

    assert [event["params"]["requestId"] for event in driver.get_cdp_events(tab_id=7)] == [
        "r1",
        "r2",
    ]
    assert [event["params"]["requestId"] for event in driver.get_cdp_events(
        tab_id="7", methods=["Network.requestWillBeSent"],
    )] == ["r1"]
    assert [event["params"]["requestId"] for event in driver.get_cdp_events(limit=1)] == [
        "r3",
    ]

    driver.clear_cdp_events(7)
    assert driver.get_cdp_events(tab_id=7) == []
    assert len(driver.get_cdp_events(tab_id=8)) == 1
