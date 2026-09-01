'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const staticDir = path.join(root, 'zero_agent', 'frontends', 'desktop', 'static');
const appPath = path.join(staticDir, 'app.js');
const adapterPath = path.join(staticDir, 'za-web.js');

async function adapterTests() {
  const calls = [];
  const windowObj = {
    location: {
      protocol: 'http:',
      hostname: '127.0.0.1',
      hash: '',
      pathname: '/',
      search: '',
    },
    history: { replaceState() {} },
  };
  class FakeWebSocket {
    static get CONNECTING() { return 0; }
    static get OPEN() { return 1; }
    constructor() { this.readyState = 0; }
    addEventListener() {}
  }
  const context = {
    window: windowObj,
    location: windowObj.location,
    document: {},
    navigator: { platform: 'MacIntel' },
    WebSocket: FakeWebSocket,
    console,
    fetch: async (url, init = {}) => {
      calls.push({ url, init });
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        text: async () => JSON.stringify({ ok: true, channels: [] }),
      };
    },
  };
  context.globalThis = context;
  vm.runInNewContext(fs.readFileSync(adapterPath, 'utf8'), context, { filename: adapterPath });
  calls.length = 0;

  await windowObj.zeroAgent.rpc('channels/list', {});
  assert.equal(calls[0].url, 'http://127.0.0.1:14168/channels');
  assert.equal(calls[0].init.method, undefined);

  await windowObj.zeroAgent.rpc('channels/start', { channelId: 'telegram' });
  assert.equal(calls[1].url, 'http://127.0.0.1:14168/channels/telegram/start');
  assert.equal(calls[1].init.method, 'POST');

  await windowObj.zeroAgent.rpc('channels/link', { channelId: 'telegram', linked: false });
  assert.equal(calls[2].url, 'http://127.0.0.1:14168/channels/telegram/link');
  assert.equal(calls[2].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[2].init.body), { linked: false });
}

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
  toggle(name, force) {
    const next = force === undefined ? !this.values.has(name) : !!force;
    if (next) this.add(name); else this.remove(name);
    return next;
  }
  contains(name) { return this.values.has(name); }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = '';
    this.classList = new FakeClassList();
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = new Map();
    this.textContent = '';
    this.innerHTML = '';
    this.disabled = false;
  }
  appendChild(node) { this.children.push(node); node.parentNode = this; return node; }
  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index >= 0) this.children.splice(index, 1);
    node.parentNode = null;
    return node;
  }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  focus() {}
}

function makeAppContext() {
  const elements = new Map();
  const document = {
    body: new FakeElement('body'),
    documentElement: new FakeElement('html'),
    createElement: (tag) => new FakeElement(tag),
    createDocumentFragment: () => new FakeElement('fragment'),
    getElementById: (id) => elements.get(id) || null,
    querySelectorAll: () => [],
  };
  for (const id of ['channel-list', 'channel-settings-modal', 'channel-settings-btn']) {
    elements.set(id, new FakeElement('div'));
  }
  const context = {
    console,
    Date,
    Math,
    JSON,
    Set,
    Map,
    RegExp,
    String,
    Number,
    Boolean,
    Array,
    Object,
    Error,
    TypeError,
    Promise,
    setTimeout,
    clearTimeout,
    performance: { now: () => 1000 },
    navigator: { platform: 'test' },
    localStorage: { getItem: () => null, setItem: () => {} },
    document,
    window: {},
  };
  context.globalThis = context;
  return { context, elements };
}

async function rendererTests() {
  const { context, elements } = makeAppContext();
  const source = fs.readFileSync(appPath, 'utf8');
  const bridgeMarker = source.indexOf('// ─── Bridge events');
  assert.ok(bridgeMarker > 0);
  vm.runInNewContext(source.slice(0, bridgeMarker) + `
channelListEl = document.getElementById('channel-list');
channelSettingsModal = document.getElementById('channel-settings-modal');
globalThis.__testExports = { state, renderChannelList, handleChannelToggle, channelErrorMessage };
`, context, { filename: appPath });

  const t = context.__testExports;
  const channelList = elements.get('channel-list');
  const rows = [
    {
      id: 'telegram',
      label: 'Telegram',
      module: 'bots/telegram_app.py',
      configured: false,
      requiredKeys: ['tg_bot_token'],
      running: false,
      pid: null,
      linked: true,
    },
    {
      id: 'discord',
      label: 'Discord',
      module: 'bots/discord_app.py',
      configured: true,
      requiredKeys: ['discord_bot_token'],
      running: true,
      pid: 321,
      linked: true,
    },
  ];
  t.state.channelStatuses = rows;
  t.renderChannelList();
  assert.match(channelList.innerHTML, /Telegram/);
  assert.match(channelList.innerHTML, /Discord/);
  assert.match(channelList.innerHTML, /未配置/);
  assert.match(channelList.innerHTML, /运行中/);
  assert.doesNotMatch(channelList.innerHTML, /tg_bot_token/);
  assert.doesNotMatch(channelList.innerHTML, /test-secret-value/);
  assert.equal(
    t.channelErrorMessage({ status: 409, message: 'Telegram 未配置: tg_bot_token' }, '启动渠道'),
    '启动渠道失败：Telegram 未配置: tg_bot_token',
  );

  const calls = [];
  context.window.zeroAgent = {
    rpc: async (method, params) => {
      calls.push({ method, params });
      return {
        ok: true,
        channels: rows.map((row) => row.id === 'telegram' ? { ...row, linked: false } : row),
      };
    },
  };
  await t.handleChannelToggle('telegram', 'linked', false);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'channels/link');
  assert.equal(calls[0].params.channelId, 'telegram');
  assert.equal(calls[0].params.linked, false);
  assert.equal(t.state.channelStatuses[0].linked, false);
}

Promise.all([adapterTests(), rendererTests()])
  .then(() => console.log('frontend_channels.test.js OK'))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
