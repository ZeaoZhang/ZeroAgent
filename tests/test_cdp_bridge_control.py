"""Behavioral tests for tab lifecycle and persistent CDP in the MV3 bridge."""

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


def test_bridge_closes_tabs_and_keeps_per_tab_cdp_attachments() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the extension worker harness")

    harness = r"""
(async () => {
const fs = require('fs');
const vm = require('vm');

const listeners = {};
function event(name) {
  if (!listeners[name]) {
    const entry = { handlers: [] };
    entry.addListener = (fn) => entry.handlers.push(fn);
    entry.removeListener = (fn) => {
      entry.handlers = entry.handlers.filter((item) => item !== fn);
    };
    entry.fire = (...args) => entry.handlers.forEach((fn) => fn(...args));
    listeners[name] = entry;
  }
  return listeners[name];
}

const state = {
  removed: [],
  updates: [],
  windows: [],
  created: [],
  attachCalls: [],
  detachCalls: [],
  sendCalls: [],
  attached: new Set(),
  requireAttached: false,
  sendDelayFor: null,
};
const tabs = [
  { id: 7, url: 'https://a.test/', title: 'A', active: true, windowId: 1, index: 0, status: 'complete', pinned: false },
  { id: 8, url: 'file:///tmp/a.html', title: 'F', active: false, windowId: 1, index: 1, status: 'complete', pinned: false },
  { id: 9, url: 'chrome://settings', title: 'S', active: false, windowId: 1, index: 2, status: 'complete', pinned: false },
  { id: 10, url: 'about:blank', title: 'B', active: false, windowId: 1, index: 3, status: 'complete', pinned: false },
  { id: 12, url: 'chrome-extension://abc/popup.html', title: 'Ext', active: false, windowId: 1, index: 4, status: 'complete', pinned: false },
];

const chrome = {
  runtime: {
    onInstalled: event('runtime.onInstalled'),
    onStartup: event('runtime.onStartup'),
    onMessage: event('runtime.onMessage'),
    reload() {},
  },
  declarativeNetRequest: { updateDynamicRules() {} },
  alarms: { create() {}, onAlarm: event('alarms.onAlarm') },
  tabs: {
    query: async () => tabs.slice(),
    get: async (id) => {
      const tab = tabs.find((item) => item.id === id);
      if (!tab) throw new Error('no tab');
      return { ...tab };
    },
    create: async (options) => {
      const tab = {
        id: 11, url: options.url, title: 'New', active: !!options.active,
        windowId: 1, index: tabs.length, status: 'complete', pinned: false,
      };
      tabs.push(tab);
      state.created.push(tab);
      return tab;
    },
    update: async (id, props) => {
      const tab = tabs.find((item) => item.id === id);
      if (!tab) throw new Error('no tab');
      Object.assign(tab, props);
      state.updates.push({ tabId: id, props });
      return { ...tab };
    },
    remove: async (id) => {
      const index = tabs.findIndex((item) => item.id === id);
      if (index < 0) throw new Error('no tab');
      tabs.splice(index, 1);
      state.removed.push(id);
    },
    onUpdated: event('tabs.onUpdated'),
    onRemoved: event('tabs.onRemoved'),
    onCreated: event('tabs.onCreated'),
    onActivated: event('tabs.onActivated'),
  },
  windows: {
    update: async (windowId, props) => {
      state.windows.push({ windowId, props });
      return { id: windowId, ...props };
    },
  },
  storage: {
    session: {
      store: {},
      async get(key) { return { [key]: this.store[key] }; },
      async set(values) { Object.assign(this.store, values); },
    },
  },
  debugger: {
    onEvent: event('debugger.onEvent'),
    onDetach: event('debugger.onDetach'),
    attach: async ({ tabId }) => {
      if (state.attached.has(tabId)) {
        throw new Error(`Another debugger is already attached to tab with id ${tabId}`);
      }
      state.attached.add(tabId);
      state.attachCalls.push(tabId);
    },
    detach: async ({ tabId }) => {
      state.attached.delete(tabId);
      state.detachCalls.push(tabId);
    },
    sendCommand: async ({ tabId }, method, params) => {
      if (state.requireAttached && !state.attached.has(tabId)) {
        throw new Error('not attached before send');
      }
      state.sendCalls.push({ tabId, method, params });
      const delay = state.sendDelayFor
        ? (state.sendDelayFor[params?.expression] || 0)
        : 0;
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      if (state.requireAttached && !state.attached.has(tabId)) {
        throw new Error('detached during send');
      }
      return { method, tabId };
    },
    getTargets: async () => [...state.attached].map((tabId) => ({ tabId, attached: true })),
  },
  scripting: { executeScript: async () => [{ result: { ok: true, data: null } }] },
  cookies: { getAll: async () => [] },
  management: { getAll: async () => [] },
  contentSettings: {},
};

const sockets = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    sockets.push(this);
  }
  send(message) {
    this.sent.push(JSON.parse(message));
  }
}

const intervals = [];
const context = {
  AbortController,
  TextEncoder,
  btoa,
  atob,
  chrome,
  console: { log() {}, error() {} },
  fetch: async () => ({ ok: false }),
  setTimeout,
  clearTimeout,
  setInterval(fn, delay) { intervals.push({ fn, delay }); return intervals.length; },
  clearInterval() {},
  WebSocket: FakeWebSocket,
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);

if (sockets.length !== 2) throw new Error(`expected two bridge sockets, got ${sockets.length}`);
const socket = sockets.find((item) => item.url.endsWith(':18765'));
if (!socket) throw new Error('missing ZeroAgent socket');
socket.readyState = FakeWebSocket.OPEN;
await socket.onopen();

let sequence = 0;
async function command(payload, tabId) {
  const id = `cmd-${++sequence}`;
  await socket.onmessage({
    data: JSON.stringify({ id, code: JSON.stringify(payload), tabId }),
  });
  const reply = socket.sent.filter((item) => item.id === id).pop();
  if (!reply) throw new Error(`no reply for ${JSON.stringify(payload)}`);
  return reply;
}
function assert(condition, message) {
  if (!condition) throw new Error(message);
}

let reply = await command({ cmd: 'tabs', method: 'list' });
assert(reply.type === 'result', 'tabs.list must succeed');
const listed = reply.result.map((tab) => tab.id);
assert(listed.includes(7), 'https tab must be listed');
assert(listed.includes(8), 'file tab must be listed');
assert(listed.includes(10), 'about:blank tab must be listed');
assert(listed.includes(12), 'extension tab must be listed');
assert(!listed.includes(9), 'chrome:// tab must stay out of the scriptable list');

reply = await command({ cmd: 'tabs', method: 'activate', tabId: 10 });
assert(reply.type === 'result', 'tabs.activate must succeed');
assert(state.updates.some((item) => item.tabId === 10 && item.props.active === true),
  'activate must select the tab');
assert(state.windows.some((item) => item.windowId === 1 && item.props.focused === true),
  'activate must focus the window');

reply = await command({ cmd: 'tabs', method: 'close', tabId: 10 });
assert(state.removed.includes(10), 'tabs.close must remove the tab');
assert(reply.result.closed.includes(10), 'tabs.close must report the closed id');

reply = await command({ cmd: 'cdp', method: 'Runtime.evaluate', params: { expression: '1' }, tabId: 7, persistent: true });
assert(reply.type === 'result', 'persistent CDP on tab 7 must succeed');
reply = await command({ cmd: 'cdp', method: 'Runtime.evaluate', params: { expression: '2' }, tabId: 8, persistent: true });
assert(reply.type === 'result', 'persistent CDP on tab 8 must succeed');
assert(state.attached.has(7) && state.attached.has(8), 'two tabs must stay attached');
assert(state.detachCalls.length === 0, 'persistent calls must not detach');

reply = await command({ cmd: 'cdp', method: 'Page.captureScreenshot', params: {}, tabId: 11 });
assert(state.attachCalls.includes(11) && state.detachCalls.includes(11),
  'one-shot CDP must attach and detach its own tab');
assert(state.attached.has(7) && state.attached.has(8),
  'one-shot CDP must not evict persistent attachments');

state.requireAttached = true;
state.sendDelayFor = { fast: 20, slow: 80 };
const fast = command({ cmd: 'cdp', method: 'Runtime.evaluate', params: { expression: 'fast' }, tabId: 11 });
await new Promise((resolve) => setTimeout(resolve, 5));
const slow = command({ cmd: 'cdp', method: 'Runtime.evaluate', params: { expression: 'slow' }, tabId: 11 });
const serialized = await Promise.all([fast, slow]);
assert(serialized.every((reply) => reply.type === 'result'),
  'same-tab concurrent one-shot commands must serialize');
state.requireAttached = false;
state.sendDelayFor = null;

const interceptReply = await command({
  cmd: 'intercept',
  action: 'apply',
  tabId: 11,
  rules: [{
    id: 'echo',
    url_contains: '/api/echo',
    method: 'POST',
    request_headers: { 'X-Injected': '1' },
    response_status: 201,
    response_headers: { 'content-type': 'application/json' },
    response_body: '{"ok":true}',
  }],
});
assert(interceptReply.type === 'result', 'intercept apply must succeed');
assert(interceptReply.result.patterns.length === 2, 'response rules need both Fetch stages');
const enableCall = state.sendCalls.filter((call) => call.method === 'Fetch.enable').pop();
assert(enableCall && enableCall.params.patterns.length === 2, 'Fetch.enable must include both stages');

chrome.debugger.onEvent.fire({ tabId: 11 }, 'Fetch.requestPaused', {
  requestId: 'f1',
  request: {
    url: 'https://api.test/api/echo',
    method: 'POST',
    headers: { 'content-type': 'text/plain' },
  },
});
await new Promise((resolve) => setTimeout(resolve, 20));
let fetchCall = state.sendCalls.filter((call) => call.method === 'Fetch.continueRequest'
  && call.params.requestId === 'f1').pop();
assert(fetchCall, 'matching request must be continued');
const injected = fetchCall.params.headers.find((header) => header.name === 'X-Injected');
assert(injected && injected.value === '1', 'request header override must be applied');

chrome.debugger.onEvent.fire({ tabId: 11 }, 'Fetch.requestPaused', {
  requestId: 'f1',
  request: { url: 'https://api.test/api/echo', method: 'POST' },
  responseStatusCode: 200,
  responseHeaders: [{ name: 'content-type', value: 'text/plain' }],
});
await new Promise((resolve) => setTimeout(resolve, 20));
const fulfilCall = state.sendCalls.filter((call) => call.method === 'Fetch.fulfillRequest').pop();
assert(fulfilCall, 'response rule must fulfill the request: ' + JSON.stringify(state.sendCalls.filter((call) => call.method.startsWith('Fetch.'))));
assert(fulfilCall.params.responseCode === 201, 'response status must be overridden');
assert(fulfilCall.params.body === btoa('{"ok":true}'), 'response body must be base64 encoded');
assert(fulfilCall.params.responseHeaders.some((header) =>
  header.name.toLowerCase() === 'content-type' && header.value === 'application/json'),
  'response headers must be overridden');

chrome.debugger.onEvent.fire({ tabId: 11 }, 'Fetch.requestPaused', {
  requestId: 'f2',
  request: { url: 'https://other.test/', method: 'GET' },
});
await new Promise((resolve) => setTimeout(resolve, 20));
const autoContinue = state.sendCalls.filter((call) => call.method === 'Fetch.continueRequest'
  && call.params.requestId === 'f2').pop();
assert(autoContinue, 'unmatched requests must be continued automatically');
assert(socket.sent.some((item) => item.type === 'cdp_event'
  && item.method === 'ZeroAgent.intercept'), 'interception decisions must be logged');

await command({
  cmd: 'intercept',
  action: 'apply',
  tabId: 11,
  rules: [{ url_contains: '/api/header', response_headers: { 'x-test': '1' } }],
});
chrome.debugger.onEvent.fire({ tabId: 11 }, 'Fetch.requestPaused', {
  requestId: 'f3',
  request: { url: 'https://api.test/api/header', method: 'GET' },
  responseStatusCode: 200,
  responseHeaders: [{ name: 'content-type', value: 'text/plain' }],
});
await new Promise((resolve) => setTimeout(resolve, 20));
const continueResponse = state.sendCalls.filter(
  (call) => call.method === 'Fetch.continueResponse',
).pop();
assert(continueResponse, 'header-only rules must use continueResponse');
assert(continueResponse.params.responseHeaders.some(
  (header) => header.name === 'x-test' && header.value === '1',
), 'continueResponse must carry header overrides');

const clearReply = await command({ cmd: 'intercept', action: 'clear', tabId: 11 });
assert(clearReply.type === 'result', 'intercept clear must succeed');
assert(state.sendCalls.some((call) => call.method === 'Fetch.disable'), 'clear must disable Fetch');

chrome.debugger.onEvent.fire({ tabId: 7 }, 'Network.requestWillBeSent', {
  requestId: 'r1', request: { url: 'https://example.test/' },
});
assert(socket.sent.some((item) => item.type === 'cdp_event' && item.tabId === 7
  && item.method === 'Network.requestWillBeSent'), 'debugger events must be forwarded');
reply = await command({ cmd: 'cdpEvents', tabId: 7, limit: 10 });
assert(reply.type === 'result' && reply.result.length === 1,
  'cdpEvents must return the buffered event');
assert(reply.result[0].params.requestId === 'r1', 'cdpEvents must preserve params');

reply = await command({ cmd: 'cdp', method: 'detach', tabId: 7 });
assert(reply.type === 'result', 'cdp.detach must succeed');
assert(!state.attached.has(7), 'cdp.detach must release the tab');
assert(state.attached.has(8), 'detaching one tab must not touch another');

state.attached.delete(8);
chrome.debugger.onDetach.fire({ tabId: 8 }, 'canceled_by_user');
const before = state.attachCalls.filter((tabId) => tabId === 8).length;
reply = await command({ cmd: 'cdp', method: 'Runtime.evaluate', params: {}, tabId: 8, persistent: true });
assert(state.attachCalls.filter((tabId) => tabId === 8).length === before + 1,
  'a detached tab must be attachable again');

process.stdout.write(JSON.stringify({
  ok: true,
  removed: state.removed,
  attachCalls: state.attachCalls,
  detachCalls: state.detachCalls,
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
    assert payload["ok"] is True
    assert 10 in payload["removed"]
    assert 7 in payload["attachCalls"] and 8 in payload["attachCalls"]
    assert 11 in payload["detachCalls"]
