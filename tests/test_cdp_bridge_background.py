"""Behavioral test for the MV3 bridge reconnection watchdog."""

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


def test_probe_alarm_reconnects_a_dormant_bridge() -> None:
    """A closed socket must be restored by the probe alarm."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the extension worker harness")

    harness = r"""
const fs = require('fs');
const vm = require('vm');

const events = {};
function event(name) {
  const entry = { handlers: [] };
  entry.addListener = (fn) => entry.handlers.push(fn);
  entry.removeListener = (fn) => {
    entry.handlers = entry.handlers.filter((item) => item !== fn);
  };
  entry.fire = (...args) => entry.handlers.forEach((fn) => fn(...args));
  events[name] = entry;
  return entry;
}

const sockets = [];
const alarms = [];
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
    onActivated: event('tabs.onActivated'),
  },
  storage: {
    session: {
      get: async () => ({}),
      set: async () => {},
    },
  },
  debugger: {
    onEvent: event('debugger.onEvent'),
    onDetach: event('debugger.onDetach'),
    attach: async () => {},
    detach: async () => {},
    sendCommand: async () => ({}),
    getTargets: async () => [],
  },
  scripting: { executeScript: async () => [{ result: { ok: true, data: null } }] },
  cookies: { getAll: async () => [] },
  management: { getAll: async () => [] },
  contentSettings: {},
  downloads: { search: async () => [], download: async () => 1 },
};

const context = {
  AbortController,
  TextEncoder,
  btoa,
  atob,
  chrome,
  console: { log() {}, error() {} },
  fetch: async () => ({ ok: true }),
  setTimeout,
  clearTimeout,
  WebSocket: FakeWebSocket,
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);

(async () => {
  if (sockets.length !== 2) throw new Error(`expected two initial sockets, got ${sockets.length}`);
  const first = sockets.find((item) => item.url.endsWith(':18765'));
  if (!first) throw new Error('missing ZeroAgent socket');
  first.readyState = FakeWebSocket.OPEN;
  await first.onopen();
  first.onclose();

  for (const listener of events['alarms.onAlarm'].handlers) {
    listener({ name: 'tmwd-ws-probe' });
  }
  await new Promise((resolve) => setTimeout(resolve, 20));

  const second = sockets.find((item) => item.url === first.url && item !== first);
  if (!second) throw new Error('probe alarm did not reconnect');
  second.readyState = FakeWebSocket.OPEN;
  await second.onopen();

  process.stdout.write(JSON.stringify({
    socketCount: sockets.length,
    alarms,
    firstSent: first.sent,
    secondSent: second.sent,
  }));
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", harness, str(BACKGROUND_JS)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["socketCount"] == 3
    alarm_names = [entry["name"] for entry in payload["alarms"]]
    assert "tmwd-ws-keepalive" in alarm_names
    assert "tmwd-ws-probe" in alarm_names
    assert payload["firstSent"][0]["type"] == "ext_ready"
    assert payload["secondSent"][0]["type"] == "ext_ready"
    assert payload["secondSent"][0]["tabs"][0]["id"] == 7
