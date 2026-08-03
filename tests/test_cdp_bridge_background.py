"""Behavioral tests for the bundled MV3 CDP bridge service worker."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


BACKGROUND_JS = (
    Path(__file__).parents[1]
    / "zero_agent"
    / "assets"
    / "tmwd_cdp_bridge"
    / "background.js"
)


def test_watchdog_reconnects_a_dormant_bridge() -> None:
    """The persistent MV3 wake-up event must restore the master WebSocket."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the extension worker harness")

    harness = r"""
const fs = require('fs');
const vm = require('vm');
const events = {};
const event = name => ({
  addListener(fn) { (events[name] ||= []).push(fn); },
  removeListener() {},
});
const sockets = [];
const intervals = [];
const clearedIntervals = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    sockets.push(this);
  }
  send(message) { this.sent.push(JSON.parse(message)); }
}
const alarms = [];
const chrome = {
  runtime: {
    onInstalled: event('runtime.onInstalled'),
    onStartup: event('runtime.onStartup'),
    onMessage: event('runtime.onMessage'),
    reload() {},
  },
  declarativeNetRequest: { updateDynamicRules() {} },
  alarms: {
    create(name, info) { alarms.push({ name, info }); },
    onAlarm: event('alarms.onAlarm'),
  },
  tabs: {
    query: async () => [{ id: 7, url: 'https://example.com', title: 'Example' }],
    onUpdated: event('tabs.onUpdated'),
    onRemoved: event('tabs.onRemoved'),
    onCreated: event('tabs.onCreated'),
  },
};
const context = {
  AbortController,
  chrome,
  console: { log() {}, error() {} },
  fetch: async () => ({ ok: false }),
  setTimeout,
  clearTimeout,
  setInterval(fn, delay) { intervals.push({ fn, delay }); return intervals.length; },
  clearInterval(id) { clearedIntervals.push(id); },
  WebSocket: FakeWebSocket,
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
if (sockets.length !== 1) throw new Error(`expected initial connection, got ${sockets.length}`);
sockets[0].readyState = 2;
for (const listener of events['alarms.onAlarm']) {
  listener({ name: 'tmwd-ws-watchdog' });
}
const replacement = sockets[1];
sockets[0].onclose();
replacement.readyState = FakeWebSocket.OPEN;
replacement.onopen();
setTimeout(() => {
  intervals[0].fn();
  replacement.onclose();
  for (const listener of events['tabs.onUpdated']) {
    listener(7, { status: 'complete' });
  }
  process.stdout.write(JSON.stringify({
    socketCount: sockets.length,
    alarms,
    intervalDelay: intervals[0].delay,
    sent: replacement.sent,
    clearedIntervals,
  }));
}, 0);
"""
    result = subprocess.run(
        [node, "-e", harness, str(BACKGROUND_JS)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "socketCount": 3,
        "alarms": [
            {"name": "tmwd-ws-watchdog", "info": {"periodInMinutes": 1}},
        ],
        "intervalDelay": 20_000,
        "sent": [
            {
                "type": "ext_ready",
                "tabs": [
                    {"id": 7, "url": "https://example.com", "title": "Example"},
                ],
            },
            {"type": "ping"},
        ],
        "clearedIntervals": [1],
    }
